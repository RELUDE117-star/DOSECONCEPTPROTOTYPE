"""
DOSE VOICE — fully offline voice assistant for the Dose Home Station
====================================================================
Wake word:  "Hey Dose"
Ears:       Vosk (offline speech recognition, small English model)
Voice:      Piper (offline neural text-to-speech, soft human voice)
Brain:      local intent engine — personality modeled on BT-7274
            (precise, literal, loyal; addresses the user as "Ryan")

No cloud. No API keys. Everything runs on the device.

Expected model layout (installed by DOSE.sh):
    ~/dose-home-station/voice/vosk-model*/        Vosk model directory
    ~/dose-home-station/voice/*.onnx (+ .json)    Piper voice
"""

import difflib
import glob
import json
import os
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime

try:                       # strong deterministic NLU (phonetic drug matcher)
    import dose_nlu as _nlu_mod
except Exception:
    _nlu_mod = None

# The Mac in the same room, if it is in the same room. Optional by
# construction: a missing module, a missing config or a closed laptop
# all mean "use the local models", which is what this device did before
# any of this existed. See dose_remote_stt.
try:
    import dose_remote_stt as _remote_stt
except Exception:
    _remote_stt = None

# ── audioop shim ──────────────────────────────────────────────────────
# Python 3.13 REMOVED the stdlib 'audioop' module. Every audio
# measurement here (rms/max/mul/tomono/ratecv) depends on it — without
# it the mic level always read 0 and the tests errored 'no module named
# audioop'. Prefer the real module (or the pip 'audioop-lts' backport);
# otherwise install a pure-Python fallback so the mic works on 3.13.
try:
    import audioop  # noqa: F401  (real module, or audioop-lts backport)
except Exception:
    import array as _array
    import types as _types

    def _ao_rms(data, width=2):
        a = _array.array('h'); a.frombytes(data[:len(data) // 2 * 2])
        if not a:
            return 0
        return int((sum(x * x for x in a) / len(a)) ** 0.5)

    def _ao_max(data, width=2):
        a = _array.array('h'); a.frombytes(data[:len(data) // 2 * 2])
        return max((abs(x) for x in a), default=0)

    def _ao_mul(data, width, factor):
        a = _array.array('h'); a.frombytes(data[:len(data) // 2 * 2])
        for i in range(len(a)):
            v = int(a[i] * factor)
            a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
        return a.tobytes()

    def _ao_tomono(data, width, lf, rf):
        a = _array.array('h'); a.frombytes(data[:len(data) // 4 * 4])
        out = _array.array('h')
        for i in range(0, len(a) - 1, 2):
            v = int(a[i] * lf + a[i + 1] * rf)
            out.append(32767 if v > 32767 else
                       (-32768 if v < -32768 else v))
        return out.tobytes()

    def _ao_ratecv(data, width, ch, in_rate, out_rate, state):
        a = _array.array('h'); a.frombytes(data[:len(data) // 2 * 2])
        if in_rate == out_rate or not a:
            return data, state
        ratio = out_rate / float(in_rate)
        n_out = int(len(a) * ratio)
        out = _array.array('h')
        for i in range(n_out):
            src = i / ratio
            j = int(src)
            if j + 1 < len(a):
                frac = src - j
                out.append(int(a[j] * (1 - frac) + a[j + 1] * frac))
            elif j < len(a):
                out.append(a[j])
        return out.tobytes(), state

    audioop = _types.ModuleType("audioop")
    audioop.rms = _ao_rms
    audioop.max = _ao_max
    audioop.mul = _ao_mul
    audioop.tomono = _ao_tomono
    audioop.ratecv = _ao_ratecv
    audioop.error = Exception
    sys.modules["audioop"] = audioop   # so local 'import audioop' works

VOICE_DIR = os.environ.get(
    "DOSE_VOICE_DIR", os.path.expanduser("~/dose-home-station/voice"))
# Every place that can delete a pre-rendered clip writes one line here,
# and the heartbeat prints the last of them. Three pieces of code can
# do it — this file, dose_app._retire_other_voices and DOSE.sh — and
# working out which one from the outside cost two device round trips
# and two wrong answers.
CACHE_PURGE_LOG = os.path.join(VOICE_DIR, "cache_purges.log")


def _cache_purge_note(msg):
    """Record who deleted pre-rendered speech, and when. Never raises:
    a diagnostic that can break the thing it is watching is worse than
    no diagnostic."""
    try:
        os.makedirs(VOICE_DIR, exist_ok=True)
        with open(CACHE_PURGE_LOG, "a") as f:
            f.write("%s pid=%d %s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(),
                       msg))
    except Exception:
        pass
LEARN_PATH = os.path.join(VOICE_DIR, "learning.json")
LEARN_FUZZ = 0.87          # similarity for a learned phrase to fire
MAX_LEARNED = 300
SAMPLE_RATE = 16000
# 64 ms per block, not 125 ms. The block size sets how often anything
# can happen: how fast a push-to-talk request is noticed, how often
# the live transcript on screen can update, and the granularity of the
# endpointer. Halving it halves the floor on all three, and a 64 ms
# block is still 8 Silero frames' worth of work every 64 ms — about
# 2 ms of a core on a Pi.
BLOCK_SIZE = int(os.environ.get("DOSE_BLOCK_SIZE", "1024"))
COMMAND_TIMEOUT = 9.0      # seconds of silence before giving up
FLOW_TIMEOUT = 20.0        # per-question timeout in multi-turn flows
# After an answer the microphone stays open this long for a follow-up,
# so a conversation can continue without starting over every sentence.
FOLLOWUP_TIMEOUT = float(os.environ.get("DOSE_FOLLOWUP_TIMEOUT", "8.0"))

# ── VOICE / LATENCY ──────────────────────────────────────────────────
# The station has to feel like a conversation, not like waiting on a
# machine, so the target is under a second from "you stop talking" to
# "it starts talking". Two things get us there:
#
#   * a voice that synthesizes FASTER than real time on a Pi 4.
#     hfc_female runs at about RTF 0.15 (~3x faster than real time);
#     Kokoro is RTF ~0.48 and Piper "Amy" is slower still — Amy was
#     the reason replies dragged, so it is no longer used at all.
#   * sentence-level streaming: the first sentence starts playing
#     while the rest is still being rendered (see _speak).
# THE VOICE. One name, one file, one engine — deliberately.
#
# The station used to carry several: Piper "Amy" locally, plus Kokoro
# and a second Piper through moonshine-voice. That was slower (Amy and
# Kokoro are both slower than real time on a Pi 4) and less reliable
# (whichever one happened to load first decided what she sounded like,
# so the same device could answer in two different voices). There is
# now exactly one, and nothing can substitute for it.
VOICE_NAME = "en_US-hfc_female-medium"

# ── WHEN HAS THE PERSON FINISHED TALKING? ────────────────────────────
# A single silence timer cannot answer this. Too short and it cuts
# people off mid-sentence; too long and every reply feels sluggish.
# So the wait depends on WHAT WAS SAID SO FAR — the assistant commits
# quickly on a sentence that is obviously finished and waits patiently
# on one that obviously is not:
#
#   crisis / emergency        commit at once — never make them repeat
#   a complete, stable command  0.35 s   "what time is it"
#   dispense or cancel        0.60 s   leaves room for "no wait, the—"
#   parses to nothing         0.70 s   probably still forming it
#   ends mid-thought          2.20 s   "how many of my..." — WAIT
#
# The mid-thought figure is not a guess. A 900 ms mid-sentence pause
# was measured beating a 700 ms grace, cutting "I've been thinking
# about ... ending my life" in half and answering the first part. 2.2 s
# covers the pause ladder people actually produce. It costs nothing in
# the common case, because that case is the 0.35 s branch.
ENDPOINT_STABLE = float(os.environ.get("DOSE_ENDPOINT_STABLE", "0.35"))
ENDPOINT_CORRECTION = float(os.environ.get("DOSE_ENDPOINT_CORRECTION",
                                           "0.60"))
ENDPOINT_UNPARSED = float(os.environ.get("DOSE_ENDPOINT_UNPARSED", "0.70"))
ENDPOINT_DANGLING = float(os.environ.get("DOSE_ENDPOINT_DANGLING", "2.20"))
# Someone has to stop eventually — a television will not. Cut an
# utterance that never ends rather than listening forever.
ENDPOINT_MAX_UTTERANCE = float(os.environ.get("DOSE_MAX_UTTERANCE", "8.0"))

# How far above the room's own noise a block must be to count as
# speech. 3x is about 10 dB. Raising it rejects more noise but also
# rejects a quiet voice from across the room, which is the trade a
# far-field microphone exists to avoid.
NOISE_GATE_RATIO = float(os.environ.get("DOSE_NOISE_GATE", "3.0"))

# The highest the learned room floor may go. At the default 3x ratio
# this puts the gate at 2400 — a normal speaking voice arrives well
# above that. Without a ceiling a loud room can raise the bar past
# anything a person produces, and the station is then deaf until the
# floor decays. Being a bit noisy is recoverable; being deaf is not.
NOISE_FLOOR_MAX = float(os.environ.get("DOSE_NOISE_FLOOR_MAX", "800"))

# How close a mis-transcribed request has to sound to a command before
# we act on it. Measured: at anything from 70 to 80 this matches 11 of
# 12 realistic mishears and never once fires on a medication name, a
# crisis phrase, or a medical question — the things that must never be
# guessed at.
VOCAB_THRESHOLD = float(os.environ.get("DOSE_VOCAB_THRESHOLD", "76"))

# ── WHAT IT CAN BE ASKED, IN WORDS PEOPLE USE ────────────────────────
# Matched phonetically as a last resort, so a mis-transcribed request
# still lands on the right action. Kept to things the station can
# actually DO — it is a medication cabinet, not a chatbot, and an
# assistant that pretends to understand everything is worse than one
# that says plainly when it does not.
COMMAND_VOCAB = {
    "nav:home": (
        "home", "main screen", "go back", "start screen", "the front",
        "back to the start", "main menu", "the dashboard",
    ),
    # "my pills" is deliberately NOT here: it collides with questions
    # about how many are left, and with phrases the user has taught.
    # A screen name has to be a screen name.
    "nav:storage": (
        "storage", "my medications", "medicine cabinet", "the cabinet",
        "medication screen", "the storage screen", "my bottles",
        "the medicine drawer", "what is loaded", "the inventory",
    ),
    # NOTE: no bare "setup" alias. It scored a perfect match against
    # the "set up" in "set up a medication" and hijacked that command
    # to the Settings screen. "settings", "options", "preferences" and
    # "configuration" cover this intent without the collision.
    "nav:settings": (
        "settings", "options", "preferences", "the settings",
        "configuration", "the options screen", "change a setting",
        "system settings", "open settings",
    ),
    "nav:user": (
        "my profile", "my record", "my stats", "my history",
        "user screen", "my progress", "my account", "my details",
        "about me", "my information",
    ),
    "time": (
        "what time is it", "the time", "current time", "got the time",
        "do you have the time", "tell me the time", "what is the time",
        "what time do you have", "clock",
    ),
    "date": (
        "what day is it", "todays date", "what is the date",
        "what day of the week is it", "what is today", "the date",
        "todays day",
    ),
    "remaining_today": (
        "what do i take today", "what is left today", "my doses today",
        "todays medication", "what do i need today",
        "anything left today", "what have i got today",
        "what is on for today", "what is due today",
        "what is still due", "what do i have left today",
        "todays schedule", "my schedule today", "what is outstanding",
        "anything else today", "what remains today",
    ),
    "next_dose": (
        "what is next", "next dose", "when is my next dose",
        "what do i take next", "when is the next one",
        "what comes next", "when do i take the next one",
        "what is coming up", "when is the next dose due",
        "what is due next", "when do i take my next pill",
        "how long until my next dose",
    ),
    "taken_today": (
        "did i take my medicine", "have i taken my pills",
        "am i caught up", "did i miss anything",
        "have i taken everything", "did i take everything",
        "am i up to date", "have i missed a dose",
        "did i already take them", "have i had my medication",
        "am i all done for today", "did i take them all",
    ),
    "count": (
        "how many are left", "how many pills do i have", "pill count",
        "am i running low", "how many do i have left",
        "how much is left", "do i need a refill", "am i running out",
        "what is my supply", "how many are in there",
        "how many tablets are left", "do i have enough",
    ),
    "adherence": (
        "how am i doing", "my score", "my adherence", "am i on track",
        "how have i been doing", "am i doing well", "my track record",
        "how good have i been", "am i keeping up",
        "have i been taking them", "how is my streak",
        "what is my score",
    ),
    "addmed": (
        "add a medication", "add a new medication",
        "register a medication", "new prescription", "add a new pill",
        "put in a new medication", "set up a medication",
        "i have a new prescription", "add something new",
        "register a new bottle",
    ),
}

# Every id above must be one _dispatch can actually act on. "help",
# "repeat" and "cancel" are handled earlier in respond() and are NOT
# listed here on purpose: routing to an id the dispatcher does not
# know produces "Instruction unclear", which is worse than the keyword
# rules that already catch them.
# Single distinctive words that belong to a COMMAND, never to a
# medication. A one-word name pulled out of a sentence can only be
# compared against these — "nekst" cannot be scored against a phrase
# like "what is next", because the length guard in the matcher
# (correctly) refuses to compare a 5-letter word with a 10-letter one.
COMMAND_WORDS = (
    "next", "time", "today", "tonight", "date", "day", "count",
    "left", "remaining", "schedule", "score", "adherence", "record",
    "profile", "storage", "settings", "home", "medicine", "medication",
    "medications", "pills", "doses", "dose", "everything",
)

assert all(k.startswith("nav:") or k in {
    "time", "date", "remaining_today", "next_dose", "taken_today",
    "count", "adherence", "addmed"} for k in COMMAND_VOCAB), \
    "COMMAND_VOCAB contains an intent _dispatch cannot handle"

# ── THE RECOGNISER ───────────────────────────────────────────────────
# Whisper, in order of preference. The HuggingFace audio course builds
# its assistant's transcription stage on openai/whisper-base.en for CPU
# and names whisper-small.en as the upgrade; these are the CTranslate2
# conversions of the same weights, which is what runs them fast enough
# on a Pi. distil-small.en is first because it is a distillation of
# small.en: the same class of accuracy at roughly twice the speed.
#
# Downloaded from HuggingFace on first use and cached on the device.
# All MIT/Apache, all on-device, nothing metered.
# base.en first, not small. On a Pi 4 the bigger models take SECONDS
# per utterance; an answer that arrives after the person has given up
# is not an answer. base.en is the largest that stays usable here, and
# DOSE_WHISPER_MODELS can name a bigger one on better hardware.
WHISPER_MODELS = tuple(filter(None, os.environ.get(
    "DOSE_WHISPER_MODELS",
    "base.en,distil-small.en,tiny.en").split(",")))

# THE FAST PATH is chosen on the actual hardware, not assumed.
#
# A real device measured moonshine tiny at 4.47 SECONDS on one word —
# 30x slower than it should be, and the single biggest reason the
# station felt broken. Rather than guess whether moonshine or Whisper
# is faster on a given board (moonshine advertises itself as faster,
# yet lost badly here), the station RACES them once at startup on
# identical audio and uses whichever actually wins. FAST_WHISPER_MODEL
# is the Whisper contender; base.en stays the escalation model for the
# minority of turns the fast one can't make out. Set DOSE_FAST_ENGINE
# to "moonshine" or "whisper" to skip the race and pin one.
FAST_WHISPER_MODEL = os.environ.get("DOSE_FAST_WHISPER", "tiny.en")
FAST_ENGINE_PIN = os.environ.get("DOSE_FAST_ENGINE", "").strip().lower()

# ── HOW LONG A TURN IS ALLOWED TO SPEND BEING UNDERSTOOD ──────────────
# Not a target — a CEILING, and one the station enforces rather than
# hopes for. The device's own turns.jsonl recorded a 25.19 s turn made
# of a 17.26 s fast pass with an escalation started on top of it,
# because no stage asked what time it was before beginning.
#
# Six seconds is chosen against what the person does, not against what
# the models want: past about five seconds of silence someone assumes
# the machine did not hear them and says it again, which starts a new
# turn and makes everything worse. Raise it on better hardware; the
# escalation simply gets used more often.
STT_TURN_BUDGET = float(os.environ.get("DOSE_STT_BUDGET", "7.0"))
# THE HARD CEILING ON ONE LOCAL RECOGNISER CALL.
#
# STT_TURN_BUDGET gates the base.en escalation — the SECOND step. The
# device then recorded a FIRST step of 105.14 s, and the turn total
# came to 106.32 s, because nothing above the escalation was asking
# what time it was. The station gave a sensible answer to an empty
# room.
#
# Eight seconds is past the point where a person has already decided
# the machine did not hear them, so there is nothing to protect after
# it. The call cannot be cancelled, so it is abandoned rather than
# stopped: see _fast_transcribe. Zero disables the ceiling.
STT_LOCAL_CEILING = float(os.environ.get("DOSE_STT_LOCAL_CEILING", "8.0"))
# ...and the shortest wait worth starting one for. A recogniser handed
# 0.2 s of wall clock is a wasted pass and a guaranteed abandon; below
# this the turn is better served by the live transcript it already has.
STT_LOCAL_FLOOR = float(os.environ.get("DOSE_STT_LOCAL_FLOOR", "1.5"))
# The least audio that could hold a question. Below this no recogniser
# is run at all: the device spent 9.90 s on 0.36 s of noise and
# returned nothing, which is the worst trade in the log. Measured
# against the turns that worked — every correct one carried at least
# 0.88 s of trimmed audio, and the shortest good utterance, "Okay.",
# had 1.9 s of buffer behind it.
MIN_TURN_AUDIO_S = float(os.environ.get("DOSE_MIN_TURN_AUDIO", "0.5"))
# The most audio a single turn may hand the recogniser. Transcription
# cost is linear in length, so the worst turn is the longest one: the
# device logged "fast": 25.02 on a buffer that had been accumulating
# through a long pause. A budget downstream cannot rescue that — by the
# time it is consulted the twenty-five seconds are already spent.
# Twelve seconds is far longer than anything anyone says to a medicine
# cabinet, and the LAST twelve are the ones kept.
STT_MAX_AUDIO_S = float(os.environ.get("DOSE_STT_MAX_AUDIO", "12.0"))

# Turn measurement without a person standing in front of the cabinet.
# See the hook in the supervising loop. Default OFF; a station in
# somebody's kitchen never has this set.
TEST_HOOKS = os.environ.get("DOSE_TEST_HOOKS", "").strip().lower() in (
    "1", "true", "yes", "on")
# The longest FIRST spoken fragment. Only this chunk is rendered before
# any sound comes out, so this number IS the station's time-to-first-
# sound on an uncached reply.
#
# IT WAS 42, AND "PIPER RENDERS THAT IN WELL UNDER ONE SECOND" WAS
# MEASURED IN THE WRONG PROCESS.
#
# Standalone, yes. Inside the application, the device's own turn rows
# say otherwise, and they scale almost linearly with the length of
# this chunk:
#
#     13 chars   0.95 s
#     21 chars   2.30 s
#     29 chars   2.19 s
#     31 chars   2.32 s
#     38 chars   3.95 s
#
# So this constant is not a style choice, it is the latency, and
# halving it roughly halves the wait before the station starts
# talking. The seam is inaudible — _split_first() cuts at the
# strongest boundary available and Piper pauses at a comma anyway —
# and the remainder still renders on a worker while the opening plays,
# so nothing is lost but silence.
#
# BUT NOT AS LOW AS LATENCY ALONE WOULD WANT.
#
# Twenty-two looked right on the numbers and the test suite refused
# it, correctly: at 22 the full stop in "I didn't catch that, Ryan.
# Tap the logo and try again." falls OUTSIDE the window, so an earlier
# COMMA wins the break and the station opens mid-thought. The same
# narrowing stopped long single-clause replies splitting at all.
#
# Those are speech-quality properties, measured and pinned in
# tests/test_tts_first_sound.py, and they are not worth half a second.
# Thirty-two keeps every one of them — the full stop is still inside
# the window, the overshoot still reaches a comma at 48 — while
# cutting a quarter off the opening fragment.
#
# The real win is not here anyway. It is the cache: a hit costs
# 0.0002 s against 2.4 s, and prewarm_replies() now caches the chunks
# _speak() actually looks up rather than whole lines it never asks for.
TTS_FIRST_CHUNK_MAX = int(os.environ.get("DOSE_TTS_FIRST_MAX", "32"))
# ...and the shortest. Below this a reply opens with a stutter, which
# sounds broken in a way that being half a second slower does not.
TTS_FIRST_CHUNK_MIN = int(os.environ.get("DOSE_TTS_FIRST_MIN", "12"))
# THE OPENINGS OF REPLIES THAT CAN NEVER BE CACHED WHOLE.
#
# Most of what this station says is assembled at the moment it answers
# — a time, an inventory, a medication name — so the whole line is
# different every time and the cache can never hold it. The device
# measured the result: three turns at speak 0.00 because their replies
# were fixed, beside
#
#     "The time is 4:48 PM."              speak 3.20
#     "One dose remains today: Atorva..."  speak 2.94
#
# But the OPENING of each of those never changes, and the opening is
# the only part on the critical path — the rest renders while it plays.
# "The time is" has no comma and no full stop inside it, so
# _split_first() had nothing to break on and rendered the whole line.
#
# These are declared, not detected, and that is the point: each one is
# a phrase somebody chose as an opening and can hear ending cleanly.
# _split_first() may break after any of them; prewarm_replies() renders
# every one at startup. Adding a phrase here without saying it out loud
# first is how a station starts opening mid-thought.
INVARIANT_OPENINGS = (
    "Current inventory:",
    "One dose remains today:",
    "I could not find",
    "The time is",
    "You have",
)
# The remainder after a declared opening only has to be SOMETHING.
#
# TTS_FIRST_CHUNK_MIN (12) exists to stop the chunker finding a comma
# three characters in and opening with a stutter. It does not apply
# here: the opening is a phrase a person chose, and the tail is
# whatever the station is actually reporting. "4:48 PM." is eight
# characters and a complete thought, and refusing to split there cost
# 3.20 s of silence on the most-asked question this station gets.
#
# Three characters, only to reject a tail that is punctuation.
TTS_OPENING_MIN_REST = 3
# base.en against tiny.en on identical audio. Used only to decide
# whether the escalation FITS, never to time anything out, and it is
# multiplied by this device's own freshly measured fast-pass time, so
# the estimate tracks thermal throttling and load for free.
# MEASURED ON THIS BOARD, not assumed. Same recording, three passes
# each, median, four threads:
#
#     tiny.en   2.12 s        base.en   3.98 s
#
# so base.en is 1.9x tiny.en here, not the 3x this started at. The
# guess was making the station skip escalations it had time for, which
# trades accuracy away for latency it was not actually short of.
ESCALATION_COST_RATIO = float(
    os.environ.get("DOSE_ESCALATION_RATIO", "2.0"))

# THE BENCHMARK the station scores itself against — the owner's targets,
# in one place so the self-test and the audit report the same numbers.
TARGET_ACCURACY = float(os.environ.get("DOSE_TARGET_ACCURACY", "99.5"))
TARGET_LATENCY = float(os.environ.get("DOSE_TARGET_LATENCY", "0.5"))
TARGET_LATENCY_WORST = float(os.environ.get("DOSE_TARGET_LATENCY_WORST",
                                            "0.8"))
TARGET_TEMP_MAX = float(os.environ.get("DOSE_TARGET_TEMP", "80"))



# ── LEVELS ───────────────────────────────────────────────────────────
# Unity on the way in. Every boost here multiplies with the ALSA
# capture level and with the software auto-gain, and three of those at
# once is how speech ended up arriving at nearly full scale.
SOURCE_VOLUME = float(os.environ.get("DOSE_SOURCE_VOLUME", "1.0"))

# The speaker is NOT run at maximum. It sits inches from the
# microphone: every dB of it that leaks back in is noise the
# recogniser has to hear the person through, and it raises the
# measured room floor so real speech has to clear a higher gate.
# Loud enough to hear across a room, quiet enough not to deafen the
# thing listening for you.
SINK_VOLUME = float(os.environ.get("DOSE_SINK_VOLUME", "0.65"))

# Where speech should land. The recogniser wants a healthy signal with
# headroom, not a hot one — around a third of full scale.
TARGET_SPEECH_RMS = float(os.environ.get("DOSE_TARGET_RMS", "3000"))
# Above this the input is too hot and the hardware level gets stepped
# down, whatever a previous run left behind in the mixer.
HOT_SPEECH_RMS = float(os.environ.get("DOSE_HOT_RMS", "6000"))
# Below this, speech is too quiet for the recogniser and the hardware
# capture is stepped UP. This is the recovery path for a mic left stuck
# low by the down-only watchdog (device reported voice RMS 15).
LOW_SPEECH_RMS = float(os.environ.get("DOSE_LOW_RMS", "500"))

# ── capture liveness ──────────────────────────────────────────────────
# How long the DEVICE can go without handing us a single block before we
# treat the capture as dead. This is deliberately not about speech: a
# quiet room still delivers blocks, and the old ten-second rule (keyed
# off speech reaching the queue) tore down a healthy microphone every
# ten seconds of silence. Reopening takes longer than that on this
# board, so the clock was already expired when the new stream came up.
CAPTURE_DEAD_AFTER = float(os.environ.get("DOSE_CAPTURE_DEAD_AFTER", "8"))
# Floor between reopen attempts, and the ceiling it backs off to. A mic
# that cannot be reopened must not be hammered: every attempt walks a
# long device/rate/channel list and is another chance to strand a PCM.
CAPTURE_REOPEN_MIN_GAP = float(
    os.environ.get("DOSE_CAPTURE_REOPEN_GAP", "5"))
CAPTURE_REOPEN_MAX_GAP = float(
    os.environ.get("DOSE_CAPTURE_REOPEN_MAX_GAP", "60"))

# ── DIGITAL SILENCE ───────────────────────────────────────────────────
# The watchdog above answers "did the device stop delivering blocks".
# On 2026-09-18 this station failed a DIFFERENT way, and nothing in the
# program noticed it for hours:
#
#     HEARING: YES    blocks/sec: 46.4    live level: peak 0  rms 0
#
# 46.4 blocks/sec is exactly 48000/1024 — the capture was perfect. The
# kernel's hw_ptr advanced 144,385 frames in 3 s, arecord's wchar
# climbed at 96,000 B/s, the app's rchar climbed in step, and a
# standalone `arecord -D plughw:5,0` at the same moment read peak 8917.
# Every byte the ENGINE received was zero.
#
# CAPTURE_DEAD_AFTER cannot see that: blocks were arriving, on time,
# for ever. The station was deaf and every liveness check said YES. It
# stayed that way until a person noticed and said so — which, for a
# medication cabinet somebody relies on, is not a recovery story.
#
# So: blocks arriving AND peak at the dead-endpoint floor for this long,
# while idle, is itself a fault. A real microphone in a silent room
# peaks at 29-107 (see ROUTE_LIVE_PEAK); only a dead endpoint reads 0.
# Sixty seconds is long enough that no ordinary quiet can trip it and
# short enough that the station heals itself well inside a conversation.
# TEN MINUTES, and the device is why.
#
# The first guess was sixty seconds, on the reasoning that a real
# microphone in a silent room still shows an analog noise floor. It
# does — three seconds of this capsule read peak 29 and 858 non-zero
# samples — but that is not what a HEARTBEAT sees. The engine measures
# one 21 ms block at a time, on one channel, and in a genuinely empty
# room most of those blocks are exactly zero. Watched live, the station
# sat at peak 0 for twenty-seven seconds at a stretch with a capture
# that the acceptance run then measured at peak 20,347.
#
# So quiet and dead look identical over a short window, and no
# threshold cleverness fixes that. What separates them is TIME: a room
# somebody lives in produces something inside ten minutes — a door, a
# chair, a fridge, a footstep — and the fault this watchdog exists for
# is permanent and total. Waiting ten minutes to heal by itself is
# unarguably better than never, and it cannot fire on a quiet evening.
SILENT_CAPTURE_AFTER = float(
    os.environ.get("DOSE_SILENT_CAPTURE_AFTER", "600"))
# Minimum gap between rungs of the recovery ladder. Each rung costs a
# device reopen at most; giving the previous one time to prove itself
# matters more than climbing fast, and a mic that has just been reopened
# needs a few seconds of blocks before its peak means anything.
SILENCE_STEP_GAP = float(os.environ.get("DOSE_SILENCE_STEP_GAP", "25"))
# ...and the horizon for a capture that DID work and has since gone
# quiet. Half an hour, because that case is not the fault this was
# written for and the cost of being wrong is tearing down a microphone
# that works. The fault itself never delivers a single non-zero sample,
# so it is caught by SILENT_CAPTURE_AFTER above and never reaches this.
SILENT_DEAD_AFTER = float(
    os.environ.get("DOSE_SILENT_DEAD_AFTER", "1800"))

# ---------------------------------------------------------------------
# DEADLINES ON DEVICE SELECTION.
#
# WHY THESE EXIST — a py-spy dump of the live station, 2026-09-18:
#
#     Thread 76603 (idle): "Thread-5 (_run)"
#         stop (sounddevice.py:1143)
#         _probe_device (dose_voice.py:1946)
#         _pick_input_device (dose_voice.py:2339)
#         open_portaudio (dose_voice.py:4111)
#         open_capture (dose_voice.py:4568)
#         _run (dose_voice.py:4658)
#
# The voice engine was not listening. It was not crashed, not
# restarting, not out of CPU. It was parked inside PortAudio's
# Pa_StopStream, seven minutes into a device probe, holding the ALSA
# PCM in SETUP, and it was never coming back. Every symptom that has
# been called "the microphone is flaky" for months is downstream of
# this: the station was still deciding which microphone to use.
#
# Nothing in device selection is allowed to be unbounded any more.
# Selection is a best-effort search, and a search that cannot finish
# must lose, loudly, rather than hang the product forever.
PROBE_RATE_SECONDS = float(       # audio captured per rate, per device
    os.environ.get("DOSE_PROBE_RATE_SECONDS", "0.6"))
PROBE_CLOSE_TIMEOUT = float(      # how long a stream close may take
    os.environ.get("DOSE_PROBE_CLOSE_TIMEOUT", "2.0"))
PROBE_DEVICE_BUDGET = float(      # whole probe of ONE device
    os.environ.get("DOSE_PROBE_DEVICE_BUDGET", "4.0"))
PROBE_PICK_BUDGET = float(        # scanning EVERY PortAudio device
    os.environ.get("DOSE_PROBE_PICK_BUDGET", "12.0"))
CAPTURE_OPEN_BUDGET = float(      # the entire route walk in open_capture
    os.environ.get("DOSE_CAPTURE_OPEN_BUDGET", "45.0"))

# PEAK sample value at or above which a capture route counts as a real,
# connected microphone rather than a dead endpoint. Measured on this
# station in a quiet room: the working AIRHUG peaks at 29-107, the dead
# "USB Composite Device" on card 4 peaks at exactly 0. Anything above a
# couple of counts is an analog noise floor, which is something only a
# real microphone has. See route_floor() for why this must be peak and
# never RMS - in the same recordings, RMS was 0 for BOTH.
ROUTE_LIVE_PEAK = int(os.environ.get("DOSE_ROUTE_LIVE_PEAK", "3"))

# How long a route is listened to before it is judged, and how long a
# route that has NOT proven itself is given before it is condemned.
#
# 1.6 s was the whole window, and it was not long enough to ask. In
# this room about 1.2% of blocks carry a non-zero sample (38 of 3100,
# measured), so 1.6 s is about 75 blocks and an expected 0.9 of them
# carry anything at all. The station rejected its own working
# microphone on that coin toss roughly half the time, TERMed it, and
# started the walk again — 146 reopens in six minutes, every one of
# them "Aborted by signal Terminated" at exactly 94 blocks.
#
# Listening stops the instant a sample clears ROUTE_LIVE_PEAK, so a
# live route costs about what it always did and only a silent one pays
# the patience.
ROUTE_FLOOR_SECONDS = float(os.environ.get("DOSE_ROUTE_FLOOR_SECONDS",
                                           "1.6"))
ROUTE_FLOOR_PATIENCE = float(os.environ.get("DOSE_ROUTE_FLOOR_PATIENCE",
                                            "5.0"))

# How often the stereo downmix re-decides which channel carries the
# microphone. Once a second is far more often than a soldered capsule
# changes sides, and doing it every block cost three quarters of the
# reader thread's budget — see the downmix in the capture reader.
CHANNEL_RECHECK = int(os.environ.get("DOSE_CHANNEL_RECHECK", "47"))

# How much accumulated peak a channel needs before the downmix believes
# it and stops summing both. Below this the choice is not "left" — it
# is "we do not know yet", and the two are not the same: `>=` resolves
# a tie to left, and on a capsule wired to the right that is silence
# forever. One count is no evidence; a capsule that has been heard sits
# orders of magnitude above it.
CHANNEL_DECIDED = float(os.environ.get("DOSE_CHANNEL_DECIDED", "1.0"))

# WHICH PASS AM I?
#
# The speculative transcription runs on its own thread, at the same
# time as the real one, and both are methods on the same object. So a
# flag on `self` would be read by whichever pass asked last; a
# thread-local is read by the pass that set it.
#
# This exists because the device reported the Mac answering one turn
# in three while the Mac's own log showed it answering all three:
#
#   Mac:      stt 0.98s of audio in 0.78s -> 'What time is it?'
#   turn row: engine=whisper-tiny.en   fast=5.66
#
# _better_transcribe records what answered and how long it took onto
# `self`. The speculation finished AFTER the real pass and stamped its
# own name over somebody else's work.
_TL = threading.local()


def _recording():
    """True only for the pass whose answer the turn will actually use."""
    return not getattr(_TL, "speculative", False)

# How much audio ALSA holds for us before it gives up, in microseconds.
# The default is about half a second, and a decode on this board takes
# four — so half a second of a busy machine costs the microphone. See
# the arecord command in open_arecord for the measurement.
#
# IT IS A REQUEST, NOT A REQUIREMENT, AND THE DIFFERENCE COST A DEPLOY.
# Asking for five seconds first produced this, which I read as success:
#
#   reopens: 168 -> 4    overruns: 0
#
# and then, in the next two fields on the same line, the truth:
#
#   rec=0   blocks/sec 0.0
#
# No recorder at all. A USB card will not necessarily install a
# 240,000-frame capture buffer, and arecord does not negotiate — it
# fails to open. Every arecord route failed, the walk fell through to
# the endpoints that deliver nothing, and the reopen counter stopped
# climbing because there was no longer anything to reopen. A zero can
# mean "fixed" or "gone", and I nearly reported the wrong one.
#
# So: the ladder below is tried largest first and the FIRST size the
# card accepts is used; if it refuses all of them the recorder opens
# with ALSA's own default, exactly as it did before any of this. A
# station that hears with a small buffer beats one that does not hear.
CAPTURE_BUFFER_US = int(os.environ.get("DOSE_CAPTURE_BUFFER_US", "5000000"))
# Descending, so the best case is tried first and the last entry is
# "ask for nothing and take whatever the driver gives".
CAPTURE_BUFFER_LADDER = [CAPTURE_BUFFER_US, 2000000, 1000000, 500000, 0]

# AND THE PERIOD, BECAUSE arecord DERIVES IT FROM THE BUFFER.
#
# arecord defaults the period to a QUARTER of the buffer. Asking for
# five seconds therefore asked for 1.25-second periods, and the device
# said what that costs in its own selection trail:
#
#   arecord FORCED card 5,0: opened, peak 0 (rms 0, 0 BLOCKS)
#     — DIGITALLY SILENT, rejected
#
# Zero blocks from a working microphone: the first data was still 1.25 s
# away when the 1.6-second window closed, and the walk moved on to a
# PortAudio endpoint that delivers nothing, where it stayed.
#
# A period is also latency — nothing can be heard until one fills — and
# this station is trying to answer inside two seconds. So the period is
# stated outright, near the engine's own block size, and the buffer is
# free to be large: depth without delay, which is the whole point of
# asking for a buffer.
CAPTURE_PERIOD_US = int(os.environ.get("DOSE_CAPTURE_PERIOD_US", "20000"))

# How long a card's mixer state is trusted before it is forced again.
#
# Unmuting a capture card is a fixed-up-front operation: it spawns one
# `amixer scontrols`, then an `amixer sset` PER CONTROL PER ATTEMPT (four
# attempt shapes each), then an `amixer contents` and a `cset` per
# capture control — dozens of processes for one card. It ran on EVERY
# capture open, for all six card numbers, plus again inside open_arecord
# for each card it tried. Four full sweeps per selection, of work whose
# result cannot have changed since the last one.
#
# Mixer state does not drift on its own. It changes when hardware is
# plugged in or out, or when a person moves a slider. The first is
# already detected — the hot-plug watch clears this cache — and the
# second is covered by re-forcing every few minutes anyway. A user
# action that needs it now (the full mic test, an explicit rescan)
# passes force=True and bypasses the cache entirely.
MIXER_REDO_AFTER = float(os.environ.get("DOSE_MIXER_REDO_AFTER", "300"))

# Same reasoning for `systemctl --user start pipewire …`: a no-op on an
# already-running unit, but still a process spawn that can block, and it
# ran on every capture open. The failure paths pass force=True.
SERVICE_KICK_AFTER = float(
    os.environ.get("DOSE_SERVICE_KICK_AFTER", "120"))

# How long before the default sink/source, its mute state and its volume
# are asserted again. These are SYSTEM settings and a person can change
# them in the desktop mixer, so they cannot be set once and forgotten —
# but they also do not need three process spawns at the start of every
# spoken reply, which is where they were.
DEFAULT_REAPPLY_AFTER = float(
    os.environ.get("DOSE_DEFAULT_REAPPLY_AFTER", "300"))

# Piper-in-a-subprocess. ONNX Runtime aborts this app with SIGABRT from
# native code, which no Python handler can catch, so synthesis runs in a
# child that loads the model once and then waits for work. See
# tools/piper_worker.py.
#
# The timeout is generous: a Pi 4 synthesising a long sentence is slow,
# and cutting off a working synthesiser to fall back to an identical
# in-process one would just add latency. It exists so a HUNG worker
# costs one wait, not a station that never speaks again.
PIPER_WORKER_TIMEOUT = float(
    os.environ.get("DOSE_PIPER_WORKER_TIMEOUT", "25"))
# Floor between respawns, so a model that aborts during load cannot
# make us fork in a tight loop.
PIPER_WORKER_MIN_GAP = float(
    os.environ.get("DOSE_PIPER_WORKER_MIN_GAP", "10"))


def _peak_rms(data):
    """(peak, rms) of signed 16-bit mono PCM. Never raises.

    audioop was REMOVED from the standard library in Python 3.13, which
    is what this station runs. Every measurement site in this file
    guarded its import with a fallback, and the fallbacks were all some
    flavour of "assume it is fine":

        try:    import audioop
        except: return 999        # route_floor: accept ANY route
        except: return True       # capture_is_live: it's live, honest

    A station with no audioop therefore did not select a microphone at
    all. It took the first route that opened, whether or not anything
    was on the other end, and reported a confident 999. That is worse
    than failing, because it looks like it worked.

    There is no need for any of it. Peak and RMS over int16 are four
    lines of `array`, which is stdlib and always present. audioop is
    used when it exists because it is C and faster; the answer is the
    same either way, and this is the only place that has to know.
    """
    if not data:
        return 0, 0
    try:
        import audioop
        return audioop.max(data, 2), audioop.rms(data, 2)
    except Exception:
        pass
    try:
        import array
        a = array.array("h")
        a.frombytes(data[:len(data) - (len(data) % 2)])
        if not a:
            return 0, 0
        peak = max(abs(int(s)) for s in a)
        rms = int((sum(int(s) * int(s) for s in a) / len(a)) ** 0.5)
        return peak, rms
    except Exception:
        return 0, 0


def _parse_mic_pin():
    """DOSE_MIC_CARD="5" or "5,0" — pin capture to ONE ALSA device.

    THIS STATION HAS TWO USB CAPTURE DEVICES. Card 5 is the AIRHUG, the
    actual microphone. Card 4 is a "USB Composite Device" that also
    advertises capture and hears essentially nothing. Device selection
    is otherwise a runtime guess, and it has guessed wrong: an orphaned
    `arecord -D plughw:4,0` was found on the device, left behind by a
    crashed instance, meaning the app had been recording from the wrong
    device entirely.

    For a demo — or for a medication cabinet somebody relies on — a
    microphone that is picked by inference is a microphone that can be
    picked wrongly at the worst moment. Pinning removes the guess.

    Empty (the default) keeps the old auto-selection, so this changes
    nothing for a device that has not set it.
    """
    raw = os.environ.get("DOSE_MIC_CARD", "").strip()
    if not raw:
        return None
    try:
        bits = [b for b in raw.replace(":", ",").split(",") if b != ""]
        card = int(bits[0])
        dev = int(bits[1]) if len(bits) > 1 else 0
        return (card, dev)
    except Exception:
        return None


MIC_CARD_PIN = _parse_mic_pin()
# Where the hardware capture starts before the auto-leveller tunes it.
# Moderate on purpose: high enough to lift a stuck-low USB capsule off
# near-silence, low enough not to slam a hot one into clipping.
DEFAULT_CAPTURE_LEVEL = int(os.environ.get("DOSE_DEFAULT_CAPTURE", "70"))
# the shortest of the graces, used where a single number is needed
ENDPOINT_SILENCE = ENDPOINT_STABLE

# SPECULATIVE RECOGNITION — the trick that buys back most of the wait.
# Recognition normally runs AFTER the turn closes, so its cost lands
# squarely in the pause the user is sitting through. Instead, as soon
# as the input goes quiet for SPECULATE_AFTER we start transcribing
# what we have on a worker, *while* still listening. If the user was
# only drawing breath, more audio arrives and the speculation is
# thrown away (it cost nothing but idle CPU). If they were finished,
# the transcript is usually ready the instant the endpoint fires — so
# recognition takes roughly zero wall-clock time out of the pause.
SPECULATE_AFTER = float(os.environ.get("DOSE_SPECULATE_AFTER", "0.18"))

# Words nobody finishes a sentence on. If the transcript so far ends on
# one of these, the speaker is mid-thought — they are reaching for the
# next word, not done — so we wait longer before closing the turn. This
# is what stops a short endpoint from clipping "how many ... sertraline
# ... do i have left" into "how many".
# Words a sentence genuinely cannot END on: articles, possessives,
# prepositions, conjunctions and fillers. Someone who stops here is
# reaching for the next word, always, whatever the phrase happens to
# match.
HANGING_WORDS = frozenset("""
a an the my your his her its our their this that these those
and or but so if while with for to of in on at from about into than
then because um uh er hmm like just every each another some
i we they he she
""".split())
# Note the subject pronouns at the end: "how many sertraline do I" is
# obviously unfinished, however many words precede it. Object pronouns
# are NOT here — "did I take it", "thank you" are finished sentences.

# Words that only mean "still talking" in a SHORT fragment.
#
# Auxiliaries and question words end finished sentences all the time —
# "how many pills do I have", "yes I did", "which one" — so treating
# them as never-final made real questions wait the full mid-thought
# grace for nothing. But "what" on its own is not a question, it is
# the first word of one, and answering it as "say that again" is
# exactly what turned "what time is it" into "what" on the device.
SHORT_FRAGMENT_WORDS = frozenset("""
what when where which who why how is are was were be been am
do does did have has had can could should would will shall may
might must take taken taking need want get got give show tell
""".split())
SHORT_FRAGMENT_MAX = 2



# ── RASPBERRY PI 4B HARDWARE PROFILE ─────────────────────────────────
# The Pi 4B is a BCM2711: four Cortex-A72 cores at 1.5 GHz sharing a
# 1 MB L2 cache and ~4 GB/s of LPDDR4. That shape decides everything
# about how this assistant should be tuned:
#
#   * Four cores, and the touchscreen UI needs one of them. Giving the
#     model runtimes all four makes inference no faster (they are
#     memory-bandwidth bound long before they are core bound) while
#     making the screen stutter. Three is the sweet spot, and we pin
#     the UI to core 0 so the two never fight over the same core.
#   * The A72 is ARMv8.0 — it has NEON but NOT the dot-product or
#     i8mm instructions of later chips. int8 still wins here, but
#     because it halves the bytes moved, not because of an int8 MAC.
#     So: small models, int8, few threads. Bigger is NOT faster here.
#   * The root filesystem is an SD card. Writing a WAV there costs
#     tens of milliseconds and wears the card out, so every transient
#     audio file goes to /dev/shm (RAM) instead.
#   * The SoC throttles hard at 80 °C, dropping to 1000 MHz — a
#     thermally throttled Pi is ~35% slower at everything, which looks
#     exactly like a software regression. pi_health() surfaces it.
def _pi_cores():
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        return max(1, os.cpu_count() or 1)


CPU_CORES = _pi_cores()
# TWO cores are reserved, not one: the touchscreen UI needs one, and
# the CAMERA needs one. The QR decode runs in its own thread every
# 0.2 s, and if speech recognition takes every remaining core those
# decode passes get starved — which the station used to read as the
# bottle being removed and put back, over and over, while nobody had
# touched it. Speech is allowed to be a little slower; the camera is
# watching someone's medication and must not be.
def _load_now():
    """1-minute load average — how much of the machine is already
    spoken for by everything else."""
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


# Start from the core budget: the touchscreen keeps one, the camera
# keeps one. Then look at what is ACTUALLY running. The station is not
# alone on this Pi — the Tk UI, the QR decode loop, PipeWire, the
# camera stack and whatever the desktop is doing all want time. If the
# machine is already loaded when we start up, taking two more cores
# for speech just makes everything worse, so we take one.
_RESERVED_CORES = 2          # screen + camera
INFER_THREADS = max(1, min(2, CPU_CORES - _RESERVED_CORES))
_STARTUP_LOAD = _load_now()
if _STARTUP_LOAD > CPU_CORES - _RESERVED_CORES:
    INFER_THREADS = 1

# ── SILERO VAD ───────────────────────────────────────────────────────
# A real speech/not-speech model instead of an energy threshold.
#
# Energy cannot tell a voice from a tap running, a fan, or a door —
# it only knows loud from quiet. That is why a running sink defeated
# it: the noise was louder than the gate, so it counted as speech, the
# turn never ended and the recogniser was handed water. Silero is a
# 1.3 MB ONNX model that answers "is this 32 ms of audio a human
# voice?" in well under a millisecond on a Cortex-A72, and it is what
# the modular speech-to-speech pipelines use for exactly this job.
#
# It decides three things here: when a turn starts, when it ends, and
# whether you have started talking over her.
VAD_THRESHOLD = float(os.environ.get("DOSE_VAD_THRESHOLD", "0.5"))
# Frames Silero must call speech before we believe it — one frame is
# 32 ms, so 2 frames is 64 ms. Short enough to feel instant, long
# enough that a cupboard door is not a sentence.
VAD_MIN_SPEECH_FRAMES = int(os.environ.get("DOSE_VAD_MIN_FRAMES", "2"))

# Fetched as a plain file, NOT as the pip package. `pip install
# silero-vad` requires torch and torchaudio: hundreds of megabytes,
# which on a 4 GB Pi failed to install AND drove the load average to
# 5.0 while it tried. All that is actually needed is this one model
# and onnxruntime, which is already present for the speech models.
VAD_MODEL_URL = ("https://raw.githubusercontent.com/snakers4/"
                 "silero-vad/master/src/silero_vad/data/silero_vad.onnx")
VAD_MODEL_MIN_BYTES = 500_000

# ── WAKE WORD: OFF ───────────────────────────────────────────────────
# Listening for "hey dose" means running a speech recogniser on every
# block of audio for as long as the station is switched on — about a
# quarter of a Pi core, permanently. The way into a conversation here
# is tapping the Dose logo, which costs nothing until it is tapped.
# Set DOSE_WAKE_WORD=1 to pay for the wake word if you want it.
WAKE_WORD = os.environ.get("DOSE_WAKE_WORD", "0") not in ("0", "",
                                                          "false")

# ── BARGE-IN ─────────────────────────────────────────────────────────
# Talking over her stops her. People interrupt each other constantly;
# an assistant you have to wait out is the thing that feels like a
# machine. The bar is deliberately higher than for normal listening —
# her own voice is leaking back from a speaker inches away, so this
# must be sure before it cuts her off mid-sentence.
BARGE_IN = os.environ.get("DOSE_BARGE_IN", "1") not in ("0", "false")
BARGE_THRESHOLD = float(os.environ.get("DOSE_BARGE_THRESHOLD", "0.75"))
BARGE_FRAMES = int(os.environ.get("DOSE_BARGE_FRAMES", "4"))  # ~128 ms

# Transcription is a SHORT BURST — a few hundred milliseconds, once per
# thing you say — so it gets EVERY core. The reservation above protects
# the screen and the camera from work that runs continuously; this does
# not run continuously, and it is over before either of them would
# notice. This is the "use the whole Pi" lever, and a burst is the
# right place to pull it.
# One core stays free even for the burst. The device reported a load
# average of 6.66 on four cores — 167% oversubscribed — with speech
# holding every core while the UI, the camera and the model downloads
# all wanted time. Taking the whole machine for a burst is only free
# if nothing else needs it, and on this board something always does.
# CORES MINUS ONE. I took the last core for the recogniser, and the
# device threw the microphone away twice a second until I gave it back.
#
# The soak I justified it with was the wrong experiment. It pinned all
# four cores with external busy loops, and the capture was fine —
# because the scheduler balances an unrelated process against the app's
# threads, and the reader thread still got its turn. Giving the
# RECOGNISER four threads is different: those threads are inside this
# process, they run flat out for seconds during a turn, and the one
# thread that must drain arecord's pipe on time is competing with them.
#
# What the device did, with the reopen trail as evidence:
#
#     11:00:42  recorder ended after 100 blocks (rc=1)
#     11:00:44  recorder ended after 100 blocks (rc=1)
#     11:00:47  recorder ended after 100 blocks (rc=1)
#     ... 115 reopens, climbing about 26 a minute, selection #73
#
# arecord exits on an overrun. An overrun is what happens when nothing
# drains the capture in time. This file has a whole section on that
# already, written the last time it happened.
#
# "The measurement beats the caution" was the right instinct applied to
# the wrong measurement. tiny.en at three threads is 2.27s against
# 2.12s at four — a fifteenth of a second, for which I broke the
# microphone.
#
#     1 thread 4.87s   2 threads 2.84s   3 threads 2.27s   4 threads 2.12s
#
# DOSE_STT_THREADS still takes the last core on a board that can spare
# it. This one cannot.
#
# tiny.en on this board, same recording, three passes each, median:
#
#     1 thread  4.87 s     3 threads  2.27 s
#     2 threads 2.84 s     4 threads  2.12 s
#
STT_THREADS = max(1, int(os.environ.get("DOSE_STT_THREADS",
                                        max(1, CPU_CORES - 1))
                         or max(1, CPU_CORES - 1)))

for _var in ("OMP_NUM_THREADS", "ORT_NUM_THREADS",
             "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, str(INFER_THREADS))

# Transient audio lives in RAM, not on the SD card.
TMP_AUDIO_DIR = "/dev/shm" if os.path.isdir("/dev/shm") \
    and os.access("/dev/shm", os.W_OK) else tempfile.gettempdir()


def is_pi():
    """True on Raspberry Pi hardware."""
    try:
        with open("/proc/device-tree/model") as f:
            return "raspberry pi" in f.read().lower()
    except Exception:
        return False


def pi_health():
    """What the hardware is actually doing right now: clock speed,
    temperature and whether the firmware is throttling us. A throttled
    or under-volted Pi is silently ~35% slower, and that is by far the
    most common cause of 'it got laggy' — so it is worth reporting
    rather than guessing."""
    out = {"cores": CPU_CORES, "infer_threads": INFER_THREADS,
           "governor": "", "mhz": 0, "temp_c": 0.0,
           "throttled": False, "under_voltage": False, "arch64": False,
           "load": 0.0, "load_per_core": 0.0, "mem_free_mb": 0}
    out["load"] = round(_load_now(), 2)
    out["load_per_core"] = round(out["load"] / max(1, CPU_CORES), 2)
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    out["mem_free_mb"] = int(line.split()[1]) // 1024
                    break
    except Exception:
        pass
    try:
        out["arch64"] = os.uname().machine in ("aarch64", "arm64")
    except Exception:
        pass
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/"
                  "scaling_governor") as f:
            out["governor"] = f.read().strip()
    except Exception:
        pass
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/"
                  "scaling_cur_freq") as f:
            out["mhz"] = int(f.read().strip()) // 1000
    except Exception:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            out["temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        pass
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"],
                           capture_output=True, text=True, timeout=3)
        val = int(r.stdout.strip().split("=")[-1], 16)
        out["under_voltage"] = bool(val & 0x1)
        out["throttled"] = bool(val & 0x6)       # freq-capped or throttled
    except Exception:
        pass
    return out


def _pick_governor():
    """Choose a governor that idles COOL and ramps quickly under load.

    We deliberately do NOT use "performance": that pins all four cores
    to 1500 MHz every second of the day, idle or not, which is what was
    cooking the Pi (66-70 C at rest). It is not an overclock — the clock
    never goes above stock — but running flat-out with nothing to do is
    pure heat for no benefit.

    "schedutil" (scheduler-driven) and "ondemand" both sit at the 600
    MHz idle clock and jump to full speed the instant there is work, in
    tens of milliseconds — far below one turn of conversation. Since the
    speech load has already been cut right down, the ramp is invisible
    and the Pi runs many degrees cooler. We prefer them in that order
    and never fall back to performance."""
    prefer = ("schedutil", "ondemand", "conservative")
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/"
                  "scaling_available_governors") as f:
            available = set(f.read().split())
    except Exception:
        available = set()
    for g in prefer:
        if not available or g in available:
            return g
    return "ondemand"


def tune_for_pi():
    """Apply the runtime tuning that needs no root and no reboot.

    Pinning the UI process's MAIN thread across the cores keeps the
    screen responsive while speech runs, and asking for a scaling
    governor that idles at 600 MHz — NOT the always-on performance
    governor — keeps the Pi cool while still ramping to full clock the
    moment there is work to do."""
    applied = []
    if CPU_CORES >= 4:
        try:
            os.sched_setaffinity(0, set(range(CPU_CORES)))
            applied.append("cores=%d" % CPU_CORES)
        except Exception:
            pass
    # The governor file is root-owned, so a plain write fails silently
    # from the app. Try the direct write, then sudo -n (which works
    # when the user has passwordless sudo, as Raspberry Pi OS does by
    # default), and report only what actually took. We pick a governor
    # that scales DOWN when idle so the Pi stays cool — never one that
    # holds full clock forever.
    want = _pick_governor()
    for i in range(CPU_CORES):
        path = ("/sys/devices/system/cpu/cpu%d/cpufreq/"
                "scaling_governor" % i)
        try:
            with open(path, "w") as f:
                f.write(want)
            continue
        except Exception:
            pass
        try:
            subprocess.run(
                ["sudo", "-n", "tee", path], input=want.encode(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5)
        except Exception:
            pass
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/"
                  "scaling_governor") as f:
            gov = f.read().strip()
        if gov == want:
            applied.append("governor=%s (cool idle)" % gov)
        else:
            applied.append("governor=%s (could not change)" % gov)
    except Exception:
        pass
    return applied

# How Vosk tends to mis-hear "hey dose" — accept all of them
WAKE_PATTERNS = [
    "hey dose", "hey dos", "hey doze", "hey those", "hey does",
    "hey doors", "hey rose", "hey goes", "a dose", "hey toes",
    "heyday", "hey dawson", "hades",
]

NUM_WORDS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100,
}


def words_to_number(text):
    """'thirty two' -> 32, '45' -> 45. Returns None if nothing numeric."""
    text = text.strip().lower()
    m = re.search(r"\d+", text)
    if m:
        return int(m.group(0))
    total, current, found = 0, 0, False
    for w in text.replace("-", " ").split():
        if w in NUM_WORDS:
            found = True
            v = NUM_WORDS[w]
            if v == 100:
                current = max(1, current) * 100
            elif v >= 20 and current % 10 == 0:
                current += v
            else:
                current += v
        elif found:
            break
    total += current
    return total if found else None


def parse_spoken_time(text):
    """Extract a clock time from spoken words.
    'seven thirty pm' -> '7:30 PM';  'eight in the evening' -> '8:00 PM'
    'noon' -> '12:00 PM'. Returns None when no time is found.
    Careful with label text: "take one tablet at seven thirty pm" must
    yield 7:30 PM — dose amounts ("one tablet") are never times."""
    t = " " + text.lower().replace(".", " ").replace(":", " ") + " "
    t = t.replace(" p m ", " pm ").replace(" a m ", " am ")
    t = t.replace("o'clock", " oclock ").replace(" oh clock ", " oclock ")

    if "noon" in t or "midday" in t:
        return "12:00 PM"
    if "midnight" in t:
        return "12:00 AM"

    ampm = None
    if " pm " in t or "evening" in t or "night" in t or "afternoon" in t:
        ampm = "PM"
    if " am " in t or "morning" in t:
        ampm = "AM"

    TIME_CTX = {"am", "pm", "oclock", "morning", "evening", "night",
                "afternoon"}
    words = [w for w in re.split(r"[^a-z0-9']+", t) if w]

    def ctx_ok(i):
        """A lone number is a time only with context: preceded by 'at'
        or followed closely by am/pm/o'clock/part-of-day."""
        if i > 0 and words[i - 1] == "at":
            return True
        for j in range(i + 1, min(i + 4, len(words))):
            if words[j] in TIME_CTX or words[j] in ("in", "the"):
                if words[j] in TIME_CTX:
                    return True
                continue
            return False
        return False

    hour = minute = None

    # numeric values per word position (digits or number-words)
    vals = []
    for i, w in enumerate(words):
        if w.isdigit():
            vals.append((i, int(w), len(w)))
        elif w in NUM_WORDS and NUM_WORDS[w] < 100:
            vals.append((i, NUM_WORDS[w], 0))

    # 1. adjacent pair "seven thirty" / "7 30" -> h:mm
    for k in range(len(vals) - 1):
        (i1, v1, _), (i2, v2, _) = vals[k], vals[k + 1]
        if i2 == i1 + 1 and 1 <= v1 <= 12 and 10 <= v2 <= 59:
            # allow "twenty five" style minutes: combine a following ones
            if (k + 2 < len(vals) and vals[k + 2][0] == i2 + 1
                    and v2 % 10 == 0 and vals[k + 2][1] < 10):
                v2 += vals[k + 2][1]
            hour, minute = v1, v2
            break

    # 2. "half past eight"
    if hour is None and "half" in words and "past" in words:
        for i, v, _ in vals:
            if 1 <= v <= 12:
                hour, minute = v, 30
                break

    # 3. compact digits "730" -> 7:30
    if hour is None:
        for i, v, ndig in vals:
            if ndig >= 3 and 100 <= v <= 1259:
                h, mm = v // 100, v % 100
                if 1 <= h <= 12 and mm <= 59:
                    hour, minute = h, mm
                    break

    # 4. lone number, only with time context
    if hour is None:
        for i, v, _ in vals:
            if 1 <= v <= 23 and ctx_ok(i):
                hour, minute = v, 0
                break

    if hour is None or not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    if hour > 12:
        ampm = "PM"
        hour -= 12
    if hour == 0:
        hour, ampm = 12, "AM"
    if ampm is None:
        # sensible default: 1-6 assumed evening, otherwise morning
        ampm = "PM" if 1 <= hour <= 6 else "AM"
    return f"{hour}:{minute:02d} {ampm}"


_TIME_RX = re.compile(r"\b(\d{1,2}):([0-5]\d)\s?([AP]M)\b")


def to_speech(text):
    """Rewrite a reply for the SYNTHESIZER only.

    Replies are written the way they should be READ — "1:15 PM" — and
    that is exactly what goes on screen. They used to be built with
    the time already verbalized, so the screen showed "one 15 PM" as
    well, which is not how anyone writes a time. The conversion now
    happens here, on the way into the voice, and the text the user
    sees is left alone."""
    if not text:
        return text
    return _TIME_RX.sub(
        lambda m: time_to_speech("%s:%s %s" % m.groups()), text)


def time_to_speech(ts):
    """'7:30 PM' -> 'seven thirty PM' style text Piper says naturally."""
    try:
        h, rest = ts.split(":")
        m, ap = rest.split(" ")
        h, m = int(h), int(m)
    except Exception:
        return ts
    ones = ["zero", "one", "two", "three", "four", "five", "six",
            "seven", "eight", "nine", "ten", "eleven", "twelve",
            "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
            "eighteen", "nineteen"]
    tens = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty"}

    def words(n):
        if n < 20:
            return ones[n]
        t, r = divmod(n, 10)
        return tens[t * 10] + (" " + ones[r] if r else "")

    hour_w = ones[h] if h <= 12 else str(h)
    if m == 0:
        return f"{hour_w} {ap}"
    if m < 10:
        return f"{hour_w} oh {ones[m]} {ap}"
    return f"{hour_w} {words(m)} {ap}"


class DoseVoice:
    """The assistant. Owns the microphone thread; talks to the app only
    through thread-safe bridges."""

    # CLASS-LEVEL DEFAULTS FOR EVERYTHING THE AUDIO CALLBACK TOUCHES.
    #
    # The PortAudio callback runs on PortAudio's own thread and can fire
    # at any moment — including while __init__ is still running, and
    # during teardown when instance attributes are going away. The device
    # log showed exactly that:
    #
    #   File ".../dose_voice.py", line 3795, in ingest
    #       if self._muted:
    #   AttributeError: 'DoseVoice' object has no attribute '_muted'
    #
    # cffi swallows exceptions raised inside a callback ("Exception
    # ignored from cffi callback"), so this is invisible at runtime and
    # simply drops the audio block. Silently losing capture blocks in a
    # voice assistant is the worst kind of bug: it degrades recognition
    # without ever announcing itself.
    #
    # __init__ already sets these early and deliberately. These
    # class-level values are the belt to that braces: an instance that is
    # half-built, or half-torn-down, reads a sane default instead of
    # raising. They are never mutated on the class — every write path
    # assigns to the instance.
    _muted = False
    state = "idle"
    _native_rate = SAMPLE_RATE
    _ratecv_state = None
    # Set by the capture reader when its recorder dies, read by the
    # supervising loop to reopen the microphone. Defaulted here because
    # the reader thread can outlive the object it was started from.
    _capture_lost = False
    _force_reopen = False
    # Devices whose stream refused to close, and how many times device
    # selection ran out of its budget. Both are FAULTS, not trivia: a
    # wedged close is the exact failure that made this station deaf, and
    # if it starts happening again the mic report has to say so instead
    # of leaving the next person to guess. Class-level so the probe can
    # never AttributeError on a half-built engine.
    _probe_wedged = []
    _probe_timeouts = 0
    _select_timeouts = 0
    # Silero's input names, and the session they were read from. Cached
    # because asking cost an ONNX Runtime round trip on every 32 ms
    # frame; keyed on the session so a swapped model re-reads them.
    _vad_input_names = None
    _vad_names_for = None
    # ONE synthesis at a time, and ONE worker.
    #
    # MEASURED ON THE DEVICE, first hardware soak of the Piper worker:
    # `pgrep -fc piper_worker.py` returned 2, CPU sat at 267-270% (the
    # baseline is 111%) and the board ran 59.9-63.3 C instead of 45-50.
    # Two ONNX sessions, because prewarm_replies() runs on its own
    # thread while the speech path can synthesise at the same time, and
    # both threads found _pw_proc None and both spawned.
    #
    # The wasted session was the visible half. The dangerous half is
    # that _synth_via_worker() writes a request to one pipe and reads
    # one line back: two threads doing that concurrently can each read
    # the OTHER's reply, so sentence A is told "ok" about sentence B's
    # file. That is silent, and it would have been very hard to find
    # later.
    #
    # The lock therefore covers spawn AND the whole request/response,
    # not just the spawn. Serialising synthesis costs nothing real —
    # Piper is CPU-bound and two at once on a Pi 4 only thrash.
    _pw_lock = threading.Lock()

    def __init__(self, app):
        self.app = app
        self.available = False
        self.reason = ""
        # SAFE DEFAULTS FIRST — before any of the early returns below.
        # __init__ bails out early on a missing library or a
        # not-yet-downloaded model, but the app still starts background
        # self-heal threads (model download, audit) that read these.
        # When they were left unset, those threads crashed with
        # AttributeError on a fresh device — the voice/STT could then
        # never arrive. Every attribute other code may read at any time
        # gets a safe default here so a half-initialised engine is inert,
        # not a crash.
        self._piper_path = None
        self._vosk_dir = None
        self._ms_arch_used = ""
        self._ms_reason = ""
        self._ms_v2 = "unset"
        self._fast_choice = "whisper"
        self._whisper = None
        self._whisper_loaded = False
        self._whisper_size = (WHISPER_MODELS[0] if WHISPER_MODELS else "")
        self._whisper_fast = None
        self._whisper_fast_loaded = False
        self._whisper_fast_size = FAST_WHISPER_MODEL
        self._moonshine = None
        self._warmed = False
        self.state = "idle"          # idle | listening | thinking | speaking
        self._flow = None            # active multi-turn conversation
        self._stop = threading.Event()
        # Bounded. Roughly 30 s of audio at 64 ms a block. If the
        # consumer ever stalls, the OLDEST audio is dropped rather
        # than memory growing without limit — on a 4 GB board that
        # matters, and stale audio is worthless anyway.
        self._audio_q = queue.Queue(maxsize=480)
        self._muted = False
        self._last_reply = ""
        self._last_exchange = None   # {"text","intent","arg"} of last turn
        self._learn = self._learn_load()

        self._vosk_model = None
        self._piper_voice = None
        self._sd = None
        self._moonshine = None       # optional stronger command STT
        self.mic_index = None
        self.mic_name = "default"
        self.mic_card = None
        self.mic_rms = 0
        self._ack_files = []
        # set when the user ends the conversation (tap outside the
        # panel, or "I'm done talking")
        self._closed = threading.Event()
        self._level_probe = None
        self._force_reopen = False
        # Own list, not the shared class-level one.
        self._probe_wedged = []
        self._probe_timeouts = 0
        self._select_timeouts = 0
        self._ptt_requested = False   # push-to-talk (hold Dose logo)
        self._pause_capture = False   # full self-test holds the devices
        self._paused_ack = False      # capture loop released the device
        # A PIN BEATS A GUESS. DOSE_MIC_CARD names the real
        # microphone so selection never has to infer it; None
        # keeps the old auto-selection. See _parse_mic_pin().
        self._forced_card = MIC_CARD_PIN   # (card, device) or None
        self._forced_sink = None      # user-picked speaker output
        self._probe()
        self._probe_moonshine()

    # ── availability ──────────────────────────────────────────────────
    def _probe(self):
        try:
            import sounddevice as sd
            self._sd = sd
        except Exception:
            self.reason = "audio library not installed"
            return
        try:
            from vosk import Model, SetLogLevel  # noqa
        except Exception:
            self.reason = "vosk not installed"
            return
        try:
            from piper import PiperVoice  # noqa
        except Exception:
            self.reason = "piper not installed"
            return

        vosk_dirs = sorted(glob.glob(os.path.join(VOICE_DIR, "vosk-model*")))
        vosk_dirs = [d for d in vosk_dirs if os.path.isdir(d)]
        if not vosk_dirs:
            self.reason = "speech model missing"
            return
        self._vosk_dir = vosk_dirs[0]

        onnx = sorted(glob.glob(os.path.join(VOICE_DIR, "*.onnx")))
        if not onnx:
            self.reason = "voice model missing"
            return
        # HER voice, or none. hfc_female synthesizes at roughly RTF
        # 0.15 on a Pi 4 (~3x faster than real time), so a sentence is
        # ready in well under a second. If some other .onnx is sitting
        # in the folder we do NOT quietly use it — a station that
        # answers in a different voice than the one it was built with
        # is a bug, not a fallback. We say the voice is missing and
        # fetch the right one.
        want = [p for p in onnx
                if VOICE_NAME in os.path.basename(p)]
        if not want:
            self.reason = "voice model missing"
            return
        self._piper_path = want[0]

        # A microphone counts if ANY layer can see one: PortAudio,
        # the PipeWire/Pulse source list, or the kernel's own card
        # list. (PortAudio alone is not enough — when PipeWire owns
        # the hardware, PortAudio can show nothing while pw-record
        # captures perfectly.)
        has_mic = False
        try:
            dev = self._sd.query_devices(kind="input")
            has_mic = bool(dev) and dev.get("max_input_channels",
                                            0) >= 1
        except Exception:
            pass
        if not has_mic:
            try:
                has_mic = bool(self._list_sources())
            except Exception:
                pass
        if not has_mic:
            try:
                with open("/proc/asound/cards") as f:
                    has_mic = "[" in f.read()
            except Exception:
                pass
        if not has_mic:
            self.reason = "no microphone detected"
            return

        self.available = True
        self.reason = "ready"

    # ── Moonshine v2 (moonshine-voice): on-device recognizer with
    #    KEY-TERM BIASING. Passing this device's medication names as
    #    key terms is the measured fix for drug-name mishears
    #    ("liz and opera" -> Lisinopril). Loaded lazily; if the model
    #    can't be fetched we silently fall back to Whisper/Vosk.
    # Moonshine BASE is the default, not tiny. Recognition now runs
    # DURING the end-of-speech pause rather than after it (see the
    # speculation in _listen_command), so base's extra accuracy costs
    # almost nothing in wall-clock time — it finishes inside a pause
    # the user is taking anyway. Tiny stays available for a slower
    # board via DOSE_STT_ARCH.
    _MS_ARCHS = {"tiny": "TINY_STREAMING", "base": "BASE_STREAMING",
                 "small": "SMALL_STREAMING", "medium": "MEDIUM_STREAMING"}
    # "" means: take the most accurate model this package offers.
    # Set DOSE_STT_ARCH to tiny/base/small/medium to pin one.
    STT_ARCH = os.environ.get("DOSE_STT_ARCH", "")

    def _moonshine_v2(self):
        """Load the recogniser, and say WHY if it won't load.

        This used to ask for one exact arch name and swallow every
        error, so a package whose build doesn't define that name — or
        a download that never completed — looked identical to a
        download still in progress. The station sat on "downloading…"
        forever with nothing to go on. Now it tries the arch we want,
        falls back through the ones the installed package actually
        offers, and records the reason it failed."""
        if getattr(self, "_ms_v2", "unset") != "unset":
            return self._ms_v2
        self._ms_v2 = None
        self._ms_reason = ""
        self._ms_arch_used = ""
        try:
            import moonshine_voice as mv
        except Exception as e:
            self._ms_reason = "library not installed (%s)" % e
            return None

        # THIS IS THE FAST MODEL. Speed is its entire job.
        #
        # The device reported "moonshine medium" taking 4.11 SECONDS
        # on a single "thanks". I had ordered these biggest-first,
        # reasoning that accuracy was what was scarce — and that
        # reasoning was right for the ESCALATION model and completely
        # wrong here. This one answers every sentence; if it is slow,
        # everything is slow. Whisper is the accuracy path and it runs
        # only when this one comes back with nothing usable.
        #
        # base is the largest that stays quick on a Pi 4. small and
        # medium are reachable only by asking for them explicitly with
        # DOSE_STT_ARCH, never by falling back into them.
        ACCURACY_ORDER = ("BASE_STREAMING", "TINY_STREAMING")
        wanted = self._MS_ARCHS.get(self.STT_ARCH, "")
        order = ([wanted] if wanted else []) + [
            a for a in ACCURACY_ORDER if a != wanted]
        available = [a for a in order if hasattr(mv.ModelArch, a)]
        if not available:
            available = [a for a in dir(mv.ModelArch)
                         if a.isupper() and not a.startswith("_")]
        if not available:
            self._ms_reason = "no speech model types in this package"
            return None

        last = ""
        for arch_name in available:
            try:
                path, arch = mv.get_model_for_language(
                    "en", getattr(mv.ModelArch, arch_name))
                boost = float(os.environ.get("KEYTERM_BOOST", "5"))
                # 7+ measured to hallucinate
                boost = max(1.0, min(boost, 6.0))
                tr = mv.Transcriber(
                    model_path=path, model_arch=arch,
                    update_interval=float(
                        os.environ.get("STT_UPDATE_INTERVAL", "0.25")),
                    options={"keyterm_boost": boost,
                             "vad_window_duration": float(
                                 os.environ.get("STT_VAD_WINDOW",
                                                "0.15"))})
                self._ms_v2 = tr
                self._ms_arch_used = arch_name
                if wanted and arch_name != wanted:
                    self._ms_reason = "using %s (%s unavailable)" % (
                        arch_name.split("_")[0].lower(),
                        wanted.split("_")[0].lower())
                elif arch_name == "TINY_STREAMING":
                    # never let this be invisible again
                    self._ms_reason = "tiny — weak, Whisper checks it"
                    # Tiny mishears often enough that its answers are
                    # provisional: the stronger model gets a turn on
                    # anything that is not a confident parse, which is
                    # what _usable already decides.
                    self._fast_is_weak = True
                elif arch_name in ("MEDIUM_STREAMING",
                                   "SMALL_STREAMING"):
                    self._ms_reason = "%s — SLOW, set DOSE_STT_ARCH=base" \
                        % arch_name.split("_")[0].lower()
                return tr
            except Exception as e:
                last = "%s: %s" % (arch_name.split("_")[0].lower(),
                                   str(e)[:60])
                continue
        self._ms_reason = last or "download did not complete"
        return None

    def _moonshine_transcribe(self, audio_bytes):
        """Transcribe one captured utterance with this device's drug
        names as key terms. Returns cleaned text, or '' on any problem."""
        tr = self._moonshine_v2()
        if tr is None or not audio_bytes:
            return ""
        try:
            import array
            names = self._med_names()
            try:
                tr.set_keyterms([n.replace(",", " ") for n in names]
                                or None)
            except Exception:
                pass
            raw = bytes(audio_bytes)[:len(audio_bytes) // 2 * 2]
            try:
                # vectorised: a 5 s utterance is ~80k samples, and the
                # pure-Python loop below costs real milliseconds on a Pi
                import numpy as _np
                audio = (_np.frombuffer(raw, dtype=_np.int16)
                         .astype(_np.float32) / 32768.0)
            except Exception:
                a = array.array("h")
                a.frombytes(raw)
                audio = [x / 32768.0 for x in a]
            res = tr.transcribe_without_streaming(audio, SAMPLE_RATE)
            lines = getattr(res, "lines", None)
            if lines is None:
                lines = res if isinstance(res, (list, tuple)) else []
            parts = []
            for ln in lines:
                txt = getattr(ln, "text", None)
                if txt is None:
                    words = getattr(ln, "words", None) or []
                    txt = " ".join(getattr(w, "text", str(w))
                                   for w in words)
                if txt:
                    parts.append(txt)
            return self._clean_text(" ".join(parts))
        except Exception:
            return ""

    # ── the voice detector ───────────────────────────────────────────
    def _load_vad(self):
        """Load Silero once. Returns the session, or None."""
        if getattr(self, "_vad", "unset") != "unset":
            return self._vad
        self._vad = None
        self._vad_reason = ""
        try:
            import numpy as np          # noqa: F401  (needed below)
            import onnxruntime as ort
        except Exception as e:
            self._vad_reason = "onnxruntime missing (%s)" % str(e)[:40]
            return None
        # Our own copy first — the normal case. The pip package is
        # only used if it happens to be present already; we never ask
        # for it, because it would bring torch with it.
        path = None
        cand = os.path.join(VOICE_DIR, "silero_vad.onnx")
        if os.path.exists(cand) and os.path.getsize(cand) > \
                VAD_MODEL_MIN_BYTES:
            path = cand
        if path is None:
            try:
                import silero_vad
                base = os.path.dirname(silero_vad.__file__)
                for name in ("silero_vad_16k_op15.onnx",
                             "silero_vad.onnx"):
                    c2 = os.path.join(base, "data", name)
                    if os.path.exists(c2):
                        path = c2
                        break
            except Exception:
                pass
        if path is None:
            self._vad_reason = "downloading…"
            self.fetch_vad_model()
            return None
        try:
            opts = ort.SessionOptions()
            # one thread: it runs on the audio thread, every 32 ms, and
            # must never contend with transcription or the camera
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.log_severity_level = 4
            self._vad = ort.InferenceSession(
                path, sess_options=opts,
                providers=["CPUExecutionProvider"])
            self._vad_state = None
            self._vad_path = path
        except Exception as e:
            self._vad = None
            self._vad_reason = str(e)[:60]
        return self._vad

    def fetch_vad_model(self):
        """Download the 2.3 MB voice detector, once, in the background.

        A plain file fetch — no package, no torch, no build step."""
        if getattr(self, "_vad_fetching", False):
            return
        self._vad_fetching = True

        def work():
            try:
                os.nice(15)
            except Exception:
                pass
            dest = os.path.join(VOICE_DIR, "silero_vad.onnx")
            tmp = dest + ".part"
            try:
                os.makedirs(VOICE_DIR, exist_ok=True)
                import urllib.request
                with urllib.request.urlopen(VAD_MODEL_URL,
                                            timeout=120) as r:
                    data = r.read()
                if len(data) < VAD_MODEL_MIN_BYTES:
                    raise ValueError("short download (%d bytes)"
                                     % len(data))
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dest)
                self._vad = "unset"          # pick it up next time
                self._vad_reason = ""
            except Exception as e:
                self._vad_reason = "download failed: %s" % str(e)[:40]
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
            finally:
                self._vad_fetching = False

        threading.Thread(target=work, daemon=True,
                         name="vad-fetch").start()

    VAD_FRAME = 512          # samples at 16 kHz = 32 ms

    def vad_speech_prob(self, pcm_bytes):
        """Probability that this frame is a human voice, 0..1.
        Returns None when Silero isn't available, so callers fall back
        to the energy gate rather than going deaf."""
        sess = self._load_vad()
        if sess is None:
            return None
        try:
            import numpy as np
            a = np.frombuffer(pcm_bytes, dtype=np.int16)
            if a.size < self.VAD_FRAME:
                return None
            a = a[:self.VAD_FRAME].astype(np.float32) / 32768.0
            if self._vad_state is None:
                self._vad_state = np.zeros((2, 1, 128), dtype=np.float32)
            # ASK THE MODEL ITS INPUT NAMES ONCE, NOT 31 TIMES A SECOND.
            #
            # This was `names = {i.name for i in sess.get_inputs()}`,
            # evaluated on EVERY VAD frame. A frame is 512 samples at
            # 16 kHz — 32 ms — so while anybody is speaking this crossed
            # into the ONNX Runtime C API, allocated a NodeArg per
            # input and built a fresh set roughly thirty-one times a
            # second, forever, to answer a question that is fixed for
            # the life of a loaded session.
            #
            # Cached against the session object itself, so swapping the
            # model (a re-download, a different Silero build with
            # different input names) re-reads it rather than feeding the
            # new session the old session's names.
            names = getattr(self, "_vad_input_names", None)
            if names is None or self._vad_names_for is not sess:
                names = {i.name for i in sess.get_inputs()}
                self._vad_input_names = names
                self._vad_names_for = sess
            feed = {"input": a.reshape(1, -1)}
            if "sr" in names:
                feed["sr"] = np.array(SAMPLE_RATE, dtype=np.int64)
            if "state" in names:
                feed["state"] = self._vad_state
            out = sess.run(None, feed)
            if len(out) > 1 and getattr(out[1], "shape", None) == (2, 1, 128):
                self._vad_state = out[1]
            return float(np.ravel(out[0])[0])
        except Exception:
            self._vad = None          # stop trying on a broken model
            return None

    def reset_vad(self):
        self._vad_state = None

    def is_speech(self, frame, energetic):
        """Is this frame a human voice?

        Energy says "something is here". Silero says "that something
        is a person". Both have to agree — which is what a running tap
        cannot do, because it is loud but it is not a voice.

        FAILS OPEN, deliberately. If Silero is unavailable, broken, or
        disagrees with clear audio often enough to look wrong, its vote
        is dropped and energy decides alone. A voice detector that
        silently vetoes everything would make the station deaf, and a
        deaf medication device is far worse than a slightly noisy one.
        """
        if not energetic:
            return False
        if not self._vad_trusted:
            return True
        # Only while a conversation is actually happening. Idle, the
        # energy gate is enough to learn the room and drive the meter
        # — running a neural detector on every loud block while nobody
        # is talking to the station means a television keeps it busy
        # all evening for nothing.
        # Default to DOING the work if the state is somehow unknown:
        # skipping the detector is the risky direction, not running it.
        if getattr(self, "state", "listening") == "idle" \
                and not WAKE_WORD:
            return True
        p = self.vad_speech_prob(frame)
        if p is None:
            return True                      # no detector: energy alone
        self._vad_seen += 1
        speech = p >= VAD_THRESHOLD
        if speech:
            self._vad_agreed += 1
        # After a few hundred frames of audio that energy called
        # signal, a working detector will have agreed with some of it.
        # If it has agreed with almost none, it is not doing the job we
        # think it is — stop listening to it rather than go deaf.
        if self._vad_seen >= 300:
            if self._vad_agreed < self._vad_seen * 0.02:
                self._vad_trusted = False
                self._vad_reason = "disagreed with clear audio — ignored"
            self._vad_seen = self._vad_agreed = 0
        return speech

    def _detect_barge_in(self, data):
        """Have they started talking while she is still speaking?

        Requires SUSTAINED voice — a couple of consecutive frames
        Silero calls speech — so a cough, a door, or her own voice
        leaking back through the speaker does not cut her off. Once it
        fires, playback stops immediately and the turn becomes theirs.
        Interrupting an assistant is how people actually talk; waiting
        politely for it to finish a sentence you no longer want is
        the thing that makes one feel like a machine."""
        if not BARGE_IN or not self._vad_trusted:
            return
        try:
            import audioop
            if audioop.rms(data, 2) < max(120.0, self._nfloor * 2.5):
                self._barge_frames = 0
                return
        except Exception:
            return
        p = self.vad_speech_prob(data)
        if p is None:
            return
        if p >= BARGE_THRESHOLD:
            self._barge_frames = getattr(self, "_barge_frames", 0) + 1
            if self._barge_frames >= BARGE_FRAMES:
                self._barge = True
                self._stop_playback()
        else:
            self._barge_frames = 0

    def _stop_playback(self):
        """Cut the audio that is playing, now.

        Reads the handle ONCE. It is set on the speaking thread and
        cleared there too, so re-reading it could terminate a clip
        that started after the interruption was decided."""
        proc = getattr(self, "_play_proc", None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        # Deliberately NOT waiting here. Barge-in must return instantly —
        # this runs the moment someone starts talking over her, and a
        # blocking wait would put that latency straight into the
        # interruption. The speaking thread is already sitting in
        # proc.wait(), and that is what reaps it; a poll() here is enough
        # to collect the child in the case where that thread has already
        # moved on.
        try:
            proc.poll()
        except Exception:
            pass

    def _probe_moonshine(self):
        """Moonshine for speed, Whisper for when speed was not enough.

        I removed faster-whisper earlier to win latency, and that was
        right at the time. It is wrong now: when the station cannot
        make out what you said, the answer is not to give up faster.
        Whisper small.en is a materially stronger model than whatever
        Moonshine arch a given package ships, and — crucially — it only
        runs when the first attempt produced something unusable. The
        common case stays fast; the failures get the better model.
        Both are MIT and run entirely on-device."""
        # NOT loaded here. Whisper only runs when the fast recogniser
        # cannot make out what was said, which is a minority of turns —
        # and holding a loaded model in memory costs RAM and startup
        # time on every single boot for something that may never be
        # used. It loads on first need instead. _whisper_size still
        # reports what WOULD load, so the audit page is honest about
        # it before it has been needed.
        self._whisper = None
        self._whisper_loaded = False
        self._whisper_size = WHISPER_MODELS[0] if WHISPER_MODELS else ""
        try:
            import moonshine_onnx
            self._moonshine = moonshine_onnx
        except Exception:
            self._moonshine = None

    def _usable(self, text):
        """Did that transcript actually mean anything here?

        A recogniser handed poor audio does not return nothing — it
        returns confident nonsense. "(urk)" for "storage" is the shape
        of it. So a transcript only counts as usable if it parses to a
        real intent, names a medication, or is one of the short
        answers a conversation depends on. Anything else is worth a
        second opinion from a stronger model."""
        t = (text or "").strip()
        if len(t) < 2:
            return False
        try:
            if _nlu_mod is not None and _nlu_mod.looks_hallucinated(t):
                return False
            # Ask the SAME matcher that would answer it. Navigation,
            # the time, the personality lines and the add-medication
            # flow all live here rather than in the pattern set, so
            # checking only the pattern set would send a perfectly good
            # "open storage" off for a second opinion it doesn't need.
            norm = " " + re.sub(r"[^a-z0-9' ]", " ",
                                t.lower()).strip() + " "
            norm = re.sub(r"\s+", " ", norm)
            hit = self._match_builtin(norm)
            if hit and hit[0]:
                return True
        except Exception:
            pass
        if _nlu_mod is None:
            return True
        try:
            if _nlu_mod.is_yes(t) or _nlu_mod.is_no(t):
                return True
            intent = _nlu_mod.parse(t, self._med_names())
            return intent.name != "unknown" or bool(
                intent.med or intent.suggestion)
        except Exception:
            return True

    def _load_whisper(self):
        """Bring up the escalation model, once, on first need."""
        if self._whisper_loaded:
            return self._whisper
        self._whisper_loaded = True
        try:
            from faster_whisper import WhisperModel
        except Exception:
            self._whisper_size = "not installed"
            return None
        for size in WHISPER_MODELS:
            try:
                self._whisper = WhisperModel(
                    size, device="cpu", compute_type="int8",
                    cpu_threads=STT_THREADS, num_workers=1)
                self._whisper_size = size
                return self._whisper
            except Exception:
                self._whisper = None
        self._whisper_size = "unavailable"
        return None

    def _load_whisper_fast(self):
        """The FAST Whisper contender — tiny.en, int8, greedy. This is
        the one that races moonshine at startup and, on a Pi 4, usually
        wins by a mile: CTranslate2's int8 kernels are far quicker on
        these A72 cores than moonshine's ONNX path turned out to be."""
        if getattr(self, "_whisper_fast_loaded", False):
            return self._whisper_fast
        self._whisper_fast_loaded = True
        self._whisper_fast = None
        self._whisper_fast_size = FAST_WHISPER_MODEL
        try:
            from faster_whisper import WhisperModel
        except Exception:
            self._whisper_fast_size = "not installed"
            return None
        try:
            self._whisper_fast = WhisperModel(
                FAST_WHISPER_MODEL, device="cpu", compute_type="int8",
                cpu_threads=STT_THREADS, num_workers=1)
        except Exception:
            self._whisper_fast = None
            self._whisper_fast_size = "unavailable"
        return self._whisper_fast

    def _fw_transcribe(self, model, audio_bytes, vad=True, beam_size=1):
        """Run one faster-whisper model over an utterance.

        beam_size=1 is greedy — a beam search is several times slower
        for a fraction of a percent of word error on short commands,
        and on a Pi that trade is not close. vad can be turned off for
        the startup race so the timing measures full compute rather
        than being flattered by silence-skipping. Also records the
        model's own confidence (avg_logprob) in self._fw_conf, so a
        confident-but-wrong reading can still be sent for a second
        opinion."""
        self._fw_conf = 0.0
        if not audio_bytes or model is None:
            return ""
        path = None
        # WHERE THE SECONDS ACTUALLY GO.
        #
        # turns.jsonl recorded "fast": 31.79 on 3.3 seconds of audio,
        # while the same model on the same board, measured standalone,
        # takes 2.1 s. Thirty seconds is not compute — it is waiting
        # for something — and a single total tells you nothing about
        # which something. Each stage is timed separately and carried
        # into the turn row: the wav write, the prompt (which reads the
        # medication list), the decode itself, and collecting the
        # segments (faster-whisper's generator is LAZY, so list() is
        # where the inference really happens).
        t_all = time.time()
        try:
            t0 = time.time()
            path = self._write_wav(audio_bytes)
            self._t_fw_wav = time.time() - t0
            t0 = time.time()
            prompt = self._whisper_prompt()
            self._t_fw_prompt = time.time() - t0
            t0 = time.time()
            segs, _info = model.transcribe(
                path, language="en", beam_size=beam_size,
                vad_filter=vad, condition_on_previous_text=False,
                initial_prompt=prompt)
            self._t_fw_call = time.time() - t0
            t0 = time.time()
            segs = list(segs)
            self._t_fw_decode = time.time() - t0
            self._t_fw_total = time.time() - t_all
            self._t_fw_audio = round(
                len(audio_bytes) / 2.0 / float(SAMPLE_RATE), 2)
            # worst (lowest) segment confidence — one weak segment is
            # enough to want the stronger model to check it
            lps = [getattr(s, "avg_logprob", 0.0) for s in segs]
            self._fw_conf = min(lps) if lps else 0.0
            return self._clean_text(" ".join(s.text for s in segs))
        except Exception:
            return ""
        finally:
            if path:
                try:
                    os.unlink(path)
                except Exception:
                    pass

    def _whisper_transcribe(self, audio_bytes):
        """The stronger ESCALATION model (base.en), for when the fast
        recogniser came back with something that meant nothing."""
        return self._fw_transcribe(self._load_whisper(), audio_bytes)

    def _whisper_prompt(self):
        """Tell Whisper what this device is about. Naming the actual
        medications and the words the station listens for biases it
        the same way Moonshine's key terms do, which is most of the
        difference on drug names."""
        try:
            meds = ", ".join(self._med_names()[:12])
        except Exception:
            meds = ""
        base = ("Medication reminder device. Commands: what time is it, "
                "what do I take today, how many pills do I have left, "
                "did I take my medicine, open storage, open settings, "
                "go to user, next dose.")
        return (base + " Medications: " + meds) if meds else base

    def _wav_bytes(self, audio_bytes):
        """The same WAV _write_wav makes, in memory.

        The Mac is handed bytes, not a path, so nothing about this
        device's filesystem is involved in talking to it."""
        import io as _io
        buf = _io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(bytes(audio_bytes))
        return buf.getvalue()

    def _write_wav(self, audio_bytes):
        # RAM, not the SD card: on a Pi this saves tens of ms per
        # utterance and stops us wearing the card out.
        fd, path = tempfile.mkstemp(suffix=".wav", dir=TMP_AUDIO_DIR)
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(bytes(audio_bytes))
        return path

    @staticmethod
    def _clean_text(text):
        text = (text or "").strip().lower()
        text = re.sub(r"[^a-z0-9' ]", " ", text)
        return " ".join(text.split())

    def _trim_silence(self, audio_bytes, keep_ms=140):
        """Cut dead air off the front and back of an utterance.

        Transcription time scales with audio length, so a buffer that
        carries a second of silence before "what" makes the recogniser
        do a second of pointless work. Trimming to the voiced part is
        the cheapest speed-up there is and it helps whichever engine
        runs. Energy-gated at 20 ms resolution, with a small margin so
        no word gets clipped."""
        try:
            import numpy as np
            raw = bytes(audio_bytes)[:len(audio_bytes) // 2 * 2]
            a = np.frombuffer(raw, dtype=np.int16)
            win = max(1, int(SAMPLE_RATE * 0.02))
            n = a.size // win
            if n < 4:
                return audio_bytes
            frames = a[:n * win].reshape(n, win).astype(np.float32)
            energy = np.sqrt((frames ** 2).mean(axis=1))
            peak = float(energy.max())
            if peak <= 0:
                return audio_bytes
            thr = max(peak * 0.08, 150.0)
            voiced = np.where(energy > thr)[0]
            if voiced.size == 0:
                return audio_bytes
            pad = max(1, int(keep_ms / 20))
            lo = max(0, int(voiced[0]) - pad)
            hi = min(n, int(voiced[-1]) + 1 + pad)
            trimmed = a[lo * win: hi * win].tobytes()
            return trimmed or audio_bytes
        except Exception:
            return audio_bytes

    def _fast_engine(self):
        """Which recogniser leads on THIS board. Pinned by env, else
        whatever won the startup race, else Whisper (never the
        proven-slow moonshine by default)."""
        if FAST_ENGINE_PIN in ("moonshine", "whisper"):
            return FAST_ENGINE_PIN
        return getattr(self, "_fast_choice", "") or "whisper"

    # Below this average log-probability the fast model's reading is
    # shaky enough that the stronger model should check it, EVEN if it
    # happens to parse — this is the "confidently wrong" case that a
    # parse-only test lets through. moonshine gives no confidence, so
    # it is always treated as low (0.0 sentinel handled by the caller).
    FAST_CONF_FLOOR = float(os.environ.get("DOSE_FAST_CONF_FLOOR",
                                           "-0.85"))

    def _fast_transcribe(self, audio_bytes, budget=None):
        """Run the fast recogniser chosen for this board, UNDER A WALL
        CLOCK. Returns (text, engine_tag). Sets self._fw_conf as a side
        effect (0.0 when the engine reports no confidence).

        `budget` is what remains of STT_TURN_BUDGET when the caller
        got here — because a remote attempt may already have spent
        most of it, and two separately bounded steps in a row are not
        a bounded turn. It is floored at STT_LOCAL_FLOOR (a pass with
        a second and a half is worth starting; one with 0.2 s is not)
        and capped at STT_LOCAL_CEILING.

        NOTHING IN A TURN MAY RUN UNBOUNDED. The device recorded this:

            reply                          stt      total   engine
            I didn't catch that, Ryan...  105.14   106.32   whisper-tiny.en

        A hundred and five seconds for a local pass on at most twelve
        seconds of audio. The station answered sensibly and answered it
        into an empty room — the person had been gone for a minute and
        a half.

        STT_TURN_BUDGET existed and did not help: it gates the base.en
        ESCALATION, and the thing that ran long was the fast pass
        underneath it. A ceiling on the second step is not a ceiling.

        Cancelling faster-whisper mid-call is not possible, so the work
        is done on a thread and ABANDONED on the deadline: the turn
        carries on with the live transcript, the orphan finishes into
        nothing, and the row says so. Wasting one pass beats making a
        person stand at a medication cabinet for a hundred seconds.
        """
        if STT_LOCAL_CEILING > 0:
            wait = STT_LOCAL_CEILING if budget is None else max(
                STT_LOCAL_FLOOR, min(STT_LOCAL_CEILING, budget))
            out = {}

            def run():
                try:
                    out["r"] = self._fast_transcribe_now(audio_bytes)
                except Exception:
                    out["r"] = ("", "error")

            th = threading.Thread(target=run, daemon=True,
                                  name="stt-fast")
            th.start()
            th.join(wait)
            if "r" in out:
                return out["r"]
            self._stt_abandoned = getattr(self, "_stt_abandoned", 0) + 1
            if _recording():
                self._stt_note = (
                    "local pass abandoned at %.1fs — answering with "
                    "the live transcript" % wait)
            return "", "abandoned"
        return self._fast_transcribe_now(audio_bytes)

    def _fast_transcribe_now(self, audio_bytes):
        """The recogniser call itself. Separated so the ceiling above
        has something to give up on."""
        eng = self._fast_engine()
        if eng == "moonshine" and self._moonshine_v2() is not None:
            self._fw_conf = 0.0        # moonshine reports none
            return self._moonshine_transcribe(audio_bytes), "moonshine"
        fw = self._load_whisper_fast()
        if fw is not None:
            return (self._fw_transcribe(fw, audio_bytes),
                    "whisper-%s" % (getattr(self, "_whisper_fast_size",
                                            FAST_WHISPER_MODEL)))
        if self._moonshine_v2() is not None:
            self._fw_conf = 0.0
            return self._moonshine_transcribe(audio_bytes), "moonshine"
        return "", eng

    def _race_fast_engines(self):
        """Time both fast recognisers ONCE, on identical audio, on the
        real hardware — then use whichever actually wins.

        This is the whole answer to "moonshine says it's faster but the
        device clocked it at 4.47 s". We stop arguing about which is
        faster and measure it here, on this exact board, so the fast
        path is always the one that is actually fast."""
        if FAST_ENGINE_PIN in ("moonshine", "whisper"):
            self._fast_choice = FAST_ENGINE_PIN
            return
        try:
            import numpy as np
            n = int(SAMPLE_RATE * 1.5)
            tt = np.arange(n) / float(SAMPLE_RATE)
            sig = (np.sin(2 * np.pi * 180 * tt)
                   + 0.5 * np.sin(2 * np.pi * 330 * tt)
                   + 0.3 * np.sin(2 * np.pi * 90 * tt))
            sig += 0.2 * np.random.default_rng(0).standard_normal(n)
            sig = sig / (float(np.max(np.abs(sig))) or 1.0)
            buf = (sig * 8000).astype(np.int16).tobytes()
        except Exception:
            buf = b"\x00\x00" * int(SAMPLE_RATE * 1.5)

        ms_secs = float("inf")
        if self._moonshine_v2() is not None:
            try:
                t0 = time.time()
                self._moonshine_transcribe(buf)
                ms_secs = time.time() - t0
            except Exception:
                pass
        fw_secs = float("inf")
        fw = self._load_whisper_fast()
        if fw is not None:
            try:
                t0 = time.time()
                self._fw_transcribe(fw, buf, vad=False)
                fw_secs = time.time() - t0
            except Exception:
                pass
        self._ms_race_secs = ms_secs
        self._fw_race_secs = fw_secs
        if fw is not None and fw_secs <= ms_secs:
            self._fast_choice = "whisper"
        elif self._moonshine_v2() is not None and ms_secs < float("inf"):
            self._fast_choice = "moonshine"
        else:
            self._fast_choice = "whisper"

    # ── cloud STT: primary when online, free tiers only ──────────────
    def _cloud_enabled(self):
        """Is cloud STT switched on AND actually configured?

        DOSE_STT_MODE: "auto" (default) uses cloud when a free provider
        is configured and the net is up; "cloud" forces it; "local"
        turns it off entirely. Cloud is skipped silently when no
        credential is present, so a fresh device just runs local."""
        mode = os.environ.get("DOSE_STT_MODE", "auto").strip().lower()
        if mode == "local":
            return False
        try:
            import dose_cloud_stt as _c
            return bool(_c.available_providers())
        except Exception:
            return False

    def _is_online(self, ttl=30.0):
        """Cached internet check — a socket connect per utterance would
        be wasteful, and connectivity does not change second to second."""
        now = time.time()
        cached = getattr(self, "_online_cache", None)
        if cached is not None and now - cached[1] < ttl:
            return cached[0]
        try:
            import dose_cloud_stt as _c
            ok = _c.is_online()
        except Exception:
            ok = False
        self._online_cache = (ok, now)
        return ok

    # A hard wall-clock budget for the whole cloud attempt. Cloud STT
    # must NEVER freeze a turn — if it has not answered within this, we
    # abandon it and let the local model reply. gradio_client's own
    # connect/cold-start can stall for tens of seconds, which is exactly
    # what made the station look deaf, so the budget is enforced here on
    # a worker thread the turn does not wait past.
    CLOUD_BUDGET_S = float(os.environ.get("DOSE_CLOUD_BUDGET", "5.0"))

    def _cloud_transcribe(self, audio_bytes):
        """Transcribe via the free cloud chain, under a hard time
        budget. Returns (text, engine_tag, secs). Never raises, never
        blocks the turn longer than CLOUD_BUDGET_S."""
        box = {"text": "", "eng": "cloud", "secs": 0.0}

        def work():
            try:
                import dose_cloud_stt as _c
                path = self._write_wav(audio_bytes)
                try:
                    res, _all = _c.cloud_transcribe(
                        path, order=_c.available_providers(),
                        language="en")
                finally:
                    try:
                        os.unlink(path)
                    except Exception:
                        pass
                if res and res.ok:
                    box["text"] = res.text
                    box["eng"] = "cloud:" + res.engine
                    box["secs"] = res.secs
                elif res and res.error:
                    self._cloud_error = "%s: %s" % (res.engine, res.error)
            except Exception as e:
                self._cloud_error = str(e)[:80]

        th = threading.Thread(target=work, daemon=True,
                              name="cloud-stt")
        th.start()
        th.join(self.CLOUD_BUDGET_S)
        if th.is_alive():
            # cloud is taking too long — abandon it for THIS turn and let
            # local answer. The worker is a daemon; it dies with the app.
            self._cloud_error = "timed out after %.1fs — used local" \
                % self.CLOUD_BUDGET_S
            return "", "cloud", self.CLOUD_BUDGET_S
        return box["text"], box["eng"], box["secs"]

    # Intents that may be answered straight from the LIVE transcript.
    #
    # Every one of these is a question — the station looks something up
    # and says it. Nothing here moves a motor, dispenses anything,
    # changes stored data or handles a person in trouble. Those keep
    # the full recogniser and its second opinion, however long that
    # takes, because being fast about them is worth nothing and being
    # wrong about them is worth a great deal.
    QUICK_INTENTS = frozenset((
        "time", "schedule", "pills_left", "taken_today", "next_dose",
        "adherence", "did_take", "greeting", "thanks", "repeat", "help",
    ))

    def _vad_line(self):
        """One heartbeat line about the two gates a sound has to pass.

        "It hears nothing" and "it hears plenty and calls none of it
        speech" are indistinguishable from outside, and the station
        stopped entering 'listening' at exactly the point
        silero_vad.onnx stopped being deleted on every launch — so for
        the first time in a long while the VAD is genuinely running.
        That is the correct state, but it means the model's verdict is
        now load-bearing and nothing reported it.

          loud   blocks that cleared the energy gate
          voice  ...of which Silero called speech

        loud 0 means the audio is not arriving. loud high with voice 0
        means it is arriving and being rejected, which is a different
        problem with a different fix.
        """
        try:
            loud = getattr(self, "_vad_loud", 0)
            voice = getattr(self, "_vad_voice", 0)
            pct = (100.0 * voice / loud) if loud else 0.0
            # WHAT IT WILL USE, NOT WHETHER IT HAS WARMED UP YET.
            #
            # This asked `self._vad is not None`, and the session is
            # built lazily on the first is_speech() call — so a
            # freshly restarted station printed "Silero absent — fails
            # open" while the model sat on disk, ready. That is a line
            # that would have sent the next person looking for a
            # missing download. The file is the fact; the session is a
            # detail of when.
            if getattr(self, "_vad", None) is not None:
                model = "silero"
            elif os.path.exists(os.path.join(VOICE_DIR,
                                             "silero_vad.onnx")):
                model = "silero (on disk, loads on the first voice)"
            else:
                model = "energy only — silero_vad.onnx is NOT on disk"
            return ("voice gate:     loud %d   called speech %d (%.0f%%)"
                    "   floor %.0f   model: %s"
                    % (loud, voice, pct, getattr(self, "_nfloor", 0.0),
                       model))
        except Exception:
            return "voice gate:     unreadable"

    def _cache_line(self):
        """One heartbeat line about the reply cache: how many clips are
        on disk, whether anything wiped them this run, and how far the
        prewarm has got.

        It exists because the cache vanished across three restarts in a
        row — 61 to 35, 116 to 22, 126 to 0 — and there are three
        places that can delete a clip (this file's
        _purge_foreign_cache, dose_app's _retire_other_voices, and
        DOSE.sh). Working out which one by reasoning cost two device
        round trips and got the wrong answer twice. The program knows
        which one it was; this makes it say so.
        """
        try:
            import glob as _g
            d = os.path.join(VOICE_DIR, "cache")
            n = len(_g.glob(os.path.join(d, "*.wav")))
            stamp = ""
            try:
                with open(os.path.join(d, ".voice")) as f:
                    stamp = f.read().strip()
            except Exception:
                stamp = "(none)"
            purged = getattr(self, "_cache_purged", None)
            if purged:
                why = "WIPED this run (stamp was %s)" % purged
            elif getattr(self, "_cache_purge_skipped", False):
                why = "kept (voice unresolved, left alone)"
            else:
                why = "kept by this object"
            # ...AND WHAT THE DISK SAYS, which is a different question.
            # "kept" above speaks only for THIS DoseVoice in THIS
            # process, and the app builds more than one. The log is
            # written by whichever deleter actually fires, in whatever
            # process, and it outlives all of them.
            last = ""
            try:
                with open(CACHE_PURGE_LOG) as f:
                    lines = [ln.strip() for ln in f if ln.strip()]
                if lines:
                    last = "   last purge: %s" % lines[-1][:90]
            except Exception:
                pass
            done = getattr(self, "_prewarmed", 0)
            total = getattr(self, "_prewarm_total", 0)
            return ("reply cache:    %d clips   %s   prewarm %d/%d   "
                    "voice=%s%s" % (n, why, done, total, stamp, last))
        except Exception:
            return "reply cache:    unreadable"

    def _remote_line(self):
        """One heartbeat line answering 'is the Mac doing the work'."""
        if _remote_stt is None:
            return "Mac speech server: not installed on this build"
        try:
            s = _remote_stt.stats()
        except Exception as e:
            return "Mac speech server: unreadable (%s)" % str(e)[:40]
        if not s.get("paired"):
            return ("Mac speech server: NOT PAIRED — %s"
                    % (s.get("last_error") or "no dose_server.conf"))
        where = ("MAC" if (s.get("healthy") and not s.get("down_for"))
                 else "PI (local)")
        out = ("Mac speech server: %s   turns answered by Mac: %d   "
               "refused: %d   slow: %d   last round trip: %d ms"
               % (where, s.get("hits", 0), s.get("misses", 0),
                  s.get("timeouts", 0), s.get("last_ms", 0)))
        if s.get("down_for"):
            out += ("\n  backing off for %ds after %d failure(s) in a "
                    "row: %s" % (s["down_for"], s.get("fails_in_a_row", 0),
                                 s.get("last_error", "")[:60]))
        elif s.get("last_error"):
            out += "\n  last trouble: %s" % s["last_error"][:70]
        return out

    def _remote_ready(self):
        """Is the Mac there, right now, without asking the network?

        Read on the critical path of every turn, so it must never make
        a call. The probe below does that between turns and leaves the
        answer here; this is only the flag.
        """
        if _remote_stt is None:
            return False
        try:
            return bool(_remote_stt.available())
        except Exception:
            return False

    def _remote_probe_tick(self):
        """Ask the Mac whether it is awake, between turns.

        The Mac is the recogniser this station wants to use. Before
        this existed, the only thing that could bring it back after a
        failure was a timer — so a laptop that reopened its lid ten
        seconds into a back-off went unused until the timer expired,
        and every turn in between quietly used the slower local models
        while the heartbeat still said "paired".

        Cheap (one GET, two seconds at worst), never on a turn's
        critical path, and it warms the Mac's model so the first real
        question does not pay for the load.
        """
        if _remote_stt is None:
            return
        if self.state != "idle":
            return          # never compete with a turn in progress
        try:
            _remote_stt.probe()
        except Exception:
            pass

    def _quick_answer(self, live_text):
        """Answer from Vosk's transcript when it already says enough.

        THE POINT. Whisper costs about 4 s on this board and cannot be
        made much faster — it pads every utterance to a thirty-second
        window, so a one-word question costs what a sentence does. But
        the live recogniser has already produced a transcript by the
        time the person stops speaking, at no extra cost, and on this
        station it is often exactly right:

            vosk "what time is it"                -> time
            vosk "how many pills to i have left"  -> pills_left
            vosk "did i take my aspirin today"    -> did_take, Aspirin

        When that transcript ALREADY parses into a complete intent,
        waiting four more seconds to be told the same thing is waiting
        for nothing.

        The gate is the language layer's own judgement, not a
        confidence number I invented: Intent.complete means "acting on
        this now cannot be premature" — it is False for an unknown
        intent and False for one that needs a medication it has not
        matched. On the live transcripts this device logged, that is
        precisely the line between the ones that were right and the
        ones that were garbage:

            "what to i take taken"     -> unknown, complete False
            "a dose for time is it"    -> unknown, complete False
            "how many pills two i tablet" -> unknown, complete False

        So a miss costs nothing: the turn falls through to the full
        recogniser exactly as before. A hit costs the four seconds.
        """
        text = (live_text or "").strip()
        if not text or len(text.split()) < 2:
            return None
        if _nlu_mod is None:
            return None
        try:
            if _nlu_mod.looks_hallucinated(text):
                return None
            # The medication list is read from disk by _med_names().
            # This runs on the critical path of every turn, at the
            # moment the person stops speaking, so it is cached for a
            # few seconds — long enough to cost nothing within a
            # conversation, short enough that adding a medication is
            # noticed immediately afterwards. Without this the gate
            # itself added ~60 ms to the endpoint, which a reliability
            # test caught.
            now = time.time()
            meds, at = getattr(self, "_qa_meds", (None, 0.0))
            if meds is None or now - at > 5.0:
                meds = list(self._med_names() or ())
                self._qa_meds = (meds, now)
            intent = _nlu_mod.parse(text, meds)
            if intent.name not in self.QUICK_INTENTS:
                return None
            if not intent.complete:
                return None
            self._quick_hits = getattr(self, "_quick_hits", 0) + 1
            return text
        except Exception:
            return None

    def _cap_audio(self, buf):
        """Never hand the recogniser more audio than a turn can afford.

        Transcription cost is linear in audio length, so the worst turn
        in turns.jsonl is the longest one. The device logged

            "fast": 25.02   "speak": 2.01   "total": 33.66

        on a buffer that had been accumulating while somebody talked
        past the end of their sentence, a television played, or the
        endpointer waited through a long pause. Twenty-five seconds of
        transcription cannot be rescued by a budget downstream; by then
        it has already been spent.

        The LAST seconds are kept, not the first. A turn ends when
        somebody stops speaking, so the words that matter are at the
        end — and a person who rambles and then asks the question is
        much commoner than one who asks and then rambles.

        Returns the buffer unchanged when it is already short enough,
        which is almost always.
        """
        try:
            cap = int(STT_MAX_AUDIO_S * SAMPLE_RATE * 2)
            if cap <= 0 or len(buf) <= cap:
                return bytes(buf)
            self._audio_capped = getattr(self, "_audio_capped", 0) + 1
            # Held separately: _better_transcribe clears _stt_note at
            # the top of every turn, and a note written before it runs
            # would be wiped by the function it is describing.
            self._cap_note = ("audio capped: %.1fs of %.1fs kept"
                              % (STT_MAX_AUDIO_S,
                                 len(buf) / 2.0 / float(SAMPLE_RATE)))
            return bytes(buf[-cap:])
        except Exception:
            return bytes(buf)

    def _better_transcribe(self, audio_bytes, vosk_text, allow_cloud=True,
                           allow_remote=True, fast_remote=False):
        """Work out what was actually said, trying harder when the
        first answer means nothing.

        WHEN THE INTERNET IS UP, a free cloud recogniser answers first:
        the Pi 4 is a poor place to run Whisper (4.47 s per utterance),
        so transcription belongs off the box. Cloud is tried once per
        turn (allow_cloud is False for the speculative pass, to spare
        the free quota); the LOCAL model is the offline fallback and
        also catches any cloud miss. A cloud failure never raises — it
        just falls through to local."""
        if not audio_bytes:
            return vosk_text
        t_start = time.time()
        # Which engine actually answered, on a channel BOTH passes can
        # write. self._last_engine is guarded by _recording() so the
        # speculation cannot stamp its name on a live turn — correct,
        # and the reason finish() needs this to decide whether the
        # speculative answer is worth reusing.
        _TL.engine = ""
        audio_bytes = self._trim_silence(audio_bytes)
        # NOT ENOUGH AUDIO TO CONTAIN A QUESTION.
        #
        # From the device: 0.36 s of audio at peak 2260 was handed to
        # a recogniser that spent 9.90 s on it and returned nothing.
        # A third of a second is not a sentence — it is a chair, a
        # cough, or the tail of the station's own reply — and no model
        # is going to find a question in it, however long it looks.
        #
        # Measured against the turns that WORKED, so this cannot eat a
        # real one: the shortest good utterance in the log is "Okay."
        # at 1.9 s of buffer, and every correct turn carried at least
        # 0.88 s of trimmed audio. The bar is half a second.
        try:
            _secs = len(audio_bytes) / 2.0 / float(SAMPLE_RATE)
        except Exception:
            _secs = 0.0
        if audio_bytes and _secs < MIN_TURN_AUDIO_S:
            self._too_short = getattr(self, "_too_short", 0) + 1
            # ONE GUARD PER WRITE, like every other write in this
            # method. A single `if _recording():` around a block is
            # equally correct and the suite cannot see it — it checks
            # that each write is guarded by reading the line above it.
            # Rewriting the test to understand blocks would make it
            # weaker at catching the bug it exists for, which has now
            # happened four times.
            if _recording():
                self._t_fast = time.time() - t_start
            if _recording():
                self._t_slow = 0.0
            if _recording():
                self._last_engine = "too short"
            if _recording():
                self._stt_note = (
                    "%.2fs of audio — below %.2fs, answered without "
                    "running a recogniser" % (_secs, MIN_TURN_AUDIO_S))
            return vosk_text or ""
        self._raw_vosk = vosk_text or ""
        self._raw_fast = ""
        self._raw_slow = ""
        self._raw_cloud = ""
        # Cleared per turn, not per branch: a note left over from the
        # previous turn attached to this one is a lie in the log, and
        # the log is the only account of what happened out there. A cap
        # applied on the way IN is carried over, because it happened to
        # this turn and is the first thing worth knowing about it.
        if _recording():
            self._stt_note = getattr(self, "_cap_note", "") or ""
        self._cap_note = ""

        # 0a) THE MAC, if the Mac is in the house.
        #
        # Not "cloud" — a machine on this LAN, addressed by a private
        # IP, checked before a socket is opened. See dose_remote_stt.
        # It is tried before the local models because when it answers
        # it answers in a fraction of the time, and it is skipped
        # instantly when it is not there: a laptop leaving the house
        # must not make a medicine cabinet slower, let alone deaf.
        # allow_remote, NOT allow_cloud. The speculative pass sets
        # allow_cloud=False because spending a metered free-tier quota
        # on a transcript that may be thrown away is wasteful — and
        # that reasoning has nothing to do with a Mac sitting idle on
        # this LAN. It has no quota, it is doing nothing between
        # turns, and letting the speculation use it is what turns the
        # remaining 0.8-1.0 s of STT into a cache hit.
        if allow_remote and _remote_stt is not None:
            try:
                if _remote_stt.available():
                    t_r = time.time()
                    # THE FAST ROUTE FOR THE PASS THAT HAS TO
                    # FINISH FIRST.
                    #
                    # The speculation fires 0.18 s into a pause and
                    # the endpointer fires at 0.45 s, so it has about
                    # a quarter of a second to come back. Measured on
                    # the Mac: base.en 0.21 s, small.en 0.57 s, and
                    # identical output on every command phrase this
                    # station is asked. One fits in the window and one
                    # does not, which is the whole difference between
                    # a 0.45 s turn and a 1.1 s one.
                    #
                    # The real pass still gets small.en, so anything
                    # the fast model could not parse — a drug name,
                    # an unusual sentence — is asked again properly.
                    rtext = _remote_stt.transcribe(
                        self._wav_bytes(audio_bytes), fast=fast_remote)
                    r_secs = time.time() - t_r
                    if rtext and self._usable(rtext):
                        _TL.engine = "mac"
                        if _recording():
                            self._t_fast = time.time() - t_start
                        if _recording():
                            self._t_slow = 0.0
                        if _recording():
                            self._last_engine = "mac"
                        if _recording():
                            self._stt_note = (
                                "answered by the Mac in %.2fs" % r_secs)
                        return rtext
                    # THE MAC ANSWERED AND THE ANSWER WAS NO GOOD.
                    #
                    # This used to fall silently through to the Pi's
                    # own models, and that is where every long turn in
                    # the log comes from. From the device:
                    #
                    #   heard '[unk]'  7.6s of audio  fast  9.01  abandoned
                    #   heard '[unk]'  0.5s of audio  fast  9.90  tiny.en
                    #   heard '[unk] it yes'          fast 10.95  abandoned
                    #
                    # against 0.75-1.11 s on every one of the forty
                    # turns the Mac did answer. There is no middle.
                    #
                    # The Pi's tiny.en is not a second opinion on the
                    # Mac's small.en — it is a WEAKER model, and it is
                    # the offline fallback, not a court of appeal. Nine
                    # seconds to be told the same thing by something
                    # less able is the worst outcome available, and it
                    # is the one the person actually stands there for.
                    #
                    # So: a Mac that answered has answered. Say "I
                    # didn't catch that" in a second instead of taking
                    # ten to say it.
                    if rtext is not None and _remote_stt.available():
                        self._mac_unusable = getattr(
                            self, "_mac_unusable", 0) + 1
                        if _recording():
                            self._t_fast = time.time() - t_start
                        if _recording():
                            self._t_slow = 0.0
                        if _recording():
                            self._last_engine = "mac (unclear)"
                        if _recording():
                            self._stt_note = (
                                "the Mac answered %r in %.2fs and it "
                                "did not parse — not paying the Pi's "
                                "weaker model to agree"
                                % (str(rtext)[:40], r_secs))
                        return rtext or ""
            except Exception:
                pass

        # 0) CLOUD FIRST when it is available and this is the real
        #    (non-speculative) pass.
        if allow_cloud and self._cloud_enabled() and self._is_online():
            ctext, ceng, csecs = self._cloud_transcribe(audio_bytes)
            self._raw_cloud = ctext or ""
            if ctext and self._usable(ctext):
                if _recording():
                    self._t_fast = time.time() - t_start
                if _recording():
                    self._t_slow = 0.0
                if _recording():
                    self._last_engine = ceng
                return ctext
            # cloud unusable/failed — fall through to the local models

        # 1) THE FAST PATH answers.
        # WHAT IS LEFT OF THE TURN, NOT A FIXED EIGHT SECONDS.
        #
        # The ceiling worked — a pass that used to run 105 s was
        # abandoned at 8 — but the turn it was in still came to 11.96 s,
        # because the Mac had already spent its timeout before the
        # local pass started its own. Two bounded steps in a row are
        # not a bounded turn.
        #
        # STT_TURN_BUDGET is the number that was chosen against what a
        # person does: past about five seconds they assume the machine
        # did not hear them and say it again. So the fast pass gets
        # what remains of it, floored at something a recogniser can
        # actually finish in, and never more than the hard ceiling.
        fast, feng = self._fast_transcribe(
            audio_bytes, budget=STT_TURN_BUDGET - (time.time() - t_start))
        fast_conf = getattr(self, "_fw_conf", 0.0)
        self._raw_fast = fast or ""
        if _recording():
            self._t_fast = time.time() - t_start
        # Accept the fast answer only if it parses AND the model was
        # reasonably sure of it. A confident score with a shaky reading
        # (low avg_logprob) is exactly how a fast model hands back a
        # clean-looking wrong answer, so that still gets a second
        # opinion below. conf == 0.0 means "no confidence reported"
        # (moonshine) — trusted, since it has no signal to distrust.
        confident = fast_conf == 0.0 or fast_conf >= self.FAST_CONF_FLOOR
        if fast and confident and self._usable(fast):
            if _recording():
                self._t_slow = 0.0
            if _recording():
                self._last_engine = feng
            return fast

        # 2) Nothing we can act on — the stronger base.en model gets a
        #    turn on the same audio.
        #
        # ── BUT NOT AT ANY PRICE ─────────────────────────────────────
        # Measured on the device, from its own turns.jsonl:
        #
        #     worst   "fast": 17.26   "speak": 1.41   "total": 25.19
        #     best    "fast":  0.12   "speak": 1.90   "total":  2.52
        #
        # Endpointing accounts for 0.49-0.57 s of that. ALL the variance
        # is transcription — and the worst turn is the fast model taking
        # seventeen seconds and then the escalation being started ON TOP
        # of it, because nothing anywhere asked what time it was.
        #
        # A better answer that arrives at twenty-five seconds is not a
        # better answer. The person has repeated themselves, walked off,
        # or tapped the logo again. So escalation now has to fit in what
        # is left of the turn's budget.
        #
        # The cost is ESTIMATED FROM THIS DEVICE'S OWN MEASUREMENT, not
        # from a constant: base.en is roughly ESCALATION_COST_RATIO times
        # the fast model on the same audio, and self._t_fast is what the
        # fast model just took on exactly this audio, on exactly this
        # board, under exactly this load. A Pi under thermal throttling
        # and a Pi that has just booted are different machines, and a
        # hardcoded "escalation takes 4 s" is wrong on both.
        #
        # When it does not fit, step 3 below still runs and still hands
        # back the best near-miss it has, which the phonetic matcher can
        # work with. Skipping is recorded, because a station that never
        # escalates is a station whose accuracy quietly fell.
        elapsed = time.time() - t_start
        left = STT_TURN_BUDGET - elapsed
        est = max(0.2, self._t_fast * ESCALATION_COST_RATIO)
        if left < est:
            self._escalations_skipped = getattr(
                self, "_escalations_skipped", 0) + 1
            if _recording():
                self._stt_note = (
                    "escalation skipped: %.1fs used of %.1fs, "
                    "base.en needs about %.1fs"
                    % (elapsed, STT_TURN_BUDGET, est))
            self._raw_slow = ""
            if _recording():
                self._t_slow = 0.0
            if _recording():
                self._last_engine = feng
            # The fast answer, if there is one at all, beats silence and
            # beats making the person wait for an answer they will not
            # be there to hear.
            if fast:
                return fast
            return vosk_text or ""
        t_wh = time.time()
        wh = self._whisper_transcribe(audio_bytes)
        self._raw_slow = wh or ""
        if _recording():
            self._t_slow = time.time() - t_wh
        if _recording():
            self._last_engine = "whisper" if wh else feng
        if wh and self._usable(wh):
            self._whisper_saves = getattr(self, "_whisper_saves", 0) + 1
            return wh

        # 3) Neither parsed. Hand back the one with MORE IN IT — a
        #    longer, more word-like answer is the better near-miss for
        #    the phonetic matcher than a confident scrap.
        cands = [c for c in (fast, wh)
                 if c and not (_nlu_mod
                               and _nlu_mod.looks_hallucinated(c))]
        if cands:
            best = max(cands, key=lambda c: (len(c.split()), len(c)))
            if _recording():
                self._last_engine = feng if best == fast else "whisper"
            return best
        return vosk_text

    @staticmethod
    def _shut_stream(st, tag="probe"):
        """Close a PortAudio stream WITHOUT ever blocking the caller.

        sounddevice's Stream.stop() calls Pa_StopStream(), which waits
        for the device to drain before it returns. On this station that
        call was observed never returning — see the DEADLINES comment at
        the top of this file for the py-spy dump that caught it. The
        engine sat in it for seven minutes holding the ALSA PCM in
        SETUP, and would have sat in it until the process was killed.

        Two changes make that impossible:

          * abort() (Pa_AbortStream) DISCARDS buffered audio instead of
            draining it. For an input probe there is nothing worth
            draining — we already have the frames we measured — so this
            is both faster and the correct semantic.

          * it still runs on a daemon thread that is only joined for
            PROBE_CLOSE_TIMEOUT. abort() is a call into C and a wedged
            USB device can hang anything. A leaked stream costs one file
            descriptor and one thread; a wedged engine costs a deaf
            station, which is what we actually had.

        Returns True if the close completed, False if it was abandoned.
        A False here is a real fault and callers treat the device as bad.
        """
        done = threading.Event()

        def shut():
            for fn in ("abort", "close"):
                try:
                    getattr(st, fn)()
                except Exception:
                    pass
            done.set()

        threading.Thread(target=shut, name="pa-shut-" + str(tag),
                         daemon=True).start()
        return done.wait(PROBE_CLOSE_TIMEOUT)

    def _probe_device(self, index, native_rate, deadline=None):
        """Open a device briefly and measure real signal (RMS).

        Returns (rms, usable_rate), or None if it can't open, runs out
        of time, or wedges on close. Bounded absolutely: no single
        device may cost more than PROBE_DEVICE_BUDGET, and `deadline`
        (a time.time() value) caps the caller's whole scan on top of
        that. Running out of time returns None — a device we could not
        finish measuring is not a device we are willing to select.
        """
        own = time.time() + PROBE_DEVICE_BUDGET
        if deadline is not None:
            own = min(own, deadline)
        rates = []
        for r in (SAMPLE_RATE, native_rate, 48000, 44100, 24000, 8000):
            if r and r not in rates:
                rates.append(r)
        for rate in rates:
            if time.time() >= own:
                break
            frames = []

            def cb(indata, f, t, s):
                frames.append(bytes(indata))
            try:
                st = self._sd.RawInputStream(
                    device=index, samplerate=rate,
                    blocksize=max(256, int(0.2 * rate)),
                    dtype="int16", channels=1, callback=cb)
                st.start()
            except Exception:
                continue
            # Listen for the shorter of the per-rate sample and whatever
            # is left of this device's budget — never longer.
            time.sleep(max(0.15, min(PROBE_RATE_SECONDS,
                                     own - time.time())))
            clean = self._shut_stream(st, "%s@%s" % (index, rate))
            if not clean:
                # The close did not come back. The device is wedged; the
                # old code would be sitting in it right now. Record it,
                # abandon this device entirely, and let selection move on.
                try:
                    self._probe_wedged.append(
                        "device %s @ %s Hz: close did not return"
                        % (index, rate))
                    del self._probe_wedged[:-10]
                except Exception:
                    pass
                return None
            data = b"".join(frames)
            if not data:
                continue
            try:
                import audioop
                rms = audioop.rms(data, 2)
            except Exception:
                rms = 1
            return rms, rate
        return None

    def _unmute_alsa_inputs(self, force=False):
        """USB microphones AND speakers frequently arrive with their
        ALSA capture volume at zero or the capture switch OFF — which
        makes arecord record pure digital silence (the exact 'mic
        never hears anything' failure). Brute-force EVERY control on
        EVERY card to full, enabling capture, with several amixer
        forms so a differently-named C-Media control can't be
        missed. Harmless if already fine.

        Harmless, but not free, and it was being done over and over.
        See MIXER_REDO_AFTER. `force` is for the paths where a person
        is waiting on the answer and stale is not acceptable — the full
        mic test, an explicit rescan.
        """
        for card in range(6):
            self._max_capture(card, force=force)

    def _mixer_cache_clear(self):
        """Forget which cards have been unmuted.

        Called when the audio hardware fingerprint changes. A card
        number can be REUSED by different hardware across a replug, so
        this clears everything rather than trying to be clever about
        which card moved.
        """
        try:
            self._mixer_done = {}
        except Exception:
            pass

    def _max_capture(self, card, force=False):
        """Force every control on one card to full & capturing.

        Skipped when this card was already forced recently — this is
        dozens of process spawns and its result cannot have changed in
        the meantime. See MIXER_REDO_AFTER.
        """
        done = getattr(self, "_mixer_done", None)
        if done is None:
            done = self._mixer_done = {}
        if not force:
            last = done.get(card)
            if last is not None and time.time() - last < MIXER_REDO_AFTER:
                return
        # Stamp BEFORE the work, not after. If amixer hangs or the card
        # does not exist, we must not come straight back and try the
        # whole sweep again on the next capture open.
        done[card] = time.time()
        try:
            out = subprocess.run(
                ["amixer", "-c", str(card), "scontrols"],
                capture_output=True, text=True, timeout=5,
                env=self._audio_env()).stdout
        except Exception:
            return
        if not out:
            return
        for line in out.splitlines():
            m = re.search(r"'([^']+)'", line)
            if not m:
                continue
            name = m.group(1)
            low = name.lower()
            capish = any(k in low for k in ("capture", "mic", "input",
                                            "adc"))
            playbackish = (not capish) and any(k in low for k in (
                "speaker", "master", "headphone", "pcm", "output"))
            sourceish = any(k in low for k in ("source", "mux",
                                               "input source"))
            attempts = []
            if not playbackish:
                # LEAVE THE LEVEL ALONE. All we do here is make sure
                # the device is unmuted and selected as a capture
                # source — we do not turn it up.
                #
                # It used to be forced to 100%, then to 80%. Both were
                # wrong for the same reason: a microphone that works
                # properly arrives at a sensible level, and overriding
                # it only overdrives the input. A decent USB mic does
                # its own conditioning, so stacking our gain on top of
                # it is how you end up hearing the whole room and none
                # of the person.
                #
                # DOSE_CAPTURE_LEVEL forces a percentage if a
                # particular capsule really does need one, and the
                # clipping watchdog can still step it down. Neither
                # runs by default.
                attempts += [["cap", "unmute"], ["on", "cap"], ["cap"],
                             ["unmute"]]
                if self._capture_level:
                    lvl = "%d%%" % self._capture_level
                    attempts = [[lvl] + a for a in attempts] + attempts
            else:
                attempts += [["90%", "unmute", "on"]]
            # An input-source/mux enum: try selecting a mic/line item
            # (PCM2902 'PCM Capture Source' often defaults to the wrong
            # input). Setting an enum to a name it doesn't have is a
            # harmless error.
            if sourceish:
                for item in ("Mic", "Microphone", "Line", "Line In",
                             "Input", "Capture"):
                    attempts.append([item])
            for args in attempts:
                try:
                    subprocess.run(
                        ["amixer", "-c", str(card), "sset", name]
                        + args, capture_output=True, timeout=5,
                        env=self._audio_env())
                except Exception:
                    pass
        # Second pass by numid via 'amixer contents' — catches capture
        # switches/volumes that name-based sset misses (the surest way
        # to turn a capture control ON).
        self._max_capture_by_numid(card)

    def _max_capture_by_numid(self, card):
        """Enable every CAPTURE-capable control by numid with cset.

        This used to cset every capture volume to a hardcoded 100%,
        which silently undid the level the caller had just set:
        _max_capture() writes _capture_level (70% by default) to the
        named control and then calls straight into here, which put
        100% back over the top. A device audit found the AIRHUG
        sitting at exactly 100% (+31.99 dB) while DEFAULT_CAPTURE_LEVEL
        was 70 — the symmetric auto-leveller's decisions never reached
        the hardware at all. It now honours _capture_level when one is
        set, and only falls back to full when nothing was asked for."""
        try:
            out = subprocess.run(
                ["amixer", "-c", str(card), "contents"],
                capture_output=True, text=True, timeout=6,
                env=self._audio_env()).stdout
        except Exception:
            return
        numid = None
        is_cap = False
        is_bool = False
        for line in (out or "").splitlines():
            m = re.match(r"numid=(\d+)", line)
            if m:
                numid = m.group(1)
                is_cap = False
                is_bool = False
                low = line.lower()
                # capture controls are marked access=...capture or
                # named with CAPTURE/Mic in the same numid line
                if ("capture" in low or "'mic" in low
                        or "input" in low):
                    is_cap = True
                if "type=boolean" in low:
                    is_bool = True
                continue
            if numid is None:
                continue
            low = line.lower()
            if "capture" in low or "mic" in low or "input" in low:
                is_cap = True
            if "type=boolean" in low:
                is_bool = True
            # once we hit the values line, act
            if line.strip().startswith(": values=") and is_cap:
                try:
                    if is_bool:
                        subprocess.run(
                            ["amixer", "-c", str(card), "cset",
                             "numid=" + numid, "on"],
                            capture_output=True, timeout=5,
                            env=self._audio_env())
                    else:
                        lvl = ("%d%%" % self._capture_level
                               if self._capture_level else "100%")
                        subprocess.run(
                            ["amixer", "-c", str(card), "cset",
                             "numid=" + numid, lvl],
                            capture_output=True, timeout=5,
                            env=self._audio_env())
                except Exception:
                    pass
                numid = None

    @staticmethod
    def _is_usb_name(name):
        """Does this device name look like a plugged-in USB unit?
        Cheap Pi mics (SunFounder mini and friends) enumerate as
        C-Media 'USB PnP Sound Device' — sometimes without the word
        USB in the name PortAudio shows."""
        n = (name or "").lower()
        return any(k in n for k in ("usb", "pnp", "c-media", "cmedia",
                                    "cm108", "cm106", "audio device"))

    @staticmethod
    def _looks_like_speaker(desc):
        """Does this card look like a pure OUTPUT device (a USB
        speaker) that might expose a dead capture endpoint? Used to
        deprioritize it so a real mic wins. Jieli 'UACDemo' /
        'Advanced Audio Device' boards are the common cheap USB
        speaker the user has alongside the mic."""
        d = (desc or "").lower()
        return any(k in d for k in ("jieli", "uacdemo", "uac demo",
                                    "advanced audio", "speaker",
                                    "headphone", "output", "playback"))

    @staticmethod
    def _looks_like_mic(desc):
        """Does this card look like an actual MICROPHONE (as opposed
        to a speaker's capture endpoint)? Known USB mic chips:
        C-Media 'USB PnP Sound Device' (SunFounder), and the Texas
        Instruments / Burr-Brown PCM2902 'USB Audio CODEC' used by
        many cheap USB mics. Prefer these so we point at the mic,
        never the speaker's input side."""
        d = (desc or "").lower()
        if DoseVoice._looks_like_speaker(desc):
            return False
        return any(k in d for k in (
            "c-media", "cmedia", "cm108", "cm106", "pnp",
            "sound device", "microphone", " mic", "webcam",
            "sunfounder", "pcm2902", "texas instrument",
            "burr-brown", "burr brown", "audio codec", "codec"))

    @staticmethod
    def _arecord_capture_cards():
        """Authoritative list of RECORDABLE devices from `arecord -l`
        — the exact tool the Raspberry Pi mic guides use. It lists
        ONLY capture-capable hardware, with real card AND device
        numbers, e.g.:
            card 2: CODEC [USB Audio CODEC], device 0: USB Audio ...
        Returns [(card, device, "name longname"), ...]. Empty if
        arecord isn't installed (caller falls back to /proc/asound)."""
        try:
            out = subprocess.run(["arecord", "-l"],
                                 capture_output=True, text=True,
                                 timeout=6).stdout
        except Exception:
            return []
        found = []
        for line in (out or "").splitlines():
            m = re.match(
                r"\s*card\s+(\d+):\s*([^\[]*)\[([^\]]*)\].*?"
                r"device\s+(\d+):\s*([^\[]*)\[([^\]]*)\]", line)
            if m:
                card = int(m.group(1))
                dev = int(m.group(4))
                name = " ".join(x.strip() for x in
                                (m.group(2), m.group(3),
                                 m.group(5), m.group(6)) if x.strip())
                found.append((card, dev, name))
        return found

    # ── THE WINNER CACHE IS FED BY ACCEPTANCE, NOT BY OPENING ────────
    #
    # open_arecord() remembers the (subdevice, base, rate, channels)
    # combination that worked for a card so a reopen costs one spawn
    # instead of a sweep. That is worth having — the sweep is the
    # better part of half a minute on this board.
    #
    # It was written the moment arecord started, and on this hardware
    # every combination starts: plughw converts anything to anything,
    # so 16 kHz mono "works" in the only sense that check could see,
    # while delivering the averaged silence that was root-caused
    # yesterday. Once cached it was tried first on every reopen and
    # never re-examined, because the only thing that evicted it was a
    # failure to OPEN.
    #
    # These three keep the cache honest, and they are deliberately
    # separate from the walk that calls them: the question "did this
    # route prove itself" is answerable without a microphone, and
    # tests/test_capture_channels.py answers it that way.
    def _arecord_propose(self):
        """Forget any combination proposed by an earlier route, so a
        rejection can never be attributed to the wrong opener."""
        self._arecord_pending = None

    def _arecord_confirm(self):
        """The walk accepted this route as live. NOW it may be cached."""
        pend = getattr(self, "_arecord_pending", None)
        if not pend:
            return None
        card, combo = pend
        try:
            win = getattr(self, "_arecord_win", None)
            if win is None:
                win = self._arecord_win = {}
            win[card] = combo
        except Exception:
            return None
        self._arecord_pending = None
        return (card, combo)

    def _arecord_reject(self):
        """The walk measured this route and found nothing. Evict it, so
        the sweep underneath gets its turn on the next reopen instead
        of the cache handing back the same silence forever."""
        pend = getattr(self, "_arecord_pending", None)
        if not pend:
            return None
        card, combo = pend
        try:
            win = getattr(self, "_arecord_win", None) or {}
            if win.get(card) == combo:
                win.pop(card, None)
        except Exception:
            pass
        self._arecord_pending = None
        return (card, combo)

    @staticmethod
    def _capture_subdevices(card):
        """How many capture subdevices this card REALLY has.

        The sweep used to try subdevices 0, 1, 2 and 3 on every card,
        because some cheap USB mics put the working capture on
        subdevice 1. Card 5 on this station has exactly ONE
        (`subdevices_count: 1` in /proc/asound/card5/pcm0c/info), so
        three quarters of the sweep was asking the kernel for devices
        that cannot exist — and each refusal costs a process spawn and
        a wait.

        Measured consequence: a startup selection that took 64.0s
        against a 45s budget and ended in "chose: NOTHING", while the
        log filled with

            arecord -D plughw:5,2 ...: audio open error:
                No such file or directory

        Asking the kernel how many there are turns 48 attempts into 12.
        Falls back to [0] — never to a guess at more.
        """
        n = 0
        try:
            with open("/proc/asound/card%d/pcm0c/info" % int(card)) as f:
                for line in f:
                    if line.startswith("subdevices_count:"):
                        n = int(line.split(":", 1)[1].strip())
                        break
        except Exception:
            n = 0
        if n < 1 or n > 8:
            return [0]
        return list(range(n))

    @staticmethod
    def _native_channels(card):
        """How many channels this card actually captures.

        /proc/asound/card<N>/stream0 is the USB descriptor as the
        kernel read it, so it is the device's own answer rather than a
        guess. Anything else goes through ALSA's plug layer, and for a
        channel count that means an AVERAGE — which is how this station
        went deaf: a two-channel capsule averaged down to one, with a
        noise floor of one or two LSB, produces exact zeros.

        Defaults to 2 when the file is unreadable or says nothing. That
        is the safe default, not a neutral one: capturing two channels
        from a mono device duplicates the channel and ingest() picks
        the louder of two identical ones, which costs nothing. The
        reverse — asking for one channel from a stereo device — is the
        bug.
        """
        try:
            with open("/proc/asound/card%d/stream0" % int(card)) as f:
                text = f.read()
        except Exception:
            return 2
        best = 0
        for m in re.finditer(r"Channels:\s*(\d+)", text):
            try:
                best = max(best, int(m.group(1)))
            except Exception:
                pass
        if best in (1, 2, 4, 6, 8):
            return best
        return 2

    @staticmethod
    def _card_has_playback(card):
        """True if this ALSA card also exposes a PLAYBACK device — i.e.
        it's a speaker/headset (its capture side is likely a phantom
        endpoint), not a pure microphone. A cheap USB speaker (HONKYOB
        etc.) has playback; a real USB mic is capture-only."""
        try:
            for entry in os.listdir("/proc/asound/card%d" % card):
                if entry.startswith("pcm") and entry.endswith("p"):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _rank_capture(desc, has_playback=False):
        """Rank capture devices so the REAL mic wins:
          0  capture-only device with a mic-ish name (best)
          1  capture-only device (pure input = almost certainly a mic)
          2  other USB capture device
          3  device that ALSO plays back (a speaker/headset — its
             'capture' is probably a phantom endpoint)
          4  a device whose name looks like a pure speaker
        A USB SPEAKER'S fake input can no longer be mistaken for the
        mic."""
        if DoseVoice._looks_like_speaker(desc):
            return 4
        if has_playback:
            return 3
        if DoseVoice._looks_like_mic(desc):
            return 0
        return 1

    @staticmethod
    def _alsa_capture_cards():
        """Recordable devices as (card, device, desc), the actual
        microphone first. Primary source is `arecord -l` (only lists
        capture hardware); falls back to parsing /proc/asound/cards
        if arecord is unavailable. This is how we LOCATE the real mic
        device (e.g. the PCM2902 USB Audio CODEC) directly, and target
        arecord -D plughw:<card>,<device> at exactly it."""
        rec = DoseVoice._arecord_capture_cards()
        if rec:
            rec.sort(key=lambda c: (
                DoseVoice._rank_capture(
                    c[2], DoseVoice._card_has_playback(c[0])),
                c[0]))
            return rec
        # fallback: kernel card list, assume device 0
        cards = {}
        try:
            with open("/proc/asound/cards") as f:
                text = f.read()
        except Exception:
            return []
        cur = None
        for line in text.splitlines():
            m = re.match(r"\s*(\d+)\s+\[", line)
            if m:
                cur = int(m.group(1))
                cards[cur] = line
            elif cur is not None and cards.get(cur):
                cards[cur] += " " + line.strip()
        capture = []
        for num, desc in cards.items():
            has_cap = False
            try:
                for entry in os.listdir("/proc/asound/card%d" % num):
                    if entry.startswith("pcm") and entry.endswith("c"):
                        has_cap = True
                        break
            except Exception:
                has_cap = True
            if has_cap:
                capture.append((num, 0, desc))
        capture.sort(key=lambda c: (
            DoseVoice._rank_capture(
                c[2], DoseVoice._card_has_playback(c[0])),
            c[0]))
        return capture

    def _pa_refresh(self):
        """Re-scan PortAudio's device list. PortAudio snapshots the
        hardware once at startup, so a USB mic plugged in AFTER launch
        stays invisible until this runs. Only safe to call when no
        capture stream is open — open_capture calls it right after
        closing the old stream."""
        try:
            self._sd._terminate()
            self._sd._initialize()
        except Exception:
            pass

    def _audio_sig(self):
        """Fingerprint of the machine's audio devices. It changes the
        moment a USB or Bluetooth mic/speaker is plugged in or pulled,
        which is how the engine notices hot-plugs. The kernel's own
        card list (/proc/asound/cards) is the primary source — it
        needs no tools installed and every USB audio device appears
        there instantly. Names only — state columns flip constantly
        and would cause spurious reopens."""
        names = []
        try:
            with open("/proc/asound/cards") as f:
                cur = None
                for ln in f:
                    m = re.match(r"\s*(\d+)\s+\[", ln)
                    if m:
                        cur = m.group(1)
                    if "[" in ln and "]" in ln:
                        # include the CARD NUMBER so a USB port swap
                        # (which reassigns card numbers) changes the
                        # fingerprint and triggers re-selection
                        nm = ln.split("[")[1].split("]")[0].strip()
                        names.append("%s:%s" % (cur, nm))
        except Exception:
            pass
        env = self._audio_env()
        for what in ("sources", "sinks"):
            try:
                r = subprocess.run(["pactl", "list", "short", what],
                                   capture_output=True, text=True,
                                   timeout=5, env=env)
                if r.returncode == 0:
                    for ln in (r.stdout or "").splitlines():
                        parts = ln.split()
                        if len(parts) > 1:
                            names.append(parts[1])
            except Exception:
                pass
        return "|".join(sorted(names)) if names else None

    def _pick_input_device(self):
        """Choose the input whose audio actually FLOWS. Bluetooth
        headsets (AirPods) often expose a dead input until the
        hands-free profile engages, and 'default' may not be them —
        so prefer headset/USB names, but demand real signal."""
        try:
            devs = self._sd.query_devices()
        except Exception:
            return None, "default", SAMPLE_RATE
        cands = []
        for i, d in enumerate(devs):
            try:
                if d.get("max_input_channels", 0) < 1:
                    continue
                n = (d.get("name") or "").lower()
                if self._is_usb_name(n):
                    pri = 0
                elif n in ("default", "pipewire", "pulse",
                           "sysdefault"):
                    pri = 1
                elif any(k in n for k in ("airpod", "bluez", "headset",
                                          "hands-free", "hfp")):
                    pri = 3          # Bluetooth last — plugged-in wins
                else:
                    pri = 2
                cands.append((pri, i, d.get("name", "?"),
                              int(d.get("default_samplerate")
                                  or SAMPLE_RATE)))
            except Exception:
                continue
        cands.sort()
        best_silent = None
        # One budget for the WHOLE scan. Without it, selection cost grows
        # with however many capture devices the Pi happens to enumerate —
        # and this station enumerates enough of them that a full scan was
        # taking longer than a user is willing to wait for a first word.
        deadline = time.time() + PROBE_PICK_BUDGET
        for pri, i, name, native in cands:
            if time.time() >= deadline:
                self._probe_timeouts = getattr(
                    self, "_probe_timeouts", 0) + 1
                break
            got = self._probe_device(i, native, deadline=deadline)
            if got is None:
                continue
            rms, rate = got
            if rms > 25:            # live room audio
                self.mic_rms = rms
                return i, name, rate
            if best_silent is None:
                best_silent = (i, name, rate, rms)
        if best_silent:
            i, name, rate, rms = best_silent
            self.mic_rms = rms
            return i, name + " (no signal yet)", rate
        return None, "default", SAMPLE_RATE

    def _pw_dump(self):
        try:
            r = subprocess.run(["pw-dump"], capture_output=True,
                               text=True, timeout=10,
                               env=self._audio_env())
            return json.loads(r.stdout) if r.stdout else []
        except Exception:
            return []

    @staticmethod
    def _parse_pw_dump(dump):
        """(bt_devices, sources): Bluetooth devices with their
        available profiles, and live audio input sources."""
        devices, sources = [], []
        for obj in dump:
            try:
                info = obj.get("info") or {}
                props = info.get("props") or {}
                if obj.get("type", "").endswith("Interface:Device") \
                        and props.get("device.api") == "bluez5":
                    profs = []
                    for p in (info.get("params") or {}).get(
                            "EnumProfile") or []:
                        profs.append({"index": p.get("index"),
                                      "name": p.get("name", "")})
                    devices.append({
                        "id": obj.get("id"),
                        "name": props.get("device.description",
                                          props.get("device.name",
                                                    "?")),
                        "profiles": profs})
                elif obj.get("type", "").endswith("Interface:Node") \
                        and (props.get("media.class")
                             == "Audio/Source"):
                    sources.append(props.get(
                        "node.description",
                        props.get("node.name", "?")))
            except Exception:
                continue
        return devices, sources

    def _list_sinks(self):
        """Names of the system's current audio OUTPUTS (sinks)."""
        try:
            out = subprocess.run(["pactl", "list", "short", "sinks"],
                                 capture_output=True, text=True,
                                 timeout=8,
                                 env=self._audio_env()).stdout
            sinks = [ln.split()[1] for ln in out.splitlines()
                     if len(ln.split()) > 1]
            if sinks:
                return sinks
        except Exception:
            pass
        # PipeWire-native fallback when pulseaudio-utils is absent
        sinks = []
        for obj in self._pw_dump():
            try:
                props = (obj.get("info") or {}).get("props") or {}
                if props.get("media.class") == "Audio/Sink":
                    n = props.get("node.name", "")
                    if n:
                        sinks.append(n)
            except Exception:
                continue
        return sinks

    def _pick_output_target(self):
        """The sink Dose speaks through: a USB speaker the moment
        it's plugged in, else any physical output that isn't the
        Pi's (usually silent) HDMI port. Chosen fresh so a speaker
        plugged in mid-session is used on the very next sentence.
        None = trust the system default. Fully independent of the
        microphone choice — separate USB units are fine."""
        now = time.time()
        cached = getattr(self, "_out_cache", None)
        if cached and now - cached[0] < 5:
            return cached[1]
        sinks = self._list_sinks()
        # user's explicit pick wins, if it's still present
        forced = getattr(self, "_forced_sink", None)
        if forced and forced in sinks:
            self._make_default(forced, "Audio/Sink", SINK_VOLUME)
            self._out_cache = (now, forced)
            return forced
        target = None
        for s in sinks:
            if self._is_usb_name(s):
                target = s
                break
        if target is None:
            physical = [s for s in sinks
                        if "hdmi" not in s.lower()
                        and "bluez" not in s.lower()]
            if physical and len(physical) < len(sinks):
                target = physical[0]
        if target:
            # make it the system default too, unmuted and audible
            self._make_default(target, "Audio/Sink", SINK_VOLUME)
        self._out_cache = (now, target)
        return target

    def _list_sources(self):
        """Names of real capture sources (speaker monitors excluded)."""
        try:
            out = subprocess.run(["pactl", "list", "short", "sources"],
                                 capture_output=True, text=True,
                                 timeout=8,
                                 env=self._audio_env()).stdout
            srcs = [ln.split()[1] for ln in out.splitlines()
                    if len(ln.split()) > 1
                    and ".monitor" not in ln.split()[1]]
            if srcs:
                return srcs
        except Exception:
            pass
        srcs = []
        for obj in self._pw_dump():
            try:
                props = (obj.get("info") or {}).get("props") or {}
                if props.get("media.class") == "Audio/Source":
                    n = props.get("node.name", "")
                    if n:
                        srcs.append(n)
            except Exception:
                continue
        return srcs

    def _pw_node_id(self, name, media_class):
        """PipeWire node id for a node name, via pw-dump."""
        for obj in self._pw_dump():
            try:
                props = (obj.get("info") or {}).get("props") or {}
                if (props.get("media.class") == media_class
                        and props.get("node.name") == name):
                    return obj.get("id")
            except Exception:
                continue
        return None

    def _make_default(self, target, media_class, boost, force=False):
        """Make a node the system default, unmuted, at the given
        gain — via pactl when present, else wpctl (ships with
        WirePlumber on every Pi OS install, so one of the two is
        always there).

        THREE process spawns, six if pactl is missing — and it was
        being run on the way to choosing an output, which happens at
        the start of every reply and then every few seconds while one
        is being spoken. Setting the default sink to the sink it
        already is, unmuting an unmuted sink and setting a volume to
        the value it already holds are three no-ops with a real cost
        on a Pi, and they sat directly in the path between a person
        finishing a sentence and Dose starting to answer.

        So an identical (target, class, boost) is skipped unless it has
        been a while. The re-apply window exists because these are
        SYSTEM settings — somebody can move the slider in the desktop
        mixer, and a station that never reasserted itself would go
        quiet with no way back short of a restart.
        """
        kind = ("source" if media_class == "Audio/Source" else "sink")
        key = (target, media_class, round(float(boost or 0), 3))
        if not force:
            seen = getattr(self, "_default_set", None)
            if seen is None:
                seen = self._default_set = {}
            when = seen.get(key)
            if when is not None and time.time() - when < DEFAULT_REAPPLY_AFTER:
                return
            seen[key] = time.time()
            # A different target for the same class supersedes the old
            # one — drop it so switching back re-applies immediately
            # instead of being skipped as "already done".
            for k in [k for k in seen
                      if k[1] == media_class and k[0] != target]:
                seen.pop(k, None)
        env = self._audio_env()
        got = False
        for cmd in (["pactl", "set-default-" + kind, target],
                    ["pactl", "set-%s-mute" % kind, target, "0"],
                    ["pactl", "set-%s-volume" % kind, target,
                     "%d%%" % int(boost * 100)]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=5,
                                   env=env)
                got = got or r.returncode == 0
            except Exception:
                pass
        if not got:
            nid = self._pw_node_id(target, media_class)
            if nid is not None:
                for cmd in (["wpctl", "set-default", str(nid)],
                            ["wpctl", "set-mute", str(nid), "0"],
                            ["wpctl", "set-volume", str(nid),
                             "%.2f" % boost]):
                    try:
                        subprocess.run(cmd, capture_output=True,
                                       timeout=5, env=env)
                    except Exception:
                        pass

    def _pick_input_target(self):
        """The capture source Dose listens through: the plugged-in
        USB mic, else any other physical (non-Bluetooth) mic. Made
        the system default, unmuted, gain-boosted — so every capture
        route hears the right microphone with zero setup. Fully
        independent of the speaker choice."""
        srcs = self._list_sources()
        target = None
        # 1) a real microphone source (never the speaker's input side)
        for s in srcs:
            if self._looks_like_mic(s):
                target = s
                break
        # 2) a USB source that isn't obviously the speaker
        if target is None:
            for s in srcs:
                if self._is_usb_name(s) and not self._looks_like_speaker(s):
                    target = s
                    break
        # 3) any USB source
        if target is None:
            for s in srcs:
                if self._is_usb_name(s):
                    target = s
                    break
        # 4) any physical (non-Bluetooth) source
        if target is None:
            physical = [s for s in srcs if "bluez" not in s.lower()]
            target = physical[0] if physical else None
        if target:
            # UNITY. No boost past 100%.
            #
            # This was 150% for any USB mic, chosen for a very quiet
            # C-Media mini capsule. Stacked on top of an ALSA capture
            # level of 100% and a software auto-gain, it is why speech
            # was arriving at nearly full scale: three separate boosts
            # multiplying each other. A microphone that hears properly
            # needs none of them, and an overdriven signal is harder to
            # recognise than a quiet one, not easier.
            self._make_default(target, "Audio/Source", SOURCE_VOLUME)
        return target

    def _engage_bt_mic(self):
        """Force Bluetooth cards into their headset (mic-capable)
        profile — AirPods stay in playback-only A2DP until asked."""
        try:
            out = subprocess.run(["pactl", "list", "cards", "short"],
                                 capture_output=True, text=True,
                                 timeout=8,
                                 env=self._audio_env()).stdout
        except Exception:
            return
        for line in out.splitlines():
            if "bluez" not in line:
                continue
            parts = line.split()
            card = parts[1] if len(parts) > 1 else None
            if not card:
                continue
            for prof in ("headset-head-unit", "headset_head_unit",
                         "handsfree_head_unit",
                         "headset-head-unit-cvsd"):
                try:
                    r = subprocess.run(
                        ["pactl", "set-card-profile", card, prof],
                        capture_output=True, timeout=8,
                        env=self._audio_env())
                    if r.returncode == 0:
                        time.sleep(0.8)   # let the source appear
                        return
                except Exception:
                    continue

    def _engage_bt_mic_pw(self):
        """Profile switch using PipeWire's own tools (pw-dump +
        pw-cli) — these ship with PipeWire itself, so this works
        even when pulseaudio-utils was never installed."""
        devices, _ = self._parse_pw_dump(self._pw_dump())
        for dev in devices:
            head = [p for p in dev["profiles"]
                    if "head" in (p["name"] or "").lower()]
            if not head or dev["id"] is None:
                continue
            idx = head[0]["index"]
            try:
                subprocess.run(
                    ["pw-cli", "set-param", str(dev["id"]),
                     "Profile",
                     '{ "index": %d, "save": true }' % idx],
                    capture_output=True, timeout=10,
                    env=self._audio_env())
                time.sleep(1.0)
                return True
            except Exception:
                continue
        return False

    def request_reopen(self):
        """Ask the capture loop to redo device selection now (used
        when the user picks a different microphone)."""
        self._force_reopen = True

    def mixer_summary(self):
        """One-line state of the active mic card's capture controls:
        e.g. 'Mic 100%[on] · Capture 0%[off]'. Reveals whether a
        control is muted or at zero (software fix) vs. the mixer being
        fine while the device is silent (hardware). '' if unknown."""
        card = getattr(self, "mic_card", None)
        if card is None:
            return ""
        cache = getattr(self, "_mix_cache", None)
        if cache and cache[0] == card and time.time() - cache[1] < 1.0:
            return cache[2]
        try:
            out = subprocess.run(["amixer", "-c", str(card)],
                                 capture_output=True, text=True,
                                 timeout=6, env=self._audio_env()).stdout
        except Exception:
            return ""
        parts = []
        name = None
        for ln in (out or "").splitlines():
            s = ln.strip()
            m = re.match(r"Simple mixer control '([^']+)'", s)
            if m:
                name = m.group(1)
                continue
            if name and ("Capture" in s and "%" in s):
                pm = re.search(r"\[(\d+)%\].*?\[(on|off)\]", s)
                if pm and any(k in name.lower() for k in
                              ("mic", "capture", "input", "adc")):
                    parts.append("%s %s%%[%s]"
                                 % (name, pm.group(1), pm.group(2)))
                    name = None
        summ = " · ".join(parts[:4])
        self._mix_cache = (card, time.time(), summ)
        return summ

    def request_listen(self):
        """Start a listening session right now without the wake word —
        wired to holding the Dose logo. Returns True if the engine is
        running and will listen, False if voice isn't available."""
        if not self.available:
            return False
        self._ptt_requested = True
        return True

    def list_capture_devices(self):
        """All recordable devices as [(card, device, short_name,
        is_mic)], for the on-screen mic picker."""
        out = []
        try:
            for card, dev, desc in self._alsa_capture_cards():
                short = desc.split("[")[0].strip() or desc[:28]
                out.append((card, dev, short[:34],
                            self._looks_like_mic(desc)
                            and not self._card_has_playback(card)))
        except Exception:
            pass
        return out

    def force_card(self, card, device):
        """User picked a specific mic in Settings — use exactly it and
        reopen capture now."""
        self._forced_card = (int(card), int(device))
        self.request_reopen()

    def list_output_devices(self):
        """All audio OUTPUTS as [(sink_name, short_label)], for the
        on-screen speaker picker."""
        out = []
        for s in self._list_sinks():
            short = s
            for p in ("alsa_output.", "bluez_output."):
                if short.startswith(p):
                    short = short[len(p):]
            out.append((s, short[:40]))
        return out

    def force_sink(self, sink):
        """User picked a specific speaker — make it the output and
        play everything through it from now on."""
        self._forced_sink = sink
        self._out_cache = None
        try:
            # force: a person just tapped this speaker in Settings. If
            # the skip-if-recent cache swallowed it, their choice would
            # appear to do nothing at all.
            self._make_default(sink, "Audio/Sink", SINK_VOLUME,
                               force=True)
        except Exception:
            pass

    def _arecord_probe(self, card, device, seconds=2.5):
        """Record DIRECTLY from one capture device and return
        (rms, note). rms>0 = real audio, 0 = silence, -1 = every open
        failed. Tries several device spellings AND rates, because on a
        PipeWire Pi raw plughw can be busy/silent while the ALSA
        'default'/'sysdefault' paths (which go through PipeWire) work."""
        note = "no capture"
        devs = ["plughw:%d,%d" % (card, device),
                "plughw:%d,1" % card,   # some mics capture on subdev 1
                "hw:%d,%d" % (card, device),
                "sysdefault:CARD=%d" % card,
                "default"]
        import audioop
        for dev in devs:
            for rate in (48000, 44100, 16000):
                fd, path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                try:
                    r = subprocess.run(
                        ["arecord", "-D", dev, "-f", "S16_LE",
                         "-r", str(rate), "-c", "1",
                         "-d", str(int(seconds)), path],
                        capture_output=True, text=True,
                        timeout=seconds + 6, env=self._audio_env())
                    if r.returncode != 0:
                        e = (r.stderr or "").strip().splitlines()
                        note = e[-1][:80] if e else "arecord failed"
                        continue
                    with wave.open(path) as w:
                        data = w.readframes(w.getnframes())
                    rms = audioop.rms(data, 2) if data else 0
                    return (rms, "%s @%dHz" % (dev, rate))
                except Exception as e:
                    note = str(e)[:80]
                finally:
                    try:
                        os.unlink(path)
                    except Exception:
                        pass
        return (-1, note)

    def full_mic_test(self, seconds=2.5):
        """THE definitive test: pause our capture, then directly
        arecord from EVERY capture device the system exposes, measure
        the real signal on each, and pick the one that actually hears.
        Writes a full report and, if a device produced sound, forces
        it as the mic. Returns (best_summary, report_lines)."""
        report = ["DOSE full mic test", time.ctime(), ""]
        # 1) full device inventory
        for cmd in (["lsusb"], ["arecord", "-l"]):
            try:
                out = subprocess.run(cmd, capture_output=True,
                                     text=True, timeout=8,
                                     env=self._audio_env())
                report.append("$ " + " ".join(cmd))
                report.append((out.stdout or out.stderr).strip()[:600])
                report.append("")
            except FileNotFoundError:
                report.append("$ %s -> not installed" % " ".join(cmd))
            except Exception as e:
                report.append("$ %s -> %r" % (" ".join(cmd), e))
        # 2) pause the live capture so devices are free to test
        self._pause_capture = True
        # wait until the capture loop confirms it released the device
        # (so the direct probe doesn't hit 'device busy')
        for _ in range(30):
            if getattr(self, "_paused_ack", False):
                break
            time.sleep(0.1)
        time.sleep(0.4)
        best = None      # (rms, card, device, desc)
        try:
            cards = self._alsa_capture_cards()
            report.append("Testing each capture device (speak now!):")
            for card, device, desc in cards:
                # force: a person is watching this test and a
                # cached skip would report a stale card state.
                self._max_capture(card, force=True)
                rms, note = self._arecord_probe(card, device, seconds)
                kind = ("MIC" if self._looks_like_mic(desc) else
                        "speaker-in" if self._looks_like_speaker(desc)
                        else "capture")
                report.append(
                    "  card %d,%d [%s] %s -> level %s (%s)"
                    % (card, device, kind, desc.split("[")[0][:34],
                       rms, note))
                if rms is not None and rms >= 0:
                    if best is None or rms > best[0]:
                        best = (rms, card, device, desc)
        finally:
            self._pause_capture = False
        # 3) act on the result
        if best and best[0] > 8:
            self._forced_card = (best[1], best[2])
            self.request_reopen()
            summary = ("Found the working mic: card %d,%d (level %d). "
                       "Using it now — tap MIC LEVEL and speak."
                       % (best[1], best[2], best[0]))
        elif best is not None:
            # a device opened but was silent
            self._forced_card = (best[1], best[2])
            self.request_reopen()
            summary = ("Every mic opened but stayed silent (best card "
                       "%d,%d level %d). The mic isn't sending audio — "
                       "check it's a MIC not line-in, reseat it, or try "
                       "another USB port." % (best[1], best[2], best[0]))
        else:
            summary = ("No capture device could even be opened — see "
                       "the report; arecord may be missing or the mic "
                       "isn't detected.")
        report.insert(3, "VERDICT: " + summary)
        report.insert(4, "")
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            with open(os.path.join(VOICE_DIR, "full_mic_test.txt"),
                      "w") as f:
                f.write("\n".join(report))
        except Exception:
            pass
        return summary, report

    def speaker_test(self):
        """Play a short spoken line on the current speaker. Returns
        the playback path used, or False if nothing could play."""
        try:
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            self._synth(
                None,
                "Speaker test. If you can hear me, Ryan, this "
                "speaker is working.", path)
            method = self._play_wav(path)
            os.unlink(path)
            return method
        except Exception:
            return self._play_wav(
                "/usr/share/sounds/alsa/Front_Center.wav")

    def _heartbeat(self):
        """Write what the engine is doing RIGHT NOW, every second.

        WHY THIS EXISTS. voice/selection.txt is written once per device
        selection, and I spent an evening reading it as if it were live:
        quoting "audio blocks delivered since start: 0" from a file
        written at startup while the microphone was, at that moment,
        streaming perfectly — arecord's wchar climbing at exactly
        96000 B/s and the app's rchar climbing with it.

        A snapshot read as a live value is worse than no value. This
        file is always current, so "is it hearing me" is answerable in
        one second, by anyone, without attaching a profiler.

        Cheap on purpose: a handful of integers, one atomic replace,
        once a second, inside a bare except. It must never be able to
        affect the audio path it reports on.
        """
        try:
            now = time.time()
            blocks = getattr(self, "_blocks_in", 0)
            prev_b, prev_t = getattr(self, "_hb_prev", (0, now))
            dt = max(0.001, now - prev_t)
            rate = (blocks - prev_b) / dt
            self._hb_prev = (blocks, now)
            last = getattr(self, "_last_block_ts", 0.0)
            lines = [
                "state:          %s" % getattr(self, "state", "?"),
                "muted:          %s" % getattr(self, "_muted", "?"),
                # getattr, not self.mic_name. An engine that has not
                # finished starting has no mic_name yet, and a bare
                # AttributeError in here is swallowed by the except
                # below — producing NO heartbeat file at all, silently,
                # at precisely the moment somebody is asking why the
                # station is not listening. Every other line in this
                # list already reads defensively; this one did not, and
                # it is the instrument, not the patient.
                "mic:            %s" % (getattr(self, "mic_name", None)
                                        or "?"),
                "",
                "HEARING:        %s" % (
                    "YES" if rate > 0.5 else "NO — no audio arriving"),
                "blocks/sec:     %.1f" % rate,
                "blocks total:   %d" % blocks,
                "last block:     %.1fs ago" % (
                    (now - last) if last else -1),
                "live level:     peak %d  rms %d" % (
                    getattr(self, "_hb_peak", 0),
                    getattr(self, "mic_rms", 0)),
                "block bytes:    %d   non-zero in first 64: %d" % (
                    getattr(self, "_hb_bytes", 0),
                    getattr(self, "_hb_nz", 0)),
                "native rate:    %s   backend: %s" % (
                    getattr(self, "_native_rate", "?"),
                    getattr(self, "mic_name", "?")),
                "",
                "heard so far:   %r" % (
                    str(getattr(self, "_partial", ""))[:80]),
                "last reply:     %r" % (
                    str(getattr(self, "_last_reply", ""))[:80]),
                "",
                # Two different numbers, because they mean two
                # different things. "reopens" is the microphone dying
                # on us; "closed on purpose" is us ending a recorder
                # deliberately, during a selection or a re-open. They
                # used to be the same number, and a run of 332 reopens
                # turned out to be 332 of the second kind — our own
                # teardown, reported by the reader as a death, which
                # set _force_reopen, which brought the loop back to
                # tear down the replacement.
                "capture reopens: %d   closed on purpose: %d" % (
                    getattr(self, "_capture_restarts", 0),
                    getattr(self, "_capture_closes", 0)),
                # WHERE IS THE RECOGNISING ACTUALLY HAPPENING?
                #
                # Ryan asked for confirmation that the models run on
                # the Mac and not on the Pi, and the only honest answer
                # was "no, and here is why" — because nothing on this
                # station reported it. A claim that cannot be checked
                # by reading a file is a claim somebody has to take on
                # trust, and this project has already learned what that
                # costs.
                self._remote_line(),
                # THE CACHE IS THE WHOLE OF TIME-TO-FIRST-SOUND NOW,
                # AND IT KEPT DISAPPEARING.
                #
                # 126 clips before a restart, 0 after. Twice more
                # before that. Every restart threw away minutes of
                # synthesis, and I spent two jobs reasoning about which
                # of three deleters it was while the program could
                # simply have said so. Same lesson as
                # voice/selection.txt: ask the program, do not model
                # it.
                self._cache_line(),
                self._vad_line(),
                # The fault this station actually had: blocks arriving
                # on time, every sample zero. "HEARING: YES" above is
                # about the DEVICE; this line is about the SIGNAL.
                "signal:         %s" % (
                    "live"
                    if getattr(self, "_hb_peak", 0) > 0
                    else "SILENT for %.0fs (acts at %.0fs)" % (
                        now - (getattr(self, "_last_live_peak_ts", 0)
                               or now),
                        SILENT_CAPTURE_AFTER)),
                "blocks with signal: %d of %d" % (
                    getattr(self, "_hb_live_blocks", 0),
                    getattr(self, "_blocks_in", 0)),
                "silence recoveries: %d   next rung: %d" % (
                    getattr(self, "_silence_recoveries", 0),
                    getattr(self, "_silence_step", 0)),
                "written:        %s" % time.strftime("%H:%M:%S"),
            ]
            os.makedirs(VOICE_DIR, exist_ok=True)
            tmp = os.path.join(VOICE_DIR, "live.txt.tmp")
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, os.path.join(VOICE_DIR, "live.txt"))
        except Exception:
            pass

    def _note_reopen(self, why):
        """Record WHY the capture is about to be torn down and reopened.

        There are four separate things in the supervising loop that can
        trigger a reopen, and until now all four looked identical from
        the outside: the reopen counter went up. Knowing that a station
        re-selected twelve times in ten minutes is not actionable.
        Knowing that all twelve said "mic_name says no signal" is.
        """
        try:
            lst = getattr(self, "_reopen_why", None)
            if lst is None:
                lst = self._reopen_why = []
            lst.append("%s  %s" % (time.strftime("%H:%M:%S"), why))
            del lst[:-20]
        except Exception:
            pass

    def _dump_selection(self, chosen, is_live, started):
        """Write what device selection just decided, to disk, every time.

        WHY THIS EXISTS. mic_report() is excellent and is only ever
        called when somebody taps a button on the touchscreen. This
        station runs unattended, and on 2026-09-18 the report on its
        disk was three days old while the engine had reopened its
        microphone dozens of times in the previous ten minutes. Every
        question about which route it chose, and why it did not keep it,
        had to be answered by attaching py-spy to a live process and
        reading C stack frames.

        That is an absurd way to find out something the program already
        knows. It knows the answer at exactly this moment — it has just
        finished deciding — so it writes it down. One small file,
        rewritten per selection, with a counter so a station that is
        re-selecting in a loop says so on the first line instead of
        looking identical to one that settled immediately.

        Deliberately cheap and deliberately total: no exception from
        here may ever reach the capture path. A diagnostic that can
        break the thing it is diagnosing is worse than no diagnostic.
        """
        try:
            self._sel_count = getattr(self, "_sel_count", 0) + 1
            took = time.time() - (started or time.time())
            lines = [
                "DOSE capture selection",
                time.ctime(),
                "",
                "selection #%d since start" % self._sel_count,
                "took: %.1fs (budget %.0fs)" % (took, CAPTURE_OPEN_BUDGET),
                "chose: %s" % (chosen or "NOTHING — no route opened"),
                "verdict: %s" % (
                    "a route showed a real noise floor"
                    if is_live else
                    "NO route showed signal; fell back to the first that "
                    "merely opened"),
                "threshold: peak >= %d" % ROUTE_LIVE_PEAK,
                "engine state during the walk: %s   muted: %s"
                % (getattr(self, "state", "?"),
                   getattr(self, "_muted", "?")),
                "audio blocks delivered since start: %d"
                % getattr(self, "_blocks_in", 0),
                "",
                "every route tried, in order:",
            ]
            for t in getattr(self, "mic_trail", []) or ["(none)"]:
                lines.append("  " + str(t))
            lines += [
                "",
                "capture reopens since start: %d"
                % getattr(self, "_capture_restarts", 0),
                "probe closes that never returned: %d"
                % len(getattr(self, "_probe_wedged", [])),
                "device scans that hit their budget: %d"
                % getattr(self, "_probe_timeouts", 0),
                "route walks that hit their budget: %d"
                % getattr(self, "_select_timeouts", 0),
                "",
                "why each reopen happened (most recent last):",
            ]
            why = getattr(self, "_reopen_why", []) or []
            lines.extend("  " + str(w) for w in why[-20:])
            if not why:
                lines.append("  (no reopen yet — this is the first "
                             "selection)")
            lines += ["", "recorder stderr (last lines):"]
            errs = getattr(self, "_capture_errs", []) or []
            lines.extend("  " + str(e) for e in errs[-20:])
            if not errs:
                lines.append("  (none)")
            os.makedirs(VOICE_DIR, exist_ok=True)
            tmp = os.path.join(VOICE_DIR, "selection.txt.tmp")
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, os.path.join(VOICE_DIR, "selection.txt"))
        except Exception:
            pass

    def mic_report(self):
        """Write a full microphone diagnostic to voice/mic_report.txt
        and return a one-line human verdict. Called when a mic test
        reads silence, so we can see exactly what the audio system
        is exposing."""
        self._kick_audio_services(force=True)
        # force: this is the diagnostic somebody taps when the mic
        # is misbehaving. It must reflect the hardware NOW.
        self._unmute_alsa_inputs(force=True)
        lines = ["DOSE mic report", time.ctime(), ""]
        lines.append("chosen backend: %s (rms %s)"
                     % (self.mic_name, self.mic_rms))
        for tline in getattr(self, "mic_trail", []):
            lines.append("route: " + tline)
        # HOW OFTEN HAS THE MICROPHONE HAD TO BE REOPENED, AND WHY.
        # A station quietly restarting its capture dozens of times an
        # hour is a fault, and it is invisible unless somebody counts.
        # The recorder's own stderr is kept alongside it — that is the
        # only place that says "overrun!!!" or "Device or resource
        # busy", and it used to go to /dev/null.
        lines.append("")
        lines.append("capture reopens since start: %d"
                     % getattr(self, "_capture_restarts", 0))
        errs = getattr(self, "_capture_errs", [])
        lines.append("recorder stderr (last %d lines):" % len(errs))
        if errs:
            lines.extend("  " + e for e in errs)
        else:
            lines.append("  (none — the recorder has not complained)")
        # DEVICE SELECTION HEALTH. A wedged stream close is the fault
        # that made this station deaf for months: the engine parked in
        # PortAudio's Pa_StopStream mid-probe and never came out, so it
        # never finished choosing a microphone. It cannot hang any more,
        # but if it starts wedging again that has to be visible here
        # rather than inferred from a py-spy dump months later.
        wedged = getattr(self, "_probe_wedged", [])
        lines.append("probe closes that never returned: %d" % len(wedged))
        for w in wedged:
            lines.append("  " + w)
        lines.append("device scans that hit the %.0fs budget: %d"
                     % (PROBE_PICK_BUDGET,
                        getattr(self, "_probe_timeouts", 0)))
        lines.append("route walks that hit the %.0fs budget: %d"
                     % (CAPTURE_OPEN_BUDGET,
                        getattr(self, "_select_timeouts", 0)))
        lines.append("")
        try:
            for i, d in enumerate(self._sd.query_devices()):
                if d.get("max_input_channels", 0) > 0:
                    lines.append("portaudio input %d: %s (%s Hz)"
                                 % (i, d.get("name"),
                                    d.get("default_samplerate")))
        except Exception as e:
            lines.append("portaudio query failed: %r" % (e,))
        env = self._audio_env()
        lines.append("uid=%s XDG_RUNTIME_DIR=%s"
                     % (os.getuid(), env.get("XDG_RUNTIME_DIR")))
        # ALSA capture cards straight from the kernel — the arecord
        # path depends only on these, not on PipeWire
        for num, devn, desc in self._alsa_capture_cards():
            tag = ("  <-- MIC" if self._looks_like_mic(desc) else
                   "  (speaker input)" if self._looks_like_speaker(desc)
                   else "")
            lines.append("recordable card %d,%d: %s%s"
                         % (num, devn, desc[:100], tag))
            # the actual mixer controls + levels on this card, so we
            # can see if a capture control is muted or at zero
            try:
                mx = subprocess.run(["amixer", "-c", str(num)],
                                    capture_output=True, text=True,
                                    timeout=6, env=env).stdout
                for ml in (mx or "").splitlines():
                    s = ml.strip()
                    if (s.startswith("Simple mixer control")
                            or "Capture" in s or "Mono:" in s
                            or "Front Left:" in s or "Limits" in s):
                        lines.append("   " + s[:110])
            except Exception:
                pass
        for cmd in (["systemctl", "--user", "is-active", "pipewire",
                     "pipewire-pulse", "wireplumber"],
                    ["arecord", "-l"],
                    ["arecord", "--version"],
                    ["pactl", "info"],
                    ["pactl", "list", "cards", "short"],
                    ["pactl", "list", "sources", "short"],
                    ["pw-record", "--version"],
                    ["parec", "--version"]):
            try:
                r = subprocess.run(cmd, capture_output=True,
                                   text=True, timeout=8, env=env)
                lines.append("$ " + " ".join(cmd))
                lines.append((r.stdout or r.stderr).strip()[:800])
            except FileNotFoundError:
                lines.append("$ %s -> (not installed)"
                             % " ".join(cmd))
            except Exception as e:
                lines.append("$ %s -> %r" % (" ".join(cmd), e))
        report = "\n".join(lines)
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            with open(os.path.join(VOICE_DIR, "mic_report.txt"),
                      "w") as f:
                f.write(report)
        except Exception:
            pass

        # Decisive facts straight from PipeWire itself
        devices, sources = self._parse_pw_dump(self._pw_dump())
        lines2 = ["", "pw-dump summary:"]
        for d in devices:
            lines2.append("  bt device: %s profiles=%s" % (
                d["name"], [p["name"] for p in d["profiles"]]))
        for s in sources:
            lines2.append("  audio source: %s" % s)
        for s in self._list_sinks():
            lines2.append("  audio output: %s" % s)
        lines2.append("  speaker target: %s"
                      % (self._pick_output_target() or "system default"))
        try:
            with open(os.path.join(VOICE_DIR, "mic_report.txt"),
                      "a") as f:
                f.write("\n".join(lines2))
        except Exception:
            pass

        # Every silent-mic verdict forces a full USB-first
        # reselection and has the user retest. Verdicts are kept
        # SHORT so they always fit on screen without truncating —
        # the long story lives in the DETAILS report.
        usb_src = [s for s in self._list_sources()
                   if self._is_usb_name(s)]
        self._pick_input_target()
        self.request_reopen()
        if self._is_usb_name(self.mic_name):
            return "USB mic silent — gain boosted, RETEST + speak"
        if usb_src:
            return "reconnecting USB mic — RETEST in 5 sec"
        try:
            has_inputs = any(
                d.get("max_input_channels", 0) > 0
                and self._is_usb_name(d.get("name"))
                for d in self._sd.query_devices())
        except Exception:
            has_inputs = False
        if has_inputs:
            return "reconnecting USB mic — RETEST in 5 sec"
        return "no USB mic found — reseat the plug, RETEST"

    def mic_meter_sample(self, seconds=0.3):
        """One short sample for the LIVE level meter: returns
        (raw, boosted) peak RMS from the running capture. raw = what
        the microphone physically delivers (0 = truly nothing);
        boosted = what Vosk receives after auto-gain. Cheap and safe
        to call repeatedly for a moving bar."""
        if not self.available:
            return (0, 0)
        self._level_probe = {"until": time.time() + seconds,
                             "max": 0, "raw": 0}
        time.sleep(seconds + 0.1)
        p = self._level_probe
        self._level_probe = None
        if not p:
            return (0, 0)
        return (int(p.get("raw", 0)), int(p.get("max", 0)))

    def mic_level(self, seconds=2.0):
        """Live mic test for the Settings screen: taps the RUNNING
        capture backend (whatever is actually feeding recognition)
        and returns the peak level heard. 0 = dead mic."""
        if self.available:
            self._level_probe = {"until": time.time() + seconds,
                                 "max": 0, "raw": 0}
            time.sleep(seconds + 0.4)
            probe = self._level_probe
            self._level_probe = None
            return probe["max"] if probe else 0
        # engine not running: direct one-off probe
        try:
            import audioop
            frames = []

            def cb(indata, f, t, s):
                frames.append(bytes(indata))
            st = self._sd.RawInputStream(
                device=getattr(self, "mic_index", None),
                samplerate=getattr(self, "_native_rate", SAMPLE_RATE),
                blocksize=1024, dtype="int16", channels=1, callback=cb)
            st.start()
            time.sleep(seconds)
            # Same non-blocking teardown as the probe — this runs on the
            # UI's thread when the Settings mic test is tapped, and a
            # Pa_StopStream that never returns would freeze the whole
            # touchscreen, not just the microphone.
            self._shut_stream(st, "mic_level")
            data = b"".join(frames)
            return audioop.max(data, 2) if data else 0
        except Exception:
            return -1

    # ── lifecycle ─────────────────────────────────────────────────────
    def start(self):
        if not self.available:
            return
        # CLEAR THE LATCH FIRST.
        #
        # _stop is a threading.Event created once and, until now, never
        # cleared. The capture reader's loop is
        #
        #     while proc.poll() is None and not self._stop.is_set():
        #
        # so ONE call to stop() or release_audio() makes every recorder
        # started afterwards exit before its first read. The station
        # then runs normally in every visible respect — arecord alive,
        # card5 RUNNING, hw_ptr advancing — and delivers zero audio to
        # the engine, for ever, until the process is restarted.
        #
        # That is exactly what the device reported:
        #
        #     audio blocks delivered since start: 0
        #     arecord FORCED card 5,0: opened, peak 0 (rms 0, 0 blocks)
        #
        # while the identical arecord command, run standalone on the
        # same hardware seconds later, delivered 200 blocks in 4
        # seconds with a peak of 7388. The audio stack was never the
        # problem.
        #
        # A latch that outlives the thing it was meant to stop is not a
        # stop signal, it is a fuse.
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()
        # THE HEARTBEAT GETS ITS OWN THREAD.
        #
        # It used to be written from the top of the supervising loop,
        # which means it stopped being written for the whole of a turn:
        # _listen_command() does not return until the person has
        # finished speaking and the answer has been worked out. So
        # live.txt froze at "state: idle" for twenty seconds at exactly
        # the moment somebody would be reading it to find out whether
        # the station was listening.
        #
        # It cost me a measurement run — the harness waited for the
        # heartbeat to say "listening", the turns happened, and the file
        # never said so — and it would cost anyone else the same, in the
        # one file written specifically so that nobody has to guess.
        #
        # Its own thread, one write a second, whatever the engine is
        # doing. Still inside a bare except, still an atomic replace,
        # still unable to touch the audio path.
        threading.Thread(target=self._heartbeat_loop, daemon=True,
                         name="heartbeat").start()

    def _heartbeat_loop(self):
        """Write voice/live.txt once a second, for as long as we run.

        Also keeps the Mac's availability fresh. This thread is the
        right place for it: it already runs once a second, it is never
        on a turn's critical path, and dose_remote_stt.probe() rate
        limits itself, so the cost is one small GET every twenty
        seconds while the station is idle.
        """
        while not self._stop.is_set():
            try:
                self._heartbeat()
            except Exception:
                pass
            try:
                self._remote_probe_tick()
            except Exception:
                pass
            self._stop.wait(1.0)

    def stop(self):
        self._stop.set()

    def set_muted(self, muted):
        self._muted = bool(muted)

    # ── UI bridge (all Tk work happens on the main thread) ────────────
    def _ui(self, fn, *args):
        """Schedule fn on the Tk main thread; wait for its result."""
        done = threading.Event()
        box = {}

        def run():
            try:
                box["r"] = fn(*args)
            except Exception as e:
                box["e"] = e
            done.set()

        try:
            self.app.root.after(0, run)
        except Exception:
            return None
        done.wait(timeout=5)
        return box.get("r")

    def _set_ui_state(self, state, user_text="", reply_text=""):
        """Tell the screen what the engine is doing.

        DO NOT POST A REPAINT THAT CHANGES NOTHING. This is called from
        the listening loop for EVERY audio block, and Vosk hands back
        the same partial over and over while somebody is mid-word or
        drawing breath — so the overlay was being asked to redraw
        identical text many times a second.

        Each of those is a root.after(0, ...) onto the Tk event queue,
        and that same queue is what drives the overlay's own animation.
        Flooding it with no-op repaints is precisely how a panel ends up
        feeling sluggish while the machine looks idle, which is the
        complaint: "none of the words show up fast at all".

        The overlay is a pure function of (state, user text, reply
        text), so an identical triple has nothing to say. Skipping it
        also FIXES a small correctness bug for free: the app stamps
        _voice_last_change on every call and times the panel's minimum
        visible period from it, so repeats of unchanged text were
        pushing that moment forward and making the panel linger.

        self.state is still assigned every time — it is read all over
        the engine and must never lag behind.
        """
        self.state = state
        sig = (state, user_text, reply_text)
        if sig == getattr(self, "_ui_sig", None):
            return
        self._ui_sig = sig
        try:
            self.app.root.after(
                0, self.app._voice_overlay_update, state, user_text,
                reply_text)
        except Exception:
            pass

    # ── audio input ───────────────────────────────────────────────────
    # Comforting, warm, protective — a gentle guardian, softly reassuring.
    # What she says the moment she starts listening. Just "Ready." —
    # it is the fastest possible acknowledgement and it gets out of the
    # way. The old lines ("I'm here for you, always", "I've got you")
    # were reassurance nobody asked for at the start of every single
    # exchange, which reads as unsettling rather than warm.
    ACKS = ("Ready.",)

    def _prime_speech(self):
        """Load Piper up front and pre-render the short acknowledgment
        lines to wav files, so the reply to 'Hey Dose' starts as fast
        as a person would answer.

        These go through the SAME voice-keyed cache as everything else.
        They used to be keyed on the line text alone, which meant a
        clip rendered in an older voice was found on disk, reused, and
        played forever — the acknowledgements are the first thing you
        hear, so that was the one place an old voice could survive
        every other cleanup. Now the voice is part of the key, so a
        clip in any other voice simply cannot be selected."""
        try:
            self._purge_foreign_cache()
            # No eager _load_piper() here. render_to_cache() below goes
            # through _synth(), which uses the worker and only builds an
            # in-process session if the worker declines.
            self._ack_files = []
            for line in self.ACKS:
                path = self.render_to_cache(line)
                if path:
                    self._ack_files.append((line, path))
        except Exception:
            self._ack_files = []
        # everything else she can say, rendered in the background
        try:
            self.prewarm_replies()
        except Exception:
            pass
        # warm the recognisers, so the FIRST thing said isn't the one
        # that pays for loading model weights
        try:
            self.warm_models()
        except Exception:
            pass
        # watch the microphone level and turn it down if it saturates
        try:
            self.watch_input_level()
        except Exception:
            pass
        # a calibration measured on a previous run still applies
        try:
            self.load_calibration()
        except Exception:
            pass
        # hardware tuning (governor, core budget) — no root needed for
        # the parts that matter, and harmless everywhere else
        try:
            self._tuned = tune_for_pi()
        except Exception:
            self._tuned = []
        # fetch the free upgraded models (retries until present)
        try:
            self.ensure_upgraded_models()
        except Exception:
            pass

    # Every fixed sentence the engine can say. Pre-rendered in the
    # background so these ALWAYS start playing instantly (the build
    # guide's core latency trick: never synthesize at reply time).
    def _fixed_lines(self):
        lines = list(self.ACKS)
        lines += [
            "Standing by, Ryan.",
            "I didn't catch that, Ryan. Hold the logo and try again.",
            "No response received. Standing by, Ryan.",
            "Acknowledged. Standing by.",
            "Understood, Ryan.",
            "Cancelling. I will be here.",
            "Safety protocol, Ryan: I cannot give medical advice. "
            "Never change a dose on your own — please contact your "
            "pharmacist or doctor. Protocol three: protect the patient.",
            "I am sorry you are feeling that way, Ryan. I cannot tell "
            "you what is causing it — please call your pharmacist or "
            "doctor, and tell them what you have taken today. If it is "
            "severe, if you are struggling to breathe, or if you think "
            "you have taken too much, call 9 1 1, or poison control at "
            "1 800, 2 2 2, 1 2 2 2, right now.",
            "This sounds like an emergency, Ryan. I am only an "
            "assistant — please call 9 1 1, or your local emergency "
            "number, right now. Poison control in the U S is "
            "1 800, 2 2 2, 1 2 2 2.",
            "I am really glad you told me, Ryan. You do not have to go "
            "through this alone. You can call or text 9 8 8, the "
            "Suicide and Crisis Lifeline, any time, to talk with "
            "someone right now. If you are in danger, please call 9 1 1.",
            "Nothing further is scheduled today, Ryan. Rest easy.",
            "Okay, Ryan.", "Understood.", "Got it.", "Right you are.",
            "Okay.", "No problem, Ryan.", "Hello, Ryan.",
            "Hi Ryan — what do you need?", "Morning, Ryan.",
            "Goodbye, Ryan.", "No need to apologise, Ryan.", "Go on.",
            "Instruction unclear. Standing by.",
            "Home screen, Ryan.",
            "Opening storage.",
            "Opening settings.",
            "Here is your record, Ryan.",
        ]
        # "did you mean <med>?" for every loaded medication
        for n in self._med_names():
            lines.append("I want to be certain before I answer, Ryan — "
                         "did you mean %s?" % n)
        # THE INVARIANT OPENINGS OF THE VARIABLE REPLIES.
        #
        # Most of what this station says is assembled at the moment it
        # answers — an inventory, a time, a medication name — so the
        # whole line can never be cached. But the OPENING of several of
        # them never changes, and the opening is the only part on the
        # critical path: the rest renders on a worker while it plays.
        #
        # "Current inventory: New Medication, 26; Metformin, 12."
        # splits to "Current inventory:" — eighteen characters that are
        # identical every single time somebody asks. Rendering it once
        # at startup turns the slowest reply the station has into one
        # that starts speaking immediately.
        # ONE LIST, TWO USERS. These are the same openings _split_first()
        # is allowed to break after — declared once, at the top of the
        # file, so an opening the chunker can produce is always an
        # opening the cache holds. Two lists would drift, and the
        # failure would be silent: a fragment rendered from scratch on
        # the critical path, every single time, for as long as nobody
        # noticed.
        lines += list(INVARIANT_OPENINGS)
        lines += [
            "No medications are in view today, Ryan.",
            "Nothing further is scheduled today, Ryan.",
            "Yes, Ryan.", "No, Ryan.",
            # First sentences of replies built with an f-string, so
            # _spoken_constants() cannot see them but the chunker
            # splits here every time. The device measured
            # "Negative, Ryan. New Medication has not been dispensed
            # today." at speak 1.95 — all of it in the first fifteen
            # characters, which never change.
            "Negative, Ryan.",
            "Partially.",
        ]
        seen, out = set(), []
        for ln in lines:
            if ln and ln not in seen:
                seen.add(ln)
                out.append(ln)
        return out

    def hardware_report(self):
        """Rows for the Settings page: what the Pi is doing, and
        whether anything about it is costing us response time."""
        h = pi_health()
        rows = [
            ("CPU", "%d cores · %d for speech" % (h["cores"],
                                                  h["infer_threads"]), True),
            # what EVERYTHING on this Pi is asking for, not just us:
            # the UI, the QR camera, PipeWire and the desktop all count
            ("Load", "%.2f (%.0f%% of %d cores)"
             % (h["load"], h["load_per_core"] * 100, h["cores"]),
             h["load_per_core"] < 0.9),
            ("Free RAM", "%d MB" % h["mem_free_mb"],
             h["mem_free_mb"] > 200),
            # A cool-idle governor is the GOAL now, not a warning: it
            # sits low when nothing is happening and ramps under load.
            # Only "could not change" or a wedged-low clock is a problem.
            ("Clock", "%d MHz (%s)" % (h["mhz"], h["governor"] or "?"),
             h["governor"] in ("schedutil", "ondemand", "conservative",
                               "performance") or h["mhz"] >= 1400),
            ("Temp", "%.1f °C" % h["temp_c"], h["temp_c"] < 75),
            ("Throttling", "yes — the Pi is being slowed down"
             if h["throttled"] else "no", not h["throttled"]),
            ("Power", "UNDER-VOLTAGE — use a 3 A supply"
             if h["under_voltage"] else "ok", not h["under_voltage"]),
            ("OS", "64-bit" if h["arch64"]
             else "32-bit (64-bit is ~30% faster)", h["arch64"]),
            ("Scratch audio", "RAM (/dev/shm)"
             if TMP_AUDIO_DIR == "/dev/shm" else TMP_AUDIO_DIR,
             TMP_AUDIO_DIR == "/dev/shm"),
            ("Voice", os.path.basename(self._piper_path or "—"), True),
            # What the room sounds like right now. When someone says
            # "it can't hear me", this is the first thing to look at:
            # a high floor means the room is the problem, not the
            # software or even the microphone.
            ("Room noise", self._room_note(), self._nfloor < 250),
            ("Voice detector", self._vad_note(),
             bool(self._vad_trusted
                  and getattr(self, "_vad", None) not in (None, "unset"))),
            ("Talk over me", "yes — it stops" if BARGE_IN else "off",
             BARGE_IN),
            ("Endpoint", "%.2fs done · %.1fs mid-thought"
             % (ENDPOINT_STABLE, ENDPOINT_DANGLING),
             ENDPOINT_STABLE <= 0.5),
        ]
        return rows

    # Speech at or above this RMS needs no help at all.
    HEALTHY_SPEECH_RMS = 1400.0

    def _agc_ceiling(self):
        """How much the auto-gain is allowed to lift, right now.

        A microphone that already delivers speech at a healthy level
        gets NOTHING — gain 1.0, the audio passes through untouched.
        Boosting a good signal is how the room ends up as loud as the
        person. The boost exists only for a capsule that genuinely
        cannot reach a usable level on its own, and it fades in as the
        measured speech level falls rather than switching on abruptly.
        """
        prof = getattr(self, "_cal_profile", None)
        if prof:
            return prof.get("gain", 1.0)   # measured, not guessed
        lvl = self._speech_level
        if lvl <= 0:
            return self._max_gain          # nothing learned yet
        if lvl >= self.HEALTHY_SPEECH_RMS:
            return 1.0                     # a good mic: hands off
        # between "quiet" and "healthy", ease the ceiling in
        frac = 1.0 - (lvl / self.HEALTHY_SPEECH_RMS)
        return 1.0 + (self._max_gain - 1.0) * frac

    # ── ROOM CALIBRATION ─────────────────────────────────────────────
    # Read a few sentences aloud and the station measures what YOUR
    # voice looks like through THIS microphone in THIS room, then sets
    # the gate and the gain from the measurement instead of from
    # numbers I guessed. Everything before this was a guess: a fixed
    # gate ratio, a fixed "healthy" level, a fixed idea of how loud a
    # room is. None of those know how far away you sit or what your
    # kitchen sounds like.
    CALIBRATION_LINES = (
        "The station is ready when you are.",
        "How many pills do I have left?",
        "What do I need to take today?",
    )

    def calibration_state(self):
        """What the calibration screen shows: (phase, level, note)."""
        c = getattr(self, "_cal", None)
        if not c:
            return ("idle", 0.0, "")
        return (c["phase"], c.get("level", 0.0), c.get("note", ""))

    def start_calibration(self):
        """Begin listening to the room, then to the user's voice."""
        self._cal = {"phase": "room", "t0": time.time(), "room": [],
                     "voice": [], "level": 0.0,
                     "note": "Measuring the room — stay quiet."}
        return True

    def cancel_calibration(self):
        self._cal = None

    def _calibration_feed(self, rms):
        """Called for every captured block while calibrating. Kept
        deliberately cheap — it runs on the audio thread."""
        c = self._cal
        if not c:
            return
        c["level"] = rms
        age = time.time() - c["t0"]
        if c["phase"] == "room":
            c["room"].append(rms)
            if age >= 3.0:
                room = sorted(c["room"])
                c["floor"] = room[len(room) // 2] if room else 30.0
                c["phase"] = "voice"
                c["t0"] = time.time()
                c["note"] = "Now read the sentence out loud."
        elif c["phase"] == "voice":
            # only blocks clearly above the measured room count as the
            # person talking
            if rms > max(40.0, c.get("floor", 30.0) * 2.5):
                c["voice"].append(rms)
            if age >= 12.0 or len(c["voice"]) >= 60:
                self._finish_calibration()

    def _finish_calibration(self):
        c = self._cal
        if not c:
            return
        voice = sorted(c.get("voice", []))
        floor = c.get("floor", 30.0)
        if len(voice) < 8:
            c["phase"] = "failed"
            c["note"] = ("I could not hear you clearly. Move closer, "
                         "or check the microphone in Settings.")
            return
        # the 60th percentile of the blocks that were clearly speech —
        # above the mumbles at the start and end of a sentence, below
        # the one loud syllable
        lvl = voice[int(len(voice) * 0.6)]
        self._cal_profile = {
            "floor": floor,
            "voice": lvl,
            # Gate halfway between the room and the voice, in log
            # terms, so it rejects the room without cutting a quiet
            # word. Clamped so a strange measurement can't make the
            # station deaf.
            "gate": max(40.0, min(lvl * 0.35, max(floor * 2.0, 60.0))),
            "gain": max(1.0, min(TARGET_SPEECH_RMS / max(lvl, 1.0), 4.0)),
            "when": time.time(),
        }
        self._save_calibration()
        c["phase"] = "done"
        c["note"] = ("Calibrated: your voice reads %.0f over a room of "
                     "%.0f." % (lvl, floor))

    def _cal_path(self):
        return os.path.join(VOICE_DIR, "calibration.json")

    def _save_calibration(self):
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            with open(self._cal_path(), "w") as f:
                json.dump(self._cal_profile, f)
        except Exception:
            pass

    def load_calibration(self):
        try:
            with open(self._cal_path()) as f:
                p = json.load(f)
            if p.get("voice", 0) > 0:
                self._cal_profile = p
                return True
        except Exception:
            pass
        return False

    def watch_input_level(self):
        """Keep an eye on the microphone level for the first few
        minutes, on its own worker.

        It cannot be judged until audio has actually been flowing, and
        it must not be tangled up with model downloads — an earlier
        version put this in that loop and delayed the pre-rendered
        replies by two minutes."""
        def work():
            try:
                os.nice(10)
            except Exception:
                pass
            for _ in range(30):                  # ~5 minutes
                if self._stop.is_set():
                    return
                time.sleep(10)
                try:
                    self.trim_capture_if_clipping()
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True,
                         name="input-level").start()

    def _capture_restart_allowed(self):
        """Gate on reopening the microphone, so a device that cannot be
        reopened is not hammered.

        Reopening capture on this hardware is not cheap — open_arecord()
        alone can walk a long list of device/rate/channel combinations —
        and every attempt is another chance to leave a stranded PCM
        behind. Without a floor between attempts a failing mic turns
        into a hot loop that makes the situation worse, which is exactly
        what the device did: capture absent two thirds of the time,
        cycling continuously.

        So: at least CAPTURE_REOPEN_MIN_GAP between attempts, backing
        off to CAPTURE_REOPEN_MAX_GAP if they keep coming. The counter
        is exposed for diagnostics — a station quietly restarting its
        microphone forty times an hour is a fault worth seeing, not
        something to hide behind a retry."""
        now = time.time()
        last = getattr(self, "_last_reopen_ts", 0.0)
        streak = getattr(self, "_reopen_streak", 0)
        # Exponential-ish backoff, bounded.
        gap = min(CAPTURE_REOPEN_MIN_GAP * (2 ** min(streak, 4)),
                  CAPTURE_REOPEN_MAX_GAP)
        if now - last < gap:
            return False
        # A long healthy spell clears the streak, so an isolated blip
        # never leaves the device permanently slow to recover.
        if now - last > CAPTURE_REOPEN_MAX_GAP * 2:
            streak = 0
        self._last_reopen_ts = now
        self._reopen_streak = streak + 1
        self._capture_restarts = getattr(self, "_capture_restarts", 0) + 1
        return True

    # ── DIGITAL-SILENCE WATCHDOG ─────────────────────────────────────
    #
    # Split into a pure decision and an impure action on purpose. The
    # decision is the part that can be wrong in a way nobody notices
    # for hours, so it is testable without a microphone, a device, or
    # the 1250-line loop it runs inside.

    def _silence_note_level(self, peak, now=None):
        """Stamp the last time the capture carried a live signal.

        Called once per supervising pass from the value ingest() already
        maintains, so it costs one comparison and never touches the
        audio path."""
        now = time.time() if now is None else now
        # STRICTLY ABOVE ZERO, not above ROUTE_LIVE_PEAK.
        #
        # ROUTE_LIVE_PEAK (3) is the right bar for CHOOSING a route:
        # there, three seconds of audio are measured at once and a real
        # capsule gives 29-107 while a dead endpoint gives 0.
        #
        # It is the wrong bar here, and the device said so. On a quiet
        # night the heartbeat read peak 0 for minute after minute with
        # a perfectly good microphone, because _hb_peak decays within
        # about two seconds and a sparse noise floor does not clear a
        # threshold of 3 in every two-second window. The ladder fired
        # twice in the first minute after a restart, tearing down a
        # capture that was working.
        #
        # The fault this watchdog exists for is not "quiet". It is
        # EXACTLY ZERO, for ever: 96,256 consecutive samples without
        # one non-zero value, because ALSA had averaged a two-channel
        # capsule into silence. A live capsule is never all-zero for
        # two minutes; a dead endpoint is never anything else. That is
        # the discriminator, and it has no false positives.
        if peak > 0:
            self._last_live_peak_ts = now
            # A live signal is proof the current rung works. Clear the
            # ladder so a later, unrelated fault starts from the cheap
            # end again instead of jumping straight to "drop the pin".
            if getattr(self, "_silence_step", 0):
                self._silence_step = 0
                self._note_reopen("capture is live again (peak %d) — "
                                  "silence ladder reset" % peak)
        elif not getattr(self, "_last_live_peak_ts", 0.0):
            # First pass of this process: start the clock now rather
            # than at the epoch, or the watchdog fires immediately on
            # a station that is merely still starting up.
            self._last_live_peak_ts = now

    def _silence_due(self, now=None):
        """Is the capture delivering blocks that are all silence?

        Returns None when there is nothing to do, or the ladder rung to
        climb next (0-based). Every condition here is a reason NOT to
        act, because a false positive tears down a working microphone:

        - blocks must actually be ARRIVING. If they stopped, this is
          CAPTURE_DEAD_AFTER's fault to handle and two watchdogs
          fighting over one stream is its own class of bug.
        - the engine must be IDLE. Mid-turn is the worst possible
          moment to reopen a device, and our own speech is loud enough
          to reset the clock anyway.
        - not muted, not paused: both are somebody asking us to leave
          the microphone alone, and both are indistinguishable from
          this fault when read off a level meter.
        - a minimum number of blocks, so a station three seconds into
          its first capture is never judged.
        """
        now = time.time() if now is None else now
        if getattr(self, "state", "idle") != "idle":
            return None
        if getattr(self, "_muted", False) or getattr(
                self, "_pause_capture", False):
            return None
        if getattr(self, "_measuring_route", False):
            return None
        last_block = getattr(self, "_last_block_ts", 0.0)
        if not last_block or now - last_block > 3.0:
            return None            # not arriving — the other watchdog
        if getattr(self, "_blocks_in", 0) < 200:
            return None            # ~4 s of audio; too early to judge
        # HAS THIS STREAM *EVER* CARRIED A SIGNAL?
        #
        # This is the discriminator, and it took measuring the room to
        # find it. Absence of signal does not separate quiet from dead,
        # because a still room really does go minutes without a
        # non-zero sample: a hand recording of this room, app stopped,
        # put 418 non-zero samples into 3 blocks out of 140 — the floor
        # arrives in short bursts, not as a continuous hiss, and gaps of
        # 204 seconds were measured live.
        #
        # What separates them is whether the stream has EVER delivered
        # anything since it opened:
        #
        #   the fault      0 signal-carrying blocks of 12,871
        #   a quiet room  42 signal-carrying blocks of 19,676
        #
        # Zero against non-zero, not a threshold on a rate. A capture
        # that has produced nothing at all since it opened is broken; a
        # capture that has produced something and is currently quiet is
        # a quiet room, and tearing it down would be vandalism.
        #
        # A stream that worked and later died still needs catching, so
        # that case gets its own, much longer horizon.
        live = getattr(self, "_last_live_peak_ts", 0.0)
        if not live:
            return None
        quiet_for = now - live
        ever = getattr(self, "_hb_live_blocks", 0)
        if ever <= 0:
            if quiet_for < SILENT_CAPTURE_AFTER:
                return None
        elif quiet_for < SILENT_DEAD_AFTER:
            return None
        if now - getattr(self, "_silence_step_ts", 0.0) < SILENCE_STEP_GAP:
            return None
        return int(getattr(self, "_silence_step", 0))

    def _silence_recover(self, step, now=None):
        """Climb one rung of the recovery ladder. Returns what it did.

        CHEAPEST FIRST, and each rung is a different hypothesis about
        why the bytes are zero:

        0. The mixer muted itself or the capture level was driven to
           nothing. Costs no teardown at all — force the unmute and the
           level and let the next pass measure. This is also the rung
           most likely to be right: the level is persisted by ALSA
           across reboots, so a bad value survives everything else.
        1. The remembered arecord combination is wrong for whatever is
           on that card now. Forget it and re-select.
        2. THE PIN IS POINTING AT THE WRONG HARDWARE. Card 5 has been
           observed as both "A28 [AIRHUG 28]" (mixer range 0-8191) and
           "Device_1 [USB PnP Sound Device]" (range 0-16) on this same
           board — USB re-enumeration can swap 4 and 5, which makes
           DOSE_MIC_CARD=5,0 name the dead composite device, and that
           device measures exactly the peak 0 we are looking at. So
           try the other recordable card explicitly.
        3. Give up on pinning entirely and let auto-selection walk
           every route, which at least measures each one for liveness
           before committing.

        Past the end of the ladder it wraps to 0: a station that keeps
        trying is better than one that stops, and SILENCE_STEP_GAP plus
        _capture_restart_allowed() bound how hard it can try.
        """
        now = time.time() if now is None else now
        self._silence_step_ts = now
        self._silence_step = (step + 1) % 4
        self._silence_recoveries = getattr(
            self, "_silence_recoveries", 0) + 1
        # ARM THE RAW TAP, ONCE, THE FIRST TIME THIS EVER FIRES.
        #
        # The tap exists because the heartbeat said peak 0 while a
        # standalone arecord on the same card, at the same moment, read
        # peak 8917 — and guessing which of those was wrong has already
        # cost hours. But it only ever fired when somebody was there to
        # touch voice/dump_raw, and the fault has so far only appeared
        # when nobody was: four attempts to deploy it failed because the
        # Pi dropped off the network, twice mid-install.
        #
        # The watchdog now knows the exact moment the condition is true.
        # That is the moment the evidence is worth having, so it collects
        # itself. Whatever the dump contains answers the question: zeros
        # throughout means the bytes really are silent and the fault is
        # upstream of the engine; a transition from zeros to signal means
        # the rung below fixed it and names the cause.
        #
        # Once per process, and only ever a flag file the engine already
        # knows how to consume — nothing here touches the audio path.
        if not getattr(self, "_silence_tapped", False):
            self._silence_tapped = True
            try:
                os.makedirs(VOICE_DIR, exist_ok=True)
                with open(os.path.join(VOICE_DIR, "dump_raw"), "w") as f:
                    f.write("armed by the silence watchdog %s\n"
                            % time.strftime("%H:%M:%S"))
            except Exception:
                pass
        did = "?"
        try:
            if step == 0:
                card = None
                forced = getattr(self, "_forced_card", None)
                if forced:
                    card = forced[0]
                self._mixer_cache_clear()
                self._unmute_alsa_inputs(force=True)
                if card is not None:
                    self._max_capture(card, force=True)
                did = ("silent capture: forced mixer unmute + level"
                       "%s" % ("" if card is None else " on card %d" % card))
            elif step == 1:
                try:
                    self._arecord_win = {}
                except Exception:
                    pass
                self._mixer_cache_clear()
                self._force_reopen = True
                did = "silent capture: forgot device cache, re-selecting"
            elif step == 2:
                other = self._other_capture_card()
                if other is None:
                    did = ("silent capture: no alternative capture card "
                           "to try")
                else:
                    self._forced_card = other
                    try:
                        self._arecord_win = {}
                    except Exception:
                        pass
                    self._mixer_cache_clear()
                    self._force_reopen = True
                    did = ("silent capture: switching to card %d,%d "
                           "(the pin may name the wrong hardware)"
                           % other)
            else:
                self._forced_card = None
                try:
                    self._arecord_win = {}
                except Exception:
                    pass
                self._mixer_cache_clear()
                self._force_reopen = True
                did = ("silent capture: dropped the card pin, "
                       "full re-selection")
        except Exception as e:
            did = "silent capture: recovery step %d failed (%s)" % (
                step, e)
        # Give the new state a full window before judging it again,
        # otherwise the ladder climbs itself on stale measurements.
        self._last_live_peak_ts = now
        self._note_reopen(did)
        return did

    def _other_capture_card(self):
        """A recordable (card, device) that is NOT the one we are on.

        Prefers a card whose description looks like a microphone, since
        the whole point is to get off a dead endpoint rather than onto
        another one. Returns None when there is no alternative, which is
        the normal case on a board with one mic — the caller then says
        so instead of silently doing nothing."""
        try:
            cur = getattr(self, "_forced_card", None)
            cards = self._alsa_capture_cards() or []
            options = [(c, d, desc) for (c, d, desc) in cards
                       if not cur or (c, d) != (cur[0], cur[1])]
            if not options:
                return None
            for c, d, desc in options:
                try:
                    if (self._looks_like_mic(desc)
                            and not self._card_has_playback(c)):
                        return (int(c), int(d))
                except Exception:
                    pass
            return (int(options[0][0]), int(options[0][1]))
        except Exception:
            return None

    def trim_capture_if_clipping(self):
        """Keep the microphone at a usable level — turning it DOWN when
        it saturates AND UP when it is too quiet.

        This used to only ever step DOWN. That was half a control loop:
        once the level had been pulled down (from a too-hot spell) it
        could never come back, ALSA persisted the low setting across
        reboots, and on the AIRHUG that low setting mapped to
        near-silence (a device reported voice RMS 15 — effectively
        deaf). It is now symmetric and converges: clipping/too-hot steps
        it down, too-quiet steps it up, a healthy signal is left alone.
        """
        seen = getattr(self, "_blocks_seen", 0)
        if seen < 80:                    # ~10 s of audio, enough to judge
            return None
        speech = getattr(self, "_speech_level", 0.0)
        if not speech and not self._clip_blocks:
            return None                  # no speech seen yet
        ratio = self._clip_blocks / float(seen)
        self._clip_ratio = ratio
        self._clip_blocks = 0
        self._blocks_seen = 0
        cur = self._capture_level if self._capture_level \
            else DEFAULT_CAPTURE_LEVEL

        # DOWN: hard clipping, or speech arriving too hot (no headroom,
        # drags the room floor up, harder for the recogniser than a
        # clean signal at a third of full scale).
        hot = speech > HOT_SPEECH_RMS
        if (ratio > 0.02 or hot) and cur > 30:
            self._capture_level = max(30, cur - 10)
            self._speech_level = 0.0     # re-learn at the new level
            self._apply_capture_level()
            return self._capture_level

        # UP: speech was heard but it is far too quiet, and it is NOT
        # clipping — the capture is set too low (the stuck-low case).
        # Step up and re-measure. Capped below 100 so we never sit at
        # the pinned-100% level that overdrives a cheap capsule.
        if speech and speech < LOW_SPEECH_RMS and ratio <= 0.02 \
                and cur < 90:
            self._capture_level = min(90, cur + 10)
            self._speech_level = 0.0
            self._apply_capture_level()
            return self._capture_level
        return None

    def _apply_capture_level(self):
        """Push the current _capture_level to the live capture card."""
        card = (self._forced_card or (None,))[0]
        if card is not None:
            try:
                # force, ALWAYS. This method exists to push a NEW
                # capture level to the hardware; a cached skip here
                # would mean the level silently never arrives —
                # which is the exact class of bug _max_capture_by_numid's
                # docstring already records once before.
                self._max_capture(card, force=True)
            except Exception:
                pass

    def _vad_note(self):
        """What the voice detector is doing, in words. It is a file we
        fetch, not a package we install, so "missing" here means the
        download has not landed yet — not that something is broken."""
        live = (self._vad_trusted
                and getattr(self, "_vad", None) not in (None, "unset"))
        if live:
            return "Silero · listening"
        reason = getattr(self, "_vad_reason", "")
        if reason:
            return reason[:34]
        return "energy only (2 MB model pending)"

    def _room_note(self):
        """Plain words for what the microphone is actually getting, so
        "it can't hear me" has somewhere to start."""
        nf = getattr(self, "_nfloor", 0.0)
        # clipping is the more useful thing to say when it applies
        if time.time() - getattr(self, "_clip_recent", 0) < 30:
            return "input too hot (%s) — turning it down" % (
                "%d%%" % self._capture_level if self._capture_level
                else "device level")
        if nf < 120:
            word = "quiet"
        elif nf < 400:
            word = "some background"
        elif nf < 900:
            word = "noisy — speak closer"
        else:
            word = "too loud to hear you"
        # Say what we are doing to the signal, not just what the room
        # sounds like — "boost 1.0x" means the microphone is being left
        # alone, which is the healthy state.
        lvl = self._speech_level
        if lvl > HOT_SPEECH_RMS:
            return "voice too hot (%.0f) — turning the mic down" % lvl
        if lvl:
            return "%s · voice %.0f, boost %.1fx" % (
                word, lvl, self._agc_ceiling())
        return "%s (%.0f, boost %.1fx)" % (
            word, nf, self._agc_ceiling())

    def warm_models(self):
        """Warm the models the conversation needs — LEANLY and in
        STAGES, so a 4 GB Pi is never asked to load everything at once.

        The previous version loaded faster-whisper tiny.en AND base.en
        AND moonshine and then RAN all three in a startup race. On a Pi
        4 that is a memory-and-heat spike big enough to trip the OOM
        killer or thermal throttling — which looks like the app
        'crashing and aborting itself'. Moonshine is gone from the
        runtime (faster-whisper tiny.en is the fast path and it won);
        the escalation model loads on a delay AFTER the fast one, never
        alongside it. No race, no third model resident in RAM."""
        self._fast_choice = "whisper"

        def work():
            try:
                os.nice(10)
            except Exception:
                pass
            # WARM WITH A SIGNAL, NOT WITH SILENCE.
            #
            # This warmed the recogniser by transcribing one second of
            # zeros — with vad_filter on, which is how the station runs
            # it. The VAD removed the silence, there was nothing left
            # to decode, and the "warm-up" returned without the model
            # ever performing an inference. It warmed nothing.
            #
            # Measured on the device, real turns after a restart:
            #
            #     turn 1  decode 21.42s   on 0.96s of audio
            #     turn 2  decode  4.08s   on 1.72s of audio
            #     turn 3  decode  4.09s   on 2.66s of audio
            #
            # Twenty-one seconds of first-inference cost — building
            # kernels, faulting weights in off the SD card — landing on
            # the first person who speaks to a station that has just
            # started, every single time. Steady state is flat at about
            # 4.1s regardless of length, because Whisper pads every
            # utterance to thirty seconds, so that 21s is not the audio
            # being long. It is the first run.
            #
            # A quiet tone is enough to make the VAD keep it and the
            # decoder actually run. It is never heard by anyone: this
            # is a buffer handed straight to the model.
            import math as _m
            from array import array as _arr
            _n = int(SAMPLE_RATE * 1.2)
            warm_audio = _arr(
                "h", (int(1200 * _m.sin(i * 0.06)) for i in range(_n))
            ).tobytes()
            silence = warm_audio
            # 1) the FAST model (tiny.en) + Piper — the two things the
            #    very first turn needs. Warm Piper with a real synth so
            #    the first reply does not pay the ONNX graph cost.
            try:
                self._load_whisper_fast()
                # vad=False as well as a real signal: belt and braces,
                # because the whole point is that an inference HAPPENS.
                self._fw_transcribe(self._whisper_fast, warm_audio,
                                    vad=False)
                self._warm_decode = round(
                    getattr(self, "_t_fw_decode", 0.0), 2)
            except Exception:
                pass
            try:
                fd, wp = tempfile.mkstemp(suffix=".wav",
                                          dir=TMP_AUDIO_DIR)
                os.close(fd)
                try:
                    self._synth(None, "ready", wp)
                finally:
                    try:
                        os.unlink(wp)
                    except Exception:
                        pass
            except Exception:
                pass
            self._warmed = True
            # 2) the escalation model (base.en) LATER and separately, so
            #    it never spikes RAM at the same moment as the fast one.
            #    It is only needed on the minority of turns the fast
            #    model cannot make out, so a short delay costs nothing.
            #    MEASURED: this was landing ON the first turns. Real
            #    turns through the station, timed end to end:
            #
            #        turn 1  fast 22.09s     turn 3  fast 4.65s
            #        turn 2  fast 19.60s     turn 4  fast 4.87s
            #
            #    Twenty seconds against five, for the same model on the
            #    same audio. The difference is that turns 1 and 2 ran
            #    while base.en was being loaded off an SD card and then
            #    given a warm-up transcribe of its own — half a gigabyte
            #    of I/O and every core busy, at exactly the moment
            #    somebody first speaks to a station that has just
            #    started.
            #
            #    So: wait longer, wait for the engine to be IDLE, and
            #    drop the warm-up transcribe. Constructing the model is
            #    what makes the first escalation quick; running an
            #    inference on a second of silence to prove it was pure
            #    cost. If the station is busy the whole time, the model
            #    simply loads on first need, which is what it did
            #    before this warm-up existed.
            try:
                deadline = time.time() + float(os.environ.get(
                    "DOSE_ESCALATION_WARM_DELAY", "90"))
                while time.time() < deadline and not self._stop.is_set():
                    time.sleep(2.0)
                quiet_since = 0.0
                while not self._stop.is_set():
                    idle = (getattr(self, "state", "idle") == "idle"
                            and time.time() - getattr(
                                self, "_last_turn_end", 0) > 5.0)
                    if idle:
                        if not quiet_since:
                            quiet_since = time.time()
                        elif time.time() - quiet_since > 5.0:
                            self._load_whisper()
                            break
                    else:
                        quiet_since = 0.0
                    time.sleep(2.0)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True,
                         name="model-warm").start()

    def _spoken_constants(self):
        """Every fixed reply that lives in respond() rather than in
        _fixed_lines(), read out of this file's own syntax tree.

        WHY NOT JUST LIST THEM.
        --------------------------------------------------------------
        Because a hand-kept list is a list that goes stale. respond()
        holds twenty-seven fixed replies in random.choice() blocks —
        "Acknowledged. Standing by.", "Thank you, Ryan. I aim for
        precision.", the three ways it introduces itself — and every
        one of them is spoken far more often than the safety
        monologues that ARE in the prewarm list. The device proved the
        cost: a turn replying "Acknowledged. Protocol three: protect
        the patient." rendered its opening from scratch, because that
        whole family of replies was invisible to the prewarm.

        Copying them into _fixed_lines() would work exactly until
        somebody adds a twenty-eighth, which is the kind of decay that
        does not announce itself — the station just gets slower at one
        sentence and nobody knows why.

        WHAT IT WILL AND WILL NOT TAKE.
        --------------------------------------------------------------
        Only string literals that are elements of a LIST literal inside
        respond(). That is the shape every one of these replies has, it
        excludes docstrings, log lines and format templates, and a
        mistake costs one unnecessary clip rendered at idle — a few
        hundred kilobytes, no behaviour change. Anything with a format
        placeholder is skipped outright: "%s" cached as the literal two
        characters would be a clip that is never asked for.

        Reading its own source is cheap (one parse, once, on a
        background thread) and it cannot fail into anything worse than
        the old list, because every failure path returns [].
        """
        out = []
        try:
            import ast
            path = os.path.abspath(__file__)
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except Exception:
            return out
        try:
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                # The reply methods. `respond` holds most of them; the
                # rest are the intent handlers, one of which produced
                # the only turn in the 224 run that still rendered:
                #
                #   "Nothing remains. Every dose is logged. Protocol
                #    two is satisfied."   speak 0.98   total 2.34
                #
                # beside three at speak 0.00 and 1.39-1.58 s total.
                # Naming two methods and stopping was an arbitrary
                # line; a fixed reply is a fixed reply wherever it is
                # written.
                if not (node.name in ("respond", "_quick_answer")
                        or node.name.startswith("_intent_")
                        or node.name.startswith("_safety")):
                    continue
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.List):
                        continue
                    for el in sub.elts:
                        if not isinstance(el, ast.Constant):
                            continue
                        s = el.value
                        if not isinstance(s, str):
                            continue
                        s = s.strip()
                        if not (6 <= len(s) <= 200):
                            continue
                        if "%" in s or "{" in s or "\n" in s:
                            continue
                        if " " not in s or s[-1] not in ".!?":
                            continue
                        out.append(s)
        except Exception:
            return out
        return out

    def prewarm_replies(self):
        """Render every fixed line into the cache, in the background at
        low priority so it never competes with live audio.

        "Low priority" was os.nice(10) and nothing else, and that turned
        out not to be enough. nice only decides who wins when two
        runnable threads want the same core; it does not stop ONNX
        Runtime spinning up a worker per core and it does nothing for a
        capture that must be drained on time. On the device this ran
        flat out the moment the app started — the same moment the USB
        capture stream was coming up — and the PCM reported XRUN, which
        is dropped audio, which is misrecognition.

        Two changes: the thread count is capped in _cap_onnx_threads(),
        and this now WAITS for audio to actually be flowing before it
        starts. Getting the cache warm a few seconds later costs
        nothing; taking the machine away from the microphone during
        startup costs recognition accuracy, which is the whole point of
        the device."""
        def work():
            # NOT THE PASS THE TURN REPORTS — FOURTH TIME.
            #
            # render_to_cache() writes its own timings to self._t_tts,
            # and that is what the turn log prints as `hit`. This thread
            # calls it hundreds of times, on its own schedule, for lines
            # nobody asked for. Without this flag its rows land on
            # whatever turn happens to be in flight.
            #
            # That is exactly what the device showed: "Acknowledged."
            # was sitting in the cache, verified present by key, and the
            # turn that said it logged hit=0. The turn had hit it; the
            # prewarm wrote a miss over the row a moment later. I spent
            # an hour looking for a cache bug that was a reporting bug.
            #
            # _last_engine, _t_tts on the stream thread, _t_tts on the
            # speculation thread, and now this. Every time: two threads,
            # one attribute, last writer wins.
            _TL.speculative = True
            try:
                os.nice(10)
            except Exception:
                pass
            # Wait for the capture to be alive (blocks arriving), up to
            # a bounded time so a mic that never comes up cannot stop
            # the cache being built at all.
            deadline = time.time() + float(os.environ.get(
                "DOSE_PREWARM_DELAY_MAX", "45"))
            while time.time() < deadline:
                if self._stop.is_set():
                    return
                if getattr(self, "_blocks_seen", 0) > 0:
                    # Audio is flowing. Give the stream a moment to
                    # settle at its steady state before adding load.
                    time.sleep(3.0)
                    break
                time.sleep(0.5)
            # CACHE WHAT _speak() WILL ACTUALLY ASK FOR.
            #
            # This cached each whole line. _speak() does not render
            # whole lines — it splits them and renders chunks[0] first,
            # so the cache key it looks up is the OPENING FRAGMENT. For
            # every line long enough to be split, the prewarmed entry
            # could never be found.
            #
            # The device said so all evening and I read past it: 32
            # clips sitting in voice/cache and `'hit': 0` on every
            # single render, including replies identical across three
            # runs. A cache whose keys are not the keys anybody looks
            # up is a directory of files.
            #
            # Now each line is chunked exactly as _speak() chunks it,
            # and every chunk is rendered — the opening because it is
            # on the critical path, the rest because they are played
            # seconds later and cost nothing to have ready.
            # ORDER MATTERS MORE THAN COVERAGE.
            #
            # Rendering in list order put three safety monologues — the
            # poison-control line, the crisis line, the dose-advice line
            # — at position six, seven and eight. They are the longest
            # things this station can say and among the rarest, and the
            # cache spent its first several minutes on them while
            # "Acknowledged." and "Standing by." waited behind.
            #
            # Only the FIRST chunk of a reply is on the critical path;
            # everything after it renders during playback and is never
            # waited for. So: every line's first chunk, shortest first,
            # then the individual sentences, then the remainders. The
            # openings that decide time-to-first-sound are all a few
            # dozen characters, so the whole first group is done in
            # under a minute — and the monologues still get cached,
            # last, out of everybody's way.
            heads, sents, tails = [], [], []
            for line in self._fixed_lines() + self._spoken_constants():
                if self._stop.is_set():
                    return
                try:
                    chunks = self._split_first(self._sentences(line))
                except Exception:
                    chunks = [line]
                # AND EACH SENTENCE ON ITS OWN.
                #
                # Chunking depends on the length of the WHOLE line, so
                # the same opening lands differently in different
                # replies. "Acknowledged. Standing by." is 26
                # characters, under the split threshold, and caches
                # whole — while "Acknowledged. Protocol three: protect
                # the patient." splits and asks for "Acknowledged."
                # alone, which was never stored.
                #
                # The device showed it: every invariant opening
                # CACHED, and the turn that said "Acknowledged."
                # still rendering it from scratch.
                #
                # So the sentences are cached independently too. A
                # sentence is the unit an opening is actually made of,
                # and storing them costs a few hundred kilobytes.
                if chunks:
                    heads.append(chunks[0])
                    tails.extend(chunks[1:])
                try:
                    for part in re.split(r"(?<=[.!?])\s+", line):
                        part = part.strip()
                        if part:
                            sents.append(part)
                except Exception:
                    pass

            order, seen = [], set()
            for group in (heads, sents, tails):
                for c in sorted(group, key=len):
                    if c and c not in seen:
                        seen.add(c)
                        order.append(c)
            self._prewarm_total = len(order)

            n = 0
            for c in order:
                if self._stop.is_set():
                    return
                # NEVER RENDER DURING A TURN.
                #
                # nice(10) settles who gets a core, not who gets the
                # four ONNX threads, and a background synthesis in the
                # middle of a live reply competes with the one render
                # the person is actually waiting for. Idle is the only
                # time this work is free, so it only runs then.
                while self.state in ("thinking", "speaking"):
                    if self._stop.is_set():
                        return
                    time.sleep(0.25)
                if self.render_to_cache(c):
                    n += 1
                    self._prewarmed = n
            self._prewarmed = n
        threading.Thread(target=work, daemon=True,
                         name="tts-prewarm").start()

    # ── openWakeWord: a dedicated neural wake-word detector (~2.5 ms
    #    per 80 ms frame). Far more reliable than matching "hey dose"
    #    in a running transcript, and it leaves the CPU free.
    #    OPT-IN: set DOSE_WAKE_MODEL to a trained model (e.g. a
    #    hey_dose.onnx from openWakeWord's training notebook) or to a
    #    built-in name. Without it we keep the Vosk phrase match, so
    #    "Hey Dose" behaves exactly as before.
    def _oww(self):
        if getattr(self, "_oww_model", "unset") != "unset":
            return self._oww_model
        self._oww_model = None
        want = os.environ.get("DOSE_WAKE_MODEL", "").strip()
        if not want:
            return None
        try:
            import openwakeword
            from openwakeword.model import Model
            if not os.path.exists(want):
                try:
                    openwakeword.utils.download_models(
                        model_names=[want])
                except Exception:
                    pass
            self._oww_model = Model(wakeword_models=[want],
                                    inference_framework="onnx")
            self._oww_buf = bytearray()
            self._oww_cool = 0
            self._oww_thresh = float(
                os.environ.get("WAKE_THRESHOLD", "0.5"))
        except Exception:
            self._oww_model = None
        return self._oww_model

    def _oww_feed(self, data):
        """Feed 16 kHz int16 audio; True when the wake word fires.
        Buffers into the exact 80 ms (1280-sample) frames it expects."""
        m = self._oww()
        if m is None:
            return False
        try:
            import numpy as np
            self._oww_buf.extend(data)
            fired = False
            frame_bytes = 1280 * 2
            while len(self._oww_buf) >= frame_bytes:
                chunk = bytes(self._oww_buf[:frame_bytes])
                del self._oww_buf[:frame_bytes]
                arr = np.frombuffer(chunk, dtype=np.int16)
                scores = m.predict(arr)
                top = max(scores.values()) if scores else 0.0
                if self._oww_cool > 0:
                    self._oww_cool -= 1
                    continue
                if top >= self._oww_thresh:
                    m.reset()
                    self._oww_cool = 25      # ~2 s refractory
                    fired = True
            return fired
        except Exception:
            return False

    # ── Upgraded-model status + self-healing download ────────────────
    #    You should be able to SEE on the device whether the free
    #    upgraded models actually landed, not take it on faith. This
    #    reports each model's real state and keeps retrying in the
    #    background until they're present (network may come up later).
    def model_status(self):
        """[(label, ok, detail)] for every model the voice uses."""
        st = getattr(self, "_model_status", None)
        if st:
            return st
        return [("checking models…", None, "")]

    def ensure_upgraded_models(self, tries=6):
        """Download the two free on-device models if missing — her
        Piper voice and the Moonshine recogniser — retrying with
        backoff. Safe to call anytime."""
        if getattr(self, "_model_dl_running", False):
            return
        self._model_dl_running = True

        def work():
            try:
                os.nice(10)
            except Exception:
                pass
            delay = 5
            for attempt in range(tries):
                rows = []

                # There are exactly TWO things on this device now, and
                # they do different jobs — they are not alternatives to
                # each other and nothing else is downloaded.

                # 1) HER VOICE — one Piper file, nothing substitutes.
                pok = bool(self._piper_path
                           and os.path.exists(self._piper_path)
                           and VOICE_NAME in os.path.basename(
                               self._piper_path))
                rows.append(("Voice", pok,
                             VOICE_NAME if pok else "downloading…"))

                # 2) HEARING — the FAST path is whichever recogniser won
                #    the startup race on THIS board. Show which, and the
                #    two measured times so the choice is auditable.
                choice = self._fast_engine()
                fw = self._load_whisper_fast()
                msr = getattr(self, "_ms_race_secs", None)
                fwr = getattr(self, "_fw_race_secs", None)
                if choice == "moonshine":
                    self._ms_v2 = "unset"
                    good = self._moonshine_v2() is not None
                    if good:
                        label = "moonshine %s" % (
                            (self._ms_arch_used or "").split("_")[0]
                            .lower() or self.STT_ARCH or "?")
                    else:
                        # never leave it blank — say WHY
                        label = (getattr(self, "_ms_reason", "")
                                 or "download failed")
                else:
                    good = fw is not None
                    size = getattr(self, "_whisper_fast_size",
                                   FAST_WHISPER_MODEL)
                    # size doubles as the reason when it failed to load
                    # ("not installed" / "unavailable"), so the row is
                    # never silently blank
                    label = "whisper %s" % size
                if msr is not None and fwr is not None \
                        and (msr < float("inf") or fwr < float("inf")):
                    def _fmt(x):
                        return "%.2fs" % x if x < float("inf") else "-"
                    label += " (race ms %s / wh %s)" % (_fmt(msr),
                                                        _fmt(fwr))
                rows.append(("Speech (fast)", good, label[:52]))

                # 2b) The base.en escalation model — runs only when the
                #     fast path comes back with nothing usable.
                if self._whisper is not None:
                    detail, good = ("whisper %s (escalation)"
                                    % self._whisper_size), True
                elif self._whisper_loaded:
                    detail, good = self._whisper_size, False
                else:
                    detail, good = ("%s (loads when needed)"
                                    % self._whisper_size), True
                rows.append(("Speech (backup)", good, detail))

                # 2c) CLOUD STT — the primary path when online. Shows the
                #     configured free providers and whether the net is up,
                #     so it is obvious whether the Pi is offloading STT.
                try:
                    import dose_cloud_stt as _cs
                    provs = _cs.available_providers()
                    mode = os.environ.get("DOSE_STT_MODE",
                                          "auto").lower()
                    if mode == "local":
                        rows.append(("Speech (cloud)", True,
                                     "off (DOSE_STT_MODE=local)"))
                    elif not provs:
                        rows.append(("Speech (cloud)", True,
                                     "no key — running LOCAL. Add groq_key "
                                     "(fast) or hf_token to offload."))
                    else:
                        on = self._is_online()
                        # note when HF is anonymous vs on the user's quota
                        tag = "+".join(provs)
                        if "hf" in provs and not _cs.hf_is_authenticated():
                            tag = tag.replace("hf", "hf(anon)")
                        rows.append(("Speech (cloud)", True,
                                     "%s · %s" % (tag,
                                     "online (PRIMARY — Pi offloaded)"
                                     if on else "offline — using local")))
                except Exception:
                    pass

                # 3) The live listener. This is NOT a competing
                #    recogniser: it is what puts your words on the
                #    screen while you are still talking, and it is the
                #    safety net that keeps the station from going deaf
                #    if Moonshine is ever unavailable.
                vok = bool(self._vosk_dir and os.path.isdir(self._vosk_dir))
                rows.append(("Live listener", vok,
                             "on-screen text" if vok else "missing"))

                # while we are here: is the microphone level too hot?
                try:
                    self.trim_capture_if_clipping()
                except Exception:
                    pass

                self._model_status = rows
                if all(r[1] for r in rows):
                    # everything present — re-render the cached replies
                    # in her voice, then stop retrying
                    try:
                        self.prewarm_replies()
                    except Exception:
                        pass
                    break
                if self._stop.is_set():
                    break
                time.sleep(delay)
                delay = min(delay * 2, 120)
            self._model_dl_running = False

        threading.Thread(target=work, daemon=True,
                         name="model-fetch").start()

    def _vosk_grammar(self):
        """Constrain recognition to the words Dose actually expects —
        wake word, command phrases, medication names, numbers, days.
        A grammar makes the small Vosk model FAR more accurate and
        faster for our fixed command set (so 'what medication do I
        take today' stops being heard as random words), while '[unk]'
        still lets it fall back for anything off-script."""
        words = set()
        for p in (
            "hey dose hey dos hay dose",
            "what do i take today what medication do i need to take "
            "today what should i take what are my medications",
            "how many pills do i have left how many are left what is "
            "my count",
            "what is next when is my next dose what do i take next",
            "did i take my is it time for my have i taken",
            "add a new medication add medication register a pill",
            "yes no cancel stop never mind repeat that again go back "
            "done okay right correct wrong that is wrong",
            "what time is it help thank you thanks good morning",
            "morning afternoon evening night noon midnight today "
            "tomorrow every day",
            "monday tuesday wednesday thursday friday saturday sunday",
            "one two three four five six seven eight nine ten eleven "
            "twelve twenty thirty forty fifty at o'clock a m p m",
            "pill pills tablet tablets capsule dose doses medication "
            "medicine",
        ):
            words.update(p.split())
        try:
            for md in self.app.med_data.values():
                for w in re.findall(r"[a-z]+",
                                    (md.get("name", "") or "").lower()):
                    if len(w) > 2:
                        words.add(w)
        except Exception:
            pass
        return json.dumps([" ".join(sorted(words)), "[unk]"])

    def _run(self):
        from vosk import Model, KaldiRecognizer, SetLogLevel
        SetLogLevel(-1)
        self._prime_speech()
        try:
            self._vosk_model = Model(self._vosk_dir)
            try:
                rec = KaldiRecognizer(self._vosk_model, SAMPLE_RATE,
                                      self._vosk_grammar())
            except Exception:
                rec = KaldiRecognizer(self._vosk_model, SAMPLE_RATE)
        except Exception:
            self.available = False
            self.reason = "speech model failed to load"
            return

        self._native_rate = SAMPLE_RATE
        self._ratecv_state = None
        self._gain = 1.0
        # Ceiling on the software auto-gain — deliberately modest.
        #
        # This was 20x, then 40x, chasing a weak far-field capsule.
        # That was the wrong lever. A microphone that hears properly
        # needs no help, and a big ceiling actively hurts: every quiet
        # block between words gets multiplied too, so the room comes up
        # with the voice and the recogniser is handed an overdriven mix
        # of everything instead of a person talking. A good USB mic
        # does its own conditioning; stacking ours on top fights it.
        #
        # 4x is enough to rescue genuinely quiet input and small enough
        # that it cannot blow anything out. It also switches itself OFF
        # entirely once the microphone is shown to be healthy — see
        # _agc_ceiling().
        self._max_gain = float(os.environ.get("DOSE_MAX_GAIN", "4"))
        self._speech_level = 0.0   # typical RMS of actual speech
        self._nfloor = 50.0      # learned ambient noise floor (RMS)
        self._snr = 0.0          # how far the last block stood above it
        self._cal = None         # calibration in progress, if any
        self._vad_trusted = True  # cleared if Silero looks wrong
        self._vad_seen = 0
        self._vad_agreed = 0
        self._barge = False      # user started talking over her
        self._st_want = False    # guided self-test wants a turn
        self._st_result = None
        self._cal_profile = None  # what calibration measured
        self._clip_blocks = 0    # blocks where the input saturated
        self._blocks_seen = 0
        self._clip_recent = 0.0
        # START FROM A KNOWN, SANE CAPTURE LEVEL.
        #
        # "Leave the hardware alone" failed badly: the clipping watchdog
        # only ever stepped the level DOWN (from a too-hot spell), ALSA
        # persisted that across reboots, and on the AIRHUG the low
        # setting mapped to near-silence — a device reported voice RMS
        # 15, effectively deaf. So we now set a MODERATE default at
        # startup (overriding any stuck-low persisted value) and let the
        # symmetric auto-leveller move it up OR down from there.
        # DOSE_CAPTURE_LEVEL still pins an exact value if asked.
        _lvl = os.environ.get("DOSE_CAPTURE_LEVEL", "").strip()
        self._capture_level = (int(_lvl) if _lvl.isdigit()
                               else DEFAULT_CAPTURE_LEVEL)
        self._last_voice_ts = 0.0  # last block that carried real speech

        def ingest(data):
            """Common path for every capture backend: gate, resample
            to 16 kHz, apply auto-gain, feed the queue, service the
            live level meter."""
            # PROOF OF LIFE FROM THE DEVICE, recorded before any of the
            # early returns below. This is "the microphone delivered a
            # block", which is a completely different question from
            # "somebody said something" — and conflating the two was
            # tearing down a perfectly healthy capture every ten
            # seconds of quiet. See the watchdog in the main loop.
            self._last_block_ts = time.time()
            self._blocks_in = getattr(self, "_blocks_in", 0) + 1
            try:
                pk, _rm = _peak_rms(data)
                # decay, so the number reflects NOW rather than the
                # loudest thing since boot
                self._hb_peak = max(pk, int(getattr(self, "_hb_peak", 0)
                                            * 0.95))
                self._hb_bytes = len(data)
                if pk > 0:
                    self._hb_live_blocks = getattr(
                        self, "_hb_live_blocks", 0) + 1
                self._hb_nz = sum(1 for b in data[:64] if b)
            except Exception:
                pass
            # RAW TAP. Touch voice/dump_raw to have the engine write the
            # next ~2 s of capture EXACTLY as it arrives, before any
            # gate, resample or gain. The heartbeat says the bytes are
            # all zero while a standalone arecord on the same card, at
            # the same moment, reads peak 8917. One of those is wrong
            # and guessing which has already cost hours — so the engine
            # hands over its own bytes and the question is settled by
            # comparing two files.
            try:
                flag = os.path.join(VOICE_DIR, "dump_raw")
                if os.path.exists(flag):
                    buf = getattr(self, "_raw_dump", None)
                    if buf is None:
                        buf = self._raw_dump = bytearray()
                    buf += data
                    if len(buf) >= self._native_rate * 2 * 2:
                        import wave as _w
                        out = os.path.join(VOICE_DIR, "raw_from_engine.wav")
                        f = _w.open(out, "wb")
                        f.setnchannels(1)
                        f.setsampwidth(2)
                        f.setframerate(self._native_rate)
                        f.writeframes(bytes(buf))
                        f.close()
                        self._raw_dump = None
                        os.remove(flag)
            except Exception:
                pass
            # MEASUREMENT BEATS MUTE AND BARGE-IN.
            #
            # route_floor() decides whether a capture device is real by
            # reading _audio_q for 1.6 s. But every block below can be
            # dropped before it reaches that queue: while muted, and
            # while the engine is SPEAKING, when audio goes to barge-in
            # detection and returns.
            #
            # Device selection runs at startup, which is exactly when
            # the station is playing its greeting and prewarming
            # replies. So the walk measured an empty queue and scored
            # EVERY route digitally silent — including the pinned
            # AIRHUG, which measures peak 29-107 when tested standalone.
            # From the device, 2026-09-17 22:05:
            #
            #   arecord FORCED card 5,0: opened, peak 0 — rejected
            #   ... every other route: peak 0 — rejected
            #   took: 63.6s (budget 45s)   chose: NOTHING
            #
            # The microphone was working the whole time; the ruler was
            # being held while the engine talked over it.
            #
            # While a measurement is in progress the block goes to the
            # queue first, unconditionally. Barge-in still runs — it is
            # the reason speaking-state audio is examined at all.
            measuring = getattr(self, "_measuring_route", False)
            if self._muted and not measuring:
                return
            if self.state == "speaking" and not measuring:
                # BARGE-IN. Her own voice is coming out of a speaker
                # inches away, so "is anything loud" is useless here —
                # but "is this a HUMAN VOICE that is not the clip we
                # are playing" is answerable, and that is the whole
                # reason a real voice detector is worth its 1.3 MB.
                self._detect_barge_in(data)
                return
            if self._native_rate != SAMPLE_RATE:
                try:
                    import audioop
                    data, self._ratecv_state = audioop.ratecv(
                        data, 2, 1, self._native_rate, SAMPLE_RATE,
                        self._ratecv_state)
                except Exception:
                    return
            # ADAPTIVE AGC with a self-calibrating noise gate: cheap USB
            # mics (C-Media/CM108) capture so quietly that raw speech
            # sits near the noise floor where Vosk hears nothing. We
            # continuously learn the mic's own ambient floor (fast down,
            # very slow up), leave ambient hiss untouched so amplified
            # noise never confuses recognition, and boost only blocks
            # that rise clearly above that floor — toward a healthy RMS
            # for Vosk. This adapts to ANY mic quietness with no fixed
            # threshold a faint mic could never cross.
            rms_raw = 0
            try:
                import audioop
                rms = rms_raw = audioop.rms(data, 2)
                # ── AMBIENT NOISE FLOOR ──────────────────────────────
                # Both directions move as an average. This used to
                # snap straight down to the quietest block seen and
                # then climb back at 0.0005 per block — about four
                # minutes. So one momentary dip left the gate pinned
                # at its minimum, and anything continuous after that
                # (a running tap, a fan, a television) sat above the
                # gate and was treated as speech: amplified by the
                # AGC, fed to the recogniser, and — because every
                # block kept stamping "speech heard" — the turn never
                # ended. It looked like the microphone had stopped
                # understanding anything.
                #
                # Down over ~2 s so it follows a room going quiet;
                # up over ~6 s so a burst of speech does not raise it,
                # but a tap running does within seconds.
                # ── THE NOISE FLOOR MUST NOT LEARN FROM YOUR VOICE
                # This updated on EVERY block, speech included. Talking
                # for three seconds dragged the floor up toward the
                # level of the speech itself, the gate went up with it,
                # and your voice could no longer clear the gate it had
                # just raised. The station went deaf BECAUSE you spoke
                # to it — the device reported "room too loud to hear
                # you · voice 2462" with the room supposedly quiet, and
                # 1 of 10 turns understood.
                #
                # The gate is computed from the PREVIOUS floor, the
                # block is judged against it, and the floor learns only
                # from what is not a voice. A tap or a fan still raises
                # it — Silero calls those not-speech — but you cannot.
                nf = self._nfloor
                prev_gate = max(40.0, nf * NOISE_GATE_RATIO)
                # SILERO IS A NEURAL NET, AND IT RUNS PER BLOCK.
                #
                # is_speech() is an ONNX inference. Above the gate it
                # runs on every 21 ms block — about 26 times a second
                # in this room, where 55% of blocks now clear the gate
                # — on the same cores Piper renders the reply on.
                #
                # It is gated for SPEAKING further up (that path goes
                # to barge-in and returns) but not for THINKING, which
                # is precisely the window where the first chunk is
                # being synthesized and the person is waiting.
                #
                # The numbers fit: a render measured 0.67 s in a run
                # where 4.5% of blocks carried signal, and 2.2-3.4 s in
                # runs at 54-55%, while every other explanation was
                # ruled out with a measurement. The noise floor still
                # learns from these blocks — that is arithmetic, not a
                # model — so only the inference is skipped.
                if self.state == "thinking":
                    is_voice = False
                else:
                    is_voice = (rms > prev_gate
                                and self.is_speech(data, True))
                if rms < nf:
                    nf = nf * 0.95 + rms * 0.05      # room went quiet
                elif not is_voice:
                    nf = nf * 0.98 + rms * 0.02      # louder, not a voice
                # else: this is speech. Learn nothing from it.
                #
                # And a hard ceiling, so no amount of noise can raise
                # the gate past the point where ordinary speech could
                # never clear it. Being a bit noisy is recoverable;
                # being deaf is not.
                self._nfloor = max(1.0, min(nf, NOISE_FLOOR_MAX))
                # Speech has to stand clear of the room, not merely be
                # audible in it.
                prof = getattr(self, "_cal_profile", None)
                if prof:
                    # measured in THIS room, with THIS microphone, in
                    # YOUR voice — trusted over any general rule, but
                    # still allowed to rise if the room gets louder
                    # than it was on the day
                    gate = max(prof["gate"],
                               self._nfloor * NOISE_GATE_RATIO)
                else:
                    gate = max(40.0, self._nfloor * NOISE_GATE_RATIO)
                self._snr = rms / max(1.0, self._nfloor)
                # Is the input saturating? A clipped sample carries no
                # information the recogniser can use, and it is what
                # "even a clap is too loud" means.
                try:
                    peak = audioop.max(data, 2)
                except Exception:
                    peak = 0
                if self._cal is not None:
                    self._calibration_feed(rms_raw)
                if peak >= 31000:
                    self._clip_blocks += 1
                    self._clip_recent = time.time()
                self._blocks_seen += 1
                # per-turn quality, for the log
                self._turn_blocks = getattr(self, "_turn_blocks", 0) + 1
                self._turn_peak = max(getattr(self, "_turn_peak", 0),
                                      peak)
                if peak >= 31000:
                    self._turn_clip = getattr(self, "_turn_clip", 0) + 1
                if rms > gate:
                    self._turn_snr = max(getattr(self, "_turn_snr", 0.0),
                                         self._snr)
                energetic = rms > gate
                # WHAT THE GATE AND THE MODEL EACH DECIDED.
                #
                # The station stopped entering 'listening' at exactly
                # the point silero_vad.onnx stopped being deleted on
                # every launch — so for the first time in a while the
                # VAD is genuinely running, and "it hears nothing" and
                # "it hears everything and calls none of it speech"
                # look identical from outside. Two counters separate
                # them, and the heartbeat prints both.
                if energetic:
                    self._vad_loud = getattr(self, "_vad_loud", 0) + 1
                    if is_voice:
                        self._vad_voice = getattr(self, "_vad_voice", 0) + 1
                if energetic and is_voice:
                    # stamp the moment: the endpointer uses this to cut
                    # the instant the user stops talking
                    self._last_voice_ts = time.time()
                    # learn how loud this microphone actually hears
                    # speech, so we can stop boosting once it is clear
                    # that it does not need boosting
                    self._speech_level = (self._speech_level * 0.97
                                          + rms * 0.03)
                    g = max(1.0, min(3000.0 / rms, self._agc_ceiling()))
                    # rise quickly toward target, no pumping
                    self._gain = self._gain * 0.5 + g * 0.5
                elif not energetic:
                    self._gain = 1.0               # ambient: leave clean
                if self._gain > 1.05:
                    data = audioop.mul(data, 2, self._gain)
            except Exception:
                pass
            lp = self._level_probe
            if lp and time.time() < lp["until"]:
                try:
                    import audioop
                    lp["max"] = max(lp["max"], audioop.rms(data, 2))
                    lp["raw"] = max(lp.get("raw", 0), rms_raw)
                except Exception:
                    pass
            try:
                self._audio_q.put_nowait(data)
            except queue.Full:
                try:
                    self._audio_q.get_nowait()      # drop the oldest
                    self._audio_q.put_nowait(data)
                except Exception:
                    pass

        def callback(indata, frames, t, status):
            ingest(bytes(indata))

        def open_portaudio():
            index, name, rate = self._pick_input_device()
            self.mic_index = index
            try:
                s = self._sd.RawInputStream(
                    device=index, samplerate=rate,
                    blocksize=int(BLOCK_SIZE * rate / SAMPLE_RATE),
                    dtype="int16", channels=1, callback=callback)
                s.start()
                self._native_rate = rate
                self._ratecv_state = None
                self.mic_name = name
                return ("portaudio", s)
            except Exception:
                pass
            # ASK THE DEVICE WHAT IT SUPPORTS BEFORE GUESSING.
            #
            # This used to try SAMPLE_RATE (16 kHz) first and walk down a
            # fixed list. The mic on this station — like most cheap USB
            # capsules — supports exactly ONE mode: 48 kHz stereo. So the
            # first probe always failed, and the device log filled with
            #
            #   Expression 'paInvalidSampleRate' failed ... line 2048
            #   Expression 'AlsaOpen(...)' failed ... line 1904
            #
            # dozens of times per launch. Every one of those is a failed
            # device open, and a failed open on a USB capture device is
            # exactly the operation that was stranding the PCM.
            #
            # So: try what the device REPORTS first, then 48 kHz (near
            # universal on USB audio), and only then our preferred rate.
            rates = []
            try:
                info = self._sd.query_devices(
                    getattr(self, "mic_index", None), "input")
                dsr = int(round(float(info.get("default_samplerate") or 0)))
                if dsr > 0:
                    rates.append(dsr)
            except Exception:
                pass
            for r in (48000, 44100, SAMPLE_RATE, 24000, 8000):
                if r not in rates:
                    rates.append(r)
            for r in rates:
                try:
                    s = self._sd.RawInputStream(
                        samplerate=r,
                        blocksize=int(BLOCK_SIZE * r / SAMPLE_RATE),
                        dtype="int16", channels=1, callback=callback)
                    s.start()
                    self._native_rate = r
                    self._ratecv_state = None
                    self.mic_name = "default"
                    return ("portaudio", s)
                except Exception:
                    continue
            return None

        def open_pipe_cmd(cmd, name, native_rate=SAMPLE_RATE,
                          channels=1):
            """One recorder subprocess (arecord / pw-record / parec)
            as a capture. native_rate tells ingest() what to resample
            from; channels=2 means the reader downmixes stereo to mono
            by taking the LOUDER channel per block, so a USB mic wired
            to only one channel is still captured at full level."""
            # KEEP THE RECORDER'S STDERR. It was DEVNULL, which threw
            # away the only thing that says WHY the microphone died.
            #
            # A soak on the device showed the capture cycling — present
            # in 14 of 20 samples, absent in 6, with hw_ptr resetting
            # each time, so arecord was exiting and being reopened over
            # and over. arecord says why it exits ("overrun!!!",
            # "Device or resource busy", a broken pipe) on stderr, and
            # every one of those messages was being discarded.
            #
            # Bounded to the last few KB in memory: this must never grow
            # without limit or block the recorder by filling a pipe
            # nobody drains.
            try:
                # start_new_session: the recorder gets its OWN session
                # and process group.
                #
                # arecord treats EINTR as a fatal read error and exits —
                # "pcm_read:2272: read error: Interrupted system call",
                # which this station's stderr was full of. Any signal
                # aimed at our process GROUP (a stray killpg, a SIGHUP
                # when a parent shell goes away, a terminal signal from
                # the kiosk session) therefore kills the microphone,
                # even though it was never meant for it.
                #
                # Nothing needs it in our group. We stop it by PID, on
                # purpose, in close_capture(). So it is moved out of the
                # blast radius: only a signal addressed to that exact
                # pid can end it now.
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     start_new_session=True,
                                     env=self._audio_env())
            except Exception:
                return None
            # Keep a handle on the LIVE recorder. release_audio() needs
            # to be able to end it from outside this closure, because
            # os.execv() destroys every thread and every local without
            # running a single finally — and a recorder that outlives
            # that call keeps the USB capture device away from the
            # process replacing us.
            self._cap_proc = p

            def _drain_err(proc=p, label=name):
                """Read the recorder's complaints so they are visible
                and so a full stderr pipe can never stall it."""
                try:
                    for raw in iter(proc.stderr.readline, b""):
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line:
                            continue
                        buf = getattr(self, "_capture_errs", None)
                        if buf is None:
                            buf = self._capture_errs = []
                        buf.append("%s  %s: %s" % (
                            time.strftime("%H:%M:%S"), label, line[:160]))
                        del buf[:-40]        # keep the last 40 only
                except Exception:
                    pass
            threading.Thread(target=_drain_err, daemon=True,
                             name="capture-stderr").start()
            time.sleep(0.3)
            if p.poll() is not None:
                return None
            self._native_rate = native_rate
            self._ratecv_state = None

            def reader(proc=p, label=name):
                """Drain the recorder — and CLEAN UP WHEN IT ENDS.

                This loop used to simply `break` and return. That is
                how the microphone kept dying. arecord exits on an ALSA
                XRUN (an overrun, which on this board happens whenever
                something takes the CPU away from the capture for too
                long); the read returns empty; the loop broke; and then
                NOTHING happened. The process was never reaped — the
                device grew "[arecord] <defunct>" — its stdout was
                never closed, and, worst of all, nobody told the engine
                the capture had died. The ALSA PCM was left sitting in
                state SETUP with an owner that no longer existed, which
                made it unopenable by anyone, including a fresh
                arecord, until the whole app was restarted.

                Observed directly: state RUNNING, then XRUN, then SETUP
                for as long as you care to watch.

                So the teardown now lives in a finally: TERM the
                recorder so it releases the ALSA device properly, reap
                it, close the pipe, and raise a flag the supervising
                loop can see so the capture is REOPENED rather than
                silently lost."""
                import audioop
                delivered = 0
                why = "unknown"
                try:
                    if self._stop.is_set():
                        # Caught before the first read. This used to be
                        # indistinguishable from a silent microphone.
                        why = "stop event was ALREADY SET at thread start"
                    while (proc.poll() is None
                           and not self._stop.is_set()):
                        try:
                            n = BLOCK_SIZE * 2 * (2 if channels == 2
                                                  else 1)
                            data = proc.stdout.read(n)
                        except Exception as e:
                            why = "read raised: %r" % (e,)
                            break
                        if not data:
                            why = "recorder closed its pipe (EOF)"
                            break
                        delivered += 1
                        if channels == 2:
                            # PICK THE LOUDER CHANNEL BY PEAK, AND
                            # STICK TO IT.
                            #
                            # This compared audioop.rms() — the same
                            # mistake route_floor() and capture_is_live()
                            # each had, in a third place. In a quiet
                            # room BOTH channels measure RMS 0 on this
                            # hardware, so the comparison was always a
                            # tie and always resolved to left. On a
                            # capsule wired to the right channel that is
                            # silence until somebody speaks loudly
                            # enough to break the tie — and then the
                            # choice flips mid-word, chopping the
                            # utterance in half.
                            #
                            # Peak separates the channels when RMS
                            # cannot (29 vs 0, measured), and the
                            # running maxima decay slowly so the choice
                            # is made once from accumulated evidence
                            # rather than re-litigated every 21 ms.
                            # ONE PASS PER BLOCK, NOT FOUR.
                            #
                            # This split every block into two channels
                            # and measured both, every time: four walks
                            # over the samples on the one thread that
                            # must drain arecord's pipe on schedule. The
                            # device showed what that costs —
                            #
                            #     blocks/sec: 13.0   (nominal 46.9)
                            #     overrun!!! (at least 4201.874 ms long)
                            #
                            # and arecord exits on an overrun, so the
                            # microphone was being destroyed about thirty
                            # times a minute. A controlled test settles
                            # the mechanism: the same command piped into
                            # a prompt reader survives, and piped into a
                            # deliberately slow one overruns.
                            #
                            # The choice of channel does not change from
                            # one 21 ms block to the next. It is made
                            # once every CHANNEL_RECHECK blocks, and in
                            # between there is a single tomono with the
                            # winning weights — one pass, the same
                            # answer.
                            try:
                                n_seen = getattr(self, "_ch_n", 0) + 1
                                self._ch_n = n_seen
                                if (n_seen % CHANNEL_RECHECK) == 1:
                                    left = audioop.tomono(data, 2, 1, 0)
                                    right = audioop.tomono(data, 2, 0, 1)
                                    lp, _lr = _peak_rms(left)
                                    rp, _rr = _peak_rms(right)
                                    self._ch_l = max(
                                        lp, getattr(self, "_ch_l", 0) * 0.9)
                                    self._ch_r = max(
                                        rp, getattr(self, "_ch_r", 0) * 0.9)
                                    if max(self._ch_l,
                                           self._ch_r) < CHANNEL_DECIDED:
                                        data = audioop.tomono(data, 2, 1, 1)
                                    else:
                                        data = (left
                                                if self._ch_l >= self._ch_r
                                                else right)
                                # UNDECIDED IS NOT "LEFT". IT IS "BOTH".
                                #
                                # The comparison below is `>=`, so before
                                # either channel has ever shown a sample
                                # the tie resolves to left — every block,
                                # for as long as left stays silent, which
                                # on a capsule wired to the right is
                                # forever. The re-check cannot rescue it
                                # either: it looks at ONE block, and if
                                # that block is quiet both maxima stay 0
                                # and the tie resolves to left again.
                                #
                                # The device, with capture finally
                                # correct at 48 kHz stereo:
                                #
                                #   blocks/sec: 46.9   (nominal 46.9)
                                #   live level: peak 0
                                #
                                # A flawless capture delivering silence —
                                # the exact signature that started all of
                                # this, one layer further in. And it must
                                # be a tie in a quiet room, because the
                                # hand measurement says only ~0.3% of
                                # SAMPLES are non-zero.
                                #
                                # So while nothing has been proven, the
                                # channels are SUMMED rather than picked.
                                # A capsule on either side is then heard
                                # at full amplitude, and the first block
                                # carrying anything breaks the tie for
                                # good. Summing is not the averaging that
                                # caused the original fault: (1+0)/2
                                # rounds to zero, 1+0 does not.
                                # "UNDECIDED" MUST BE REACHABLE AGAIN.
                                # The first version of this tested
                                # `== 0`, and the running maxima decay
                                # by multiplication: one sample of
                                # value 1 leaves 0.042 thirty
                                # re-checks later, which is not zero
                                # and never will be. So a single
                                # stray LSB latched the channel choice
                                # for the life of the process — the
                                # same permanence as the bug it
                                # replaced, arrived at from the other
                                # side. Below one count is no evidence
                                # at all; a capsule that has actually
                                # been heard sits far above it.
                                elif max(getattr(self, "_ch_l", 0),
                                         getattr(self, "_ch_r", 0)) \
                                        < CHANNEL_DECIDED:
                                    data = audioop.tomono(data, 2, 1, 1)
                                elif getattr(self, "_ch_l", 0) >= getattr(
                                        self, "_ch_r", 0):
                                    data = audioop.tomono(data, 2, 1, 0)
                                else:
                                    data = audioop.tomono(data, 2, 0, 1)
                            except Exception:
                                pass
                        ingest(data)
                finally:
                    # TERM, never KILL: a killed recorder does not run
                    # its cleanup and does not hand the PCM back.
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=3)
                        except Exception:
                            pass
                    try:
                        if proc.stdout:
                            proc.stdout.close()
                    except Exception:
                        pass
                    # DID WE END THIS RECORDER OURSELVES?
                    #
                    # close_capture() leaves the pid here before it
                    # terminates. Without that, our own teardown was
                    # reported as the microphone dying, _force_reopen
                    # was set, and the supervisor came straight back to
                    # tear down the replacement — a reopen loop with a
                    # one-second period that ran 332 times in a soak
                    # while blocks/sec sat at a perfect 47.7.
                    on_purpose = False
                    try:
                        seen = getattr(self, "_closed_on_purpose", None)
                        if seen and proc.pid in seen:
                            seen.discard(proc.pid)
                            on_purpose = True
                            # COUNTED, not silent. Suppressing the
                            # reopen must not also suppress the
                            # evidence: "reopens: 0" with the capture
                            # being torn down all day is the same
                            # under-reporting that sent a previous
                            # session hunting a caller that did not
                            # exist. One number, no log flood.
                            self._capture_closes = getattr(
                                self, "_capture_closes", 0) + 1
                    except Exception:
                        pass
                    if not self._stop.is_set() and not on_purpose:
                        # Tell the supervisor the microphone is gone so
                        # it reopens instead of going quietly deaf.
                        # _force_reopen is the loop's EXISTING, already
                        # exercised recovery path (it is what a USB
                        # replug triggers) — a dead recorder deserves
                        # exactly the same treatment, and reusing it
                        # beats inventing a second one.
                        self._capture_lost = True
                        self._force_reopen = True
                        # SAY WHY, with the recorder's own exit status.
                        # This is the reopen path that actually fires in
                        # practice, and it was the one path that recorded
                        # nothing: a report showing 33 selections and
                        # "capture reopens since start: 1" sent me
                        # looking for a caller that did not exist. The
                        # recorder dying IS the reopen.
                        try:
                            rc = proc.poll()
                        except Exception:
                            rc = None
                        if proc.poll() is None and self._stop.is_set():
                            why = "stop event set while running"
                        # Say what the recorder SAID, not just that it
                        # went. The last line of its stderr is the
                        # difference between "overrun" and "device
                        # disappeared" and "we closed it", and those
                        # want three different fixes.
                        last = ""
                        try:
                            buf = getattr(self, "_capture_errs", None) or []
                            for line in reversed(buf):
                                if str(label)[:24] in line:
                                    last = line.split(": ", 1)[-1][:90]
                                    break
                        except Exception:
                            pass
                        self._note_reopen(
                            "recorder %r ended after %d blocks (rc=%s): %s%s"
                            % (str(label)[:40], delivered, rc, why,
                               ("  | said: " + last) if last else
                               "  | said nothing"))
            threading.Thread(target=reader, daemon=True).start()
            self.mic_name = name
            return ("pipe", p)

        def open_arecord(card, device=0, deadline=None):
            """Record straight off an ALSA capture device with arecord.
            Tries, in order, every combination that fixes the common
            'records silence' cases on cheap USB mics (C-Media CM108,
            PCM2902):
              • plughw (rate/format converted) AND raw hw (some devices
                deliver zeros through the plug layer at a wrong rate)
              • native 48 k / 44.1 k / 16 k
              • mono AND stereo (mic wired to one channel only)
            The first combination that opens is used. Does NOT go
            through PipeWire. Ships in alsa-utils."""
            self.mic_card = card      # remember for the mixer readout
            self._max_capture(card)   # unmute + max this card's capture

            def attempt(sd, base, rate, ch):
                dev = "%s:%d,%d" % (base, card, sd)
                # NO -q. It suppresses arecord's own messages, and
                # arecord's own messages are the only thing that says
                # WHY it stopped. The device has been recording
                #
                #     recorder ended after 100 blocks (rc=1): unknown
                #
                # about thirty times a minute, and "unknown" is not the
                # recorder being unhelpful — it is this flag. A hand-run
                # arecord on the same card survives thirty seconds and
                # captures exactly 5,736,000 bytes, so the hardware is
                # fine and the answer is in the text we were throwing
                # away. The stderr drain already keeps the last forty
                # lines and cannot fill a pipe.
                # A BUFFER BIG ENOUGH TO SURVIVE A TRANSCRIPTION.
                #
                # The recorder's own words, from the device:
                #
                #     overrun!!! (at least 4201.874 ms long)
                #
                # and arecord exits on an overrun, so the microphone was
                # being destroyed about thirty times a minute. A
                # controlled test settles the mechanism exactly: the
                # same command piped into a prompt reader survives 25
                # seconds; piped into a deliberately slow one it
                # overruns in under one.
                #
                # It is not the downmix — measured in the app's own
                # interpreter, with the real C audioop, four passes over
                # a block cost 0.023 ms against a 21.3 ms budget. The
                # reader stalls because the whole machine is busy: a
                # decode takes about four seconds and this is a Pi.
                #
                # ALSA's default capture buffer here is about half a
                # second, so half a second of inattention loses the
                # microphone. Five seconds of buffer absorbs a decode
                # whole. It costs 960 KB of kernel memory and adds no
                # latency while anything is draining it — a buffer only
                # delays you if you stop reading, which is exactly the
                # case it exists to survive.
                # Largest buffer the card will actually install, and
                # ALSA's own default if it will install none of them.
                # A refused --buffer-time is not a warning; arecord
                # exits, which reads from the outside as "the
                # microphone does not open".
                # WHAT THIS CARD WILL ACCEPT IS A PROPERTY OF THE CARD,
                # so it is learned once and then reused. Laddering
                # inside every combination would turn a twelve-spawn
                # sweep into sixty, at 0.3 s each — a cure worse than
                # the disease, on a path whose whole history is being
                # too slow.
                bufs = getattr(self, "_cap_buf_us", None)
                if bufs is None:
                    bufs = self._cap_buf_us = {}
                if card in bufs:
                    ladder = [bufs[card]]
                else:
                    ladder = list(CAPTURE_BUFFER_LADDER)
                cap = None
                for us in ladder:
                    argv = ["arecord", "-D", dev, "-f", "S16_LE",
                            "-r", str(rate), "-c", str(ch)]
                    if us:
                        # Period stated alongside the buffer. Left to
                        # arecord it is a quarter of the buffer, which
                        # turns depth into delay and made a five-second
                        # buffer deliver its first block 1.25 s in.
                        argv += ["--buffer-time", str(us),
                                 "--period-time", str(CAPTURE_PERIOD_US)]
                    # The last rung asks for NOTHING — no buffer, no
                    # period — so ALSA's own defaults stay reachable
                    # exactly as they were before any of this.
                    argv += ["-t", "raw", "-"]
                    cap = open_pipe_cmd(
                        argv,
                        "mic arecord %s @%d %dch%s" % (
                            dev, rate, ch,
                            (" buf%.1fs/per%dms"
                             % (us / 1e6, CAPTURE_PERIOD_US // 1000))
                            if us else " default buf"),
                        native_rate=rate, channels=ch)
                    if cap:
                        bufs[card] = us
                        break
                    if deadline is not None and time.time() >= deadline:
                        break
                if cap is None and card in bufs and len(ladder) == 1:
                    # The remembered size stopped working (a replug, a
                    # different card behind the same number). Forget it
                    # so the next attempt ladders again rather than
                    # failing the same way for the rest of the process.
                    bufs.pop(card, None)
                if cap:
                    # OPENING IS NOT WORKING, AND THE CACHE COULD NOT
                    # TELL THE DIFFERENCE.
                    #
                    # This wrote the winner cache right here, on the
                    # strength of arecord having started. But every
                    # combination in the sweep goes through the plug
                    # layer, so on this card ALL of them open —
                    # including 16 kHz mono, which is the averaging
                    # path that annihilates a quiet room's noise floor.
                    # Cache that once and it is tried FIRST on every
                    # reopen, forever, and the full sweep underneath it
                    # never runs again, because the cache is only
                    # dropped when a combination FAILS TO OPEN, and
                    # this one always opens.
                    #
                    # The device showed the consequence, nine times in
                    # twenty seconds:
                    #
                    #   mic arecord plughw:5,0 @16000 1ch ended after
                    #   32 blocks: read error: Interrupted system call
                    #
                    # 32 blocks is 2.05 s, which is route_floor()'s
                    # listening window; EINTR is close_capture()'s own
                    # SIGTERM. Nothing was crashing. The walk opened a
                    # silent combination, measured peak 0, rejected it,
                    # closed it, and started again — about every two
                    # seconds, indefinitely.
                    #
                    # So the tuple is only PROPOSED here. It is
                    # promoted to the cache when the walk accepts the
                    # route as live, and dropped when the walk rejects
                    # it. A cache fed by acceptance cannot fill itself
                    # with a combination nobody can hear.
                    self._arecord_pending = (card, (sd, base, rate, ch))
                return cap

            # THE COMBINATION THAT WORKED LAST TIME, FIRST.
            #
            # Below is a 5 x 2 x 3 x 2 sweep — sixty combinations, and
            # every single one spawns an arecord and then waits to see
            # whether it survived. On this board that is the better part
            # of half a minute for a card that never opens, and the
            # cost is paid again on every reopen, having learned nothing
            # from the previous twenty.
            #
            # A microphone's working combination does not change while
            # it stays plugged into the same port. So it is remembered
            # per card and tried first; if it stops working, the full
            # sweep still runs directly underneath and re-learns. The
            # cache is cleared on hot-plug along with the mixer cache,
            # because a card number can be reused by different
            # hardware across a replug.
            win = getattr(self, "_arecord_win", None)
            if win is None:
                win = self._arecord_win = {}
            known = win.get(card)
            if known:
                cap = attempt(*known)
                if cap:
                    return cap
                # It stopped working. Forget it rather than trying it
                # first forever, and fall through to the full sweep.
                win.pop(card, None)

            # Try the requested SUBDEVICE first, then the card's other
            # capture subdevices — some USB mics put the working capture
            # on subdevice 1, not 0, so plughw:card,0 records silence.
            # ONLY THE SUBDEVICES THAT EXIST. See _capture_subdevices:
            # this card has one, and the other three were 36 doomed
            # process spawns per sweep.
            real = self._capture_subdevices(card)
            subdevs = []
            for d in [device] + real:
                if d in real and d not in subdevs:
                    subdevs.append(d)
            if not subdevs:
                subdevs = [0]
            # ── ASK THE CARD FOR ITS OWN CHANNEL COUNT, AND USE IT ───
            #
            # This list used to be (1, 2) — mono first — and that one
            # ordering is what made the station deaf in a quiet room.
            # Measured on the device, app stopped, twelve seconds each,
            # the SAME card and the SAME rate:
            #
            #   plughw:5,0 -c 1   peak 103   non-zero 740 / 570,000
            #   plughw:5,0 -c 2   peak 294   non-zero 3,308 / 1,152,000
            #
            # The AIRHUG's only native mode is 48 kHz S16_LE TWO
            # channels. Asking for one channel does not give us the
            # microphone; it asks ALSA's plug layer to AVERAGE the two.
            # A quiet room's noise floor on this capsule is one or two
            # LSB, and (1 + 0) / 2 rounds to zero — so averaging does
            # not attenuate the noise floor, it ANNIHILATES it, and
            # halves everything else including speech.
            #
            # That is the whole of "HEARING: YES, peak 0". The engine's
            # own raw tap caught 96,256 consecutive samples without a
            # single non-zero value while, seconds later, a hand
            # recording of the same device read peak 50. Both were true.
            # One had been averaged and one had not.
            #
            # So: capture at the card's NATIVE channel count and let
            # ingest() do the downmix, which takes the LOUDER channel
            # per block rather than the mean — full amplitude for a
            # capsule wired to one side, and nothing rounded away. The
            # other counts stay as fallback for hardware that refuses.
            nat = self._native_channels(card)
            chans = [nat] + [c for c in (2, 1) if c != nat]
            # AND A DEADLINE THE SWEEP ITSELF RESPECTS.
            #
            # CAPTURE_OPEN_BUDGET bounded the ROUTE WALK, checked
            # between routes — so a single route could sit inside this
            # loop for as long as it liked. It did: 64.0s against a 45s
            # budget, and the report said "route walks that hit their
            # budget: 1" while the walk had already overrun by twenty
            # seconds. A budget that is only consulted after the
            # expensive thing has finished is not a budget.
            for sd in subdevs:
                for base in ("plughw", "hw"):
                    for rate in (48000, 44100, 16000):
                        for ch in chans:
                            if deadline and time.time() > deadline:
                                self._note_reopen(
                                    "arecord sweep on card %d hit its "
                                    "deadline" % card)
                                return None
                            if known and (sd, base, rate, ch) == known:
                                continue     # just tried it
                            cap = attempt(sd, base, rate, ch)
                            if cap:
                                return cap
            return None

        def capture_is_live(seconds=1.4):
            """Drain the queue for a moment and measure real signal.

            PEAK, not RMS — the same bug route_floor() had, in a second
            place, with a harsher threshold. It asked for RMS > 5 from a
            silent room; the working microphone on this station measures
            RMS 0 and peak 29 under exactly those conditions, so a
            perfectly good capture was reported not live. See
            route_floor() for the measurements.
            """
            end = time.time() + seconds
            peak = 0
            rms = 0
            self._measuring_route = True
            try:
                while time.time() < end:
                    try:
                        data = self._audio_q.get(timeout=0.3)
                    except queue.Empty:
                        continue
                    p, r = _peak_rms(data)
                    peak = max(peak, p)
                    rms = max(rms, r)
            finally:
                self._measuring_route = False
            self.mic_rms = rms
            return peak >= ROUTE_LIVE_PEAK

        def close_capture(cap):
            """Tear a capture down COMPLETELY.

            kill() on its own is not a teardown, and this cost the
            device its microphone. Two things were missing:

            • No wait(). Every pipe capture we closed left a zombie
              recorder (`[arecord] <defunct>`) parented to us. A live
              audit found one sitting there.
            • SIGKILL, not SIGTERM. A killed arecord never runs its
              cleanup, so the ALSA PCM is not released — the audit
              found card 5 stranded in state SETUP with an owner_pid
              that no longer existed, hw_ptr frozen at 0.83 s of audio
              for 23 minutes, and every subsequent open (ours and
              anyone else's) failing EBUSY. The mic was unrecoverable
              short of restarting the app.

            So: TERM first so the recorder can release the device,
            KILL only as a fallback, always reap, always close the
            pipe the reader thread is holding.

            AND THE PORTAUDIO BRANCH DOES NOT GET TO BLOCK. It called
            stop() then close(), and stop() is Pa_StopStream, which
            waits for the device to drain. Caught live on the station
            by py-spy immediately after the probe was fixed — the
            engine had stopped hanging in device SELECTION and started
            hanging here instead, one frame further on:

                Thread 81104 (idle): "Thread-5 (_run)"
                    stop (sounddevice.py:1143)
                    close_capture (dose_voice.py:4400)
                    open_capture (dose_voice.py:4621)
                    _run (dose_voice.py:4649)

            close_capture() is called once per route during selection,
            so this is not a rare path — it is the hot one. Same
            treatment as everywhere else: abort, on a thread, joined
            briefly."""
            if not cap:
                return
            kind, h = cap
            if kind == "portaudio":
                self._shut_stream(h, "close_capture")
                return
            # SAY THAT THIS TEARDOWN WAS DELIBERATE.
            #
            # Without this the station reopens its microphone forever.
            # The reader thread's finally cannot see who ended the
            # recorder, so it treated OUR OWN terminate() as the mic
            # dying and set _force_reopen — which brings the loop
            # straight back here to terminate the replacement. One
            # legitimate reopen, from anything at all, and the station
            # spends the rest of its life doing this:
            #
            #   recorder ... ended after 47 blocks (rc=1):
            #     said: Aborted by signal Terminated...
            #   (every second, 332 times, blocks/sec a perfect 47.7)
            #
            # py-spy found the engine parked in open_pipe_cmd's own
            # settle-sleep, reached from _run -> open_capture ->
            # open_arecord -> attempt: not stuck, not crashed, just
            # opening a microphone it was about to close. Three jobs
            # went into inferring this from symptoms while the app was
            # writing the answer into selection.txt the whole time.
            #
            # Keyed by pid, not a bare flag: two teardowns can overlap
            # during a re-selection, and a flag set by one would
            # silence the other's genuine death report.
            try:
                seen = getattr(self, "_closed_on_purpose", None)
                if seen is None:
                    seen = self._closed_on_purpose = set()
                seen.add(h.pid)
                # Never let this grow without bound if a reader thread
                # dies before it can consume its entry.
                if len(seen) > 32:
                    seen.clear()
                    seen.add(h.pid)
            except Exception:
                pass
            # Pipe recorder: let it close the ALSA device itself.
            try:
                h.terminate()
            except Exception:
                pass
            try:
                h.wait(timeout=2)
            except Exception:
                try:
                    h.kill()
                except Exception:
                    pass
                try:
                    h.wait(timeout=2)      # reap, or it becomes a zombie
                except Exception:
                    pass
            try:
                if h.stdout:
                    h.stdout.close()
            except Exception:
                pass

        def open_usb_portaudio():
            """Open the plugged-in USB microphone's hardware directly
            by name. Fails when PipeWire has the device claimed —
            which is why the PipeWire route runs first."""
            try:
                devs = self._sd.query_devices()
            except Exception:
                return None
            for i, d in enumerate(devs):
                if d.get("max_input_channels", 0) < 1:
                    continue
                name = d.get("name", "")
                if not self._is_usb_name(name):
                    continue
                for rate in (int(d.get("default_samplerate") or 48000),
                             48000, 44100, SAMPLE_RATE, 8000):
                    try:
                        s = self._sd.RawInputStream(
                            device=i, samplerate=rate,
                            blocksize=int(BLOCK_SIZE * rate
                                          / SAMPLE_RATE),
                            dtype="int16", channels=1,
                            callback=callback)
                        s.start()
                        self._native_rate = rate
                        self._ratecv_state = None
                        self.mic_name = name
                        self.mic_index = i
                        return ("portaudio", s)
                    except Exception:
                        continue
            return None

        def route_floor(seconds=ROUTE_FLOOR_SECONDS,
                        patience=ROUTE_FLOOR_PATIENCE, deadline=None):
            """PEAK level from the just-opened route. A real microphone
            ALWAYS has an analog noise floor above zero; a wrong or dead
            route delivers perfect digital silence. This tells them
            apart with nobody speaking.

            IT MUST BE PEAK, NOT RMS, AND THIS IS NOT A DETAIL.

            The docstring above has always said peak. The code measured
            audioop.rms(). In a quiet room those two numbers are not
            close — they are on opposite sides of the decision. Measured
            on this station, 2026-09-18, three seconds per route with
            nobody speaking:

                card 5 (AIRHUG, the real mic) @16k   RMS 0   PEAK  29
                card 5 (AIRHUG, the real mic) @48k   RMS 0   PEAK  33
                card 5 via sysdefault                RMS 0   PEAK 107
                card 4 (dead "USB Composite")        RMS 0   PEAK   0

            RMS separates none of them. PEAK separates them perfectly.

            So every working route was scoring floor 0, failing the
            `floor > 1` test that means "this one is live", and being
            rejected. The walk then continued through every remaining
            route to the end of the list — where PortAudio device
            probing sat down inside Pa_StopStream and never got up.

            That is the entire "the microphone frequently hears
            nothing" fault, start to finish: a quiet room, a statistic
            that cannot see a noise floor, and an unbounded fallback at
            the end of the queue. Not the model, not the USB stack, not
            the recorder. One wrong function call.

            RMS is still computed and returned alongside, because it is
            the right measure for SPEECH once a route is chosen — it
            just cannot be the test for whether a device is connected.

            AND 1.6 SECONDS WAS NOT LONG ENOUGH TO ASK.

            With the channel fix in and the buffer negotiated, the
            device delivered this:

                mic:         arecord plughw:5,0 @48000 2ch
                blocks/sec:  45.9        (nominal 46.9)
                live level:  peak 11
                capture reopens: 146

            and, every 2.07 seconds without exception:

                recorder ... ended after 94 blocks (rc=1):
                  said: Aborted by signal Terminated

            A SIGTERM, on a route the heartbeat could see was live.
            The walk was killing a working microphone, over and over,
            because this function told it to: 94 blocks is 1.6 s at 47
            blocks/sec, which is exactly this window.

            The reason is measured and already written down one section
            away, about the silence watchdog: in this room only about
            1.2% of blocks carry a non-zero sample (38 of 3100). A
            1.6-second window is about 75 blocks, so the expected
            number of blocks carrying ANY signal is 0.9. Rejecting a
            microphone on that is a coin toss, and the station lost it
            about half the time, forever.

            No threshold separates quiet from dead. Time does — the
            same conclusion, reached twice in this file, and this is
            the second place that needed it.

            So a route that has not proven itself is listened to for
            longer instead of being condemned: up to
            ROUTE_FLOOR_PATIENCE, and the moment a sample clears
            ROUTE_LIVE_PEAK the listening STOPS, so a live route costs
            barely more than before and only a genuinely silent one
            pays the full patience. The walk's own deadline is passed
            in and honoured, because a budget consulted only after the
            expensive thing has finished is not a budget — also
            already learned here, also the hard way.
            """
            try:
                while True:
                    self._audio_q.get_nowait()
            except queue.Empty:
                pass
            t0 = time.time()
            floor_end = t0 + seconds
            patient_end = t0 + max(seconds, patience)
            if deadline is not None:
                # Never let a second look push the walk past its budget.
                patient_end = min(patient_end, deadline)
                floor_end = min(floor_end, deadline)
            peak = 0
            rms = 0
            seen = 0
            self._measuring_route = True
            try:
                while True:
                    now = time.time()
                    if peak >= ROUTE_LIVE_PEAK:
                        # Proven live. Nothing is learned by listening
                        # to a microphone we have already believed.
                        break
                    if now >= patient_end:
                        break
                    # NO EARLY EXIT FOR "NOTHING HAS ARRIVED YET".
                    #
                    # I wrote one, reasoning that more time cannot
                    # produce blocks that are not coming. The device's
                    # own selection trail, from the build with a
                    # five-second buffer, says otherwise:
                    #
                    #   arecord FORCED card 5,0: opened, peak 0
                    #     (rms 0, 0 BLOCKS) — DIGITALLY SILENT, rejected
                    #
                    # Zero blocks from a microphone that works, because
                    # arecord sizes its period at a quarter of the
                    # buffer: a five-second buffer means the first
                    # data arrives after 1.25 s, and a route asked for
                    # a second and a half of patience saw none of it.
                    # The route that had just been given a bigger
                    # buffer was the route most likely to be condemned
                    # by the shortcut — the two changes would have
                    # cancelled out, and the trail would have read the
                    # same as the bug it replaced.
                    #
                    # A route that never delivers costs the patience
                    # and nothing more, which is bounded and cheap.
                    # Being wrong about a real microphone is neither.
                    try:
                        data = self._audio_q.get(timeout=0.4)
                    except queue.Empty:
                        continue
                    seen += 1
                    p, r = _peak_rms(data)
                    peak = max(peak, p)
                    rms = max(rms, r)
            finally:
                self._measuring_route = False
            self._last_route_rms = rms
            self._last_route_blocks = seen
            self._last_route_secs = time.time() - t0
            return peak

        def open_capture():
            """WHAT THE PI HAS SET UP, first: the system-default
            capture route, exactly what the OS's own tools use. Then
            every other route in turn — and the FIRST one that shows
            a real noise floor is kept. No guessing: a live mic is
            never digitally silent; a wrong route always is. The
            full trail of what was tried and what each route heard
            goes into the mic report."""
            self._kick_audio_services()
            self._unmute_alsa_inputs()
            self._pa_refresh()   # see USB devices plugged in after launch
            target = self._pick_input_target()   # unmute + boost USB
            # ARECORD FIRST: straight to the USB mic's ALSA card,
            # zero PipeWire in the path — lowest latency, and the
            # method the mic's own vendor documents. Every capture
            # card the kernel sees, USB ahead of the built-ins.
            # Each route: (label, opener, is_speakerish). A card whose
            # name looks like a pure OUTPUT device (a USB speaker such
            # as the Jieli UAC demo) may also expose a dead capture
            # endpoint — it is deprioritized so a real microphone on
            # another card always wins the tie.
            # THE WALK'S DEADLINE, DECIDED BEFORE THE WALK STARTS, so
            # each opener can be told about it. It used to be computed
            # after the route list was built and consulted only BETWEEN
            # routes, which let one route sit inside its own sweep for
            # as long as it liked — 64.0s against a 45s budget, with
            # the report still saying the budget had been respected.
            _walk_box = [time.time() + CAPTURE_OPEN_BUDGET]

            def walk_deadline():
                return _walk_box[0]

            routes = []
            cap_cards = self._alsa_capture_cards()
            # The full self-test may have found the exact card that
            # hears — try it FIRST, but ONLY if it's still a present
            # capture device (a USB port swap changes card numbers, so
            # a stale pin must never be trusted).
            fc = self._forced_card
            if fc and any(c[0] == fc[0] and c[1] == fc[1]
                          for c in cap_cards):
                routes.append(
                    ("arecord FORCED card %d,%d" % (fc[0], fc[1]),
                     (lambda c=fc[0], d=fc[1]:
                      open_arecord(c, d, deadline=walk_deadline())),
                     False))
            else:
                # The pinned/forced card is not in the capture list
                # right now. Deliberately do NOTHING here: re-pinning
                # would make a stale pin permanent, and clearing it
                # would throw away an explicit DOSE_MIC_CARD the moment
                # the device blinked. Skip the forced route for this
                # pass; the loop below auto-selects, and the pin is
                # honoured again as soon as the card comes back.
                pass
            for card_num, dev_num, desc in cap_cards:
                short = desc.split("[")[0].strip() or desc[:20]
                tag = "card %d,%d %s" % (card_num, dev_num, short)
                routes.append(
                    ("arecord %s" % tag.strip(),
                     (lambda c=card_num, d=dev_num:
                      open_arecord(c, d, deadline=walk_deadline())),
                     self._looks_like_speaker(desc)))
            # TRUE SYSTEM DEFAULTS — route through whatever the Pi is
            # configured to use (these go via ALSA's 'default'/PipeWire
            # plugin, which on modern Pi OS is often the ONLY path that
            # actually delivers audio when raw plughw fights PipeWire).
            def arec(dev, name, ch=1):
                return open_pipe_cmd(
                    ["arecord", "-D", dev, "-f", "S16_LE", "-r",
                     "48000", "-c", str(ch), "-t", "raw", "-q", "-"],
                    name, native_rate=48000, channels=ch)
            default_routes = [
                ("arecord default", lambda: arec("default",
                                                 "arecord default"),
                 False),
                ("arecord plughw default", lambda: arec(
                    "plughw:CARD=default", "arecord plughw default"),
                 False),
            ]
            for card_num, dev_num, desc in cap_cards:
                default_routes.append(
                    ("arecord sysdefault card %d" % card_num,
                     (lambda cn=card_num: arec(
                         "sysdefault:CARD=%d" % cn,
                         "arecord sysdefault card %d" % cn)),
                     self._looks_like_speaker(desc)))
            routes += default_routes
            routes += [
                ("system default (pw-record)", lambda: open_pipe_cmd(
                    ["pw-record", "--rate", "16000", "--channels",
                     "1", "--format", "s16", "-"],
                    "system default (pw-record)"), False),
                ("system default (parec)", lambda: open_pipe_cmd(
                    ["parec", "--rate=16000", "--format=s16le",
                     "--channels=1", "--latency-msec=20"],
                    "system default (parec)"), False),
                ("USB hardware direct", open_usb_portaudio, False),
                ("portaudio default", open_portaudio, False),
            ]
            if target:
                routes += [
                    ("targeted %s (pw-record)" % target,
                     (lambda: open_pipe_cmd(
                         ["pw-record", "--target", target, "--rate",
                          "16000", "--channels", "1", "--format",
                          "s16", "-"], "targeted " + target)), False),
                    ("targeted %s (parec)" % target,
                     (lambda: open_pipe_cmd(
                         ["parec", "-d", target, "--rate=16000",
                          "--format=s16le", "--channels=1",
                          "--latency-msec=50"],
                         "targeted " + target)), False),
                ]
            self.mic_trail = []
            # A live non-speaker route now returns its stream directly
            # from inside the loop, so there is no `live` to carry out
            # of it any more. These two are the fallbacks: a live route
            # that looks like a speaker's dead capture endpoint, and a
            # route that opened but showed nothing. Both are re-opened
            # because their first stream was closed on the way past.
            live_speaker = None  # live but looks like a speaker's endpoint
            first_openable = None
            # A DEADLINE ON THE WHOLE WALK. There are a dozen-plus routes
            # here and each costs an open, route_floor() seconds of
            # listening, and a close. Unbounded, a bad day means the
            # engine spends half a minute per reopen deciding — which is
            # precisely the "capture cycling" a soak of this station
            # showed, misread at the time as the recorder crash-looping.
            # It was not crashing. It was shopping.
            t_walk = time.time()
            # Re-stamp: building the route list costs an `arecord -l`
            # and a few /proc reads, and the walk's clock should start
            # when the walk does.
            _walk_box[0] = walk_end = t_walk + CAPTURE_OPEN_BUDGET
            for label, opener, speakerish in routes:
                if time.time() >= walk_end:
                    self._select_timeouts = getattr(
                        self, "_select_timeouts", 0) + 1
                    self.mic_trail.append(
                        "... budget of %.0fs spent; %d route(s) not tried"
                        % (CAPTURE_OPEN_BUDGET,
                           len(routes) - len(self.mic_trail)))
                    break
                # Clear any combination proposed by the PREVIOUS route
                # before this one opens, so an acceptance or rejection
                # is only ever credited to the opener that earned it.
                self._arecord_propose()
                cap = opener()
                if not cap:
                    self.mic_trail.append(label + ": could not open")
                    continue
                # The walk's own budget bounds the patience, so giving
                # a quiet route a fair hearing can never cost the walk
                # its deadline.
                floor = route_floor(deadline=walk_end)
                # HOW LONG IT WAS LISTENED TO IS PART OF THE VERDICT.
                # "peak 0, rejected" was written identically whether
                # the route had been given 1.6 s or none at all, and
                # the difference between those two is the difference
                # between a dead endpoint and a walk that ran out of
                # budget — which is a whole session of looking in the
                # wrong place.
                self.mic_trail.append(
                    "%s: opened, peak %d (rms %d, %d blocks in %.1fs)%s%s"
                    % (label, floor,
                       getattr(self, "_last_route_rms", 0),
                       getattr(self, "_last_route_blocks", 0),
                       getattr(self, "_last_route_secs", 0.0),
                       "" if floor >= ROUTE_LIVE_PEAK
                       else " — DIGITALLY SILENT, rejected",
                       " (output device?)" if speakerish else ""))
                if floor >= ROUTE_LIVE_PEAK and not speakerish:
                    # ── KEEP THE STREAM WE ALREADY HAVE ──────────────
                    # This used to close the capture and then call the
                    # same opener again, reopening the same device a
                    # fraction of a second later. That is a race against
                    # the kernel releasing a USB PCM, and it is a race
                    # this station lost in a loop.
                    #
                    # The device's own report, selection #33 in three
                    # minutes: selection took 2.8s, chose "arecord
                    # FORCED card 5,0", verdict "a route showed a real
                    # noise floor" — correct every time, and then
                    # immediately thrown away and done again. Meanwhile
                    # the recorder's stderr filled with
                    #
                    #   arecord: pcm_read:2272: read error:
                    #       Interrupted system call
                    #
                    # every one to three seconds, which is EINTR, which
                    # is fatal to arecord: our own SIGTERM to the probe
                    # recorder, printed by the process we had just
                    # decided to trust and then killed. And
                    #
                    #   arecord sysdefault card 4: audio open error:
                    #       Device or resource busy
                    #
                    # which is the reopen arriving before the kernel had
                    # let go of the previous one.
                    #
                    # There was never a reason to close it. The stream
                    # is open, it is the one we just measured, and it is
                    # already feeding the queue. Keep it: no second
                    # open, no EBUSY race, no self-inflicted EINTR, and
                    # about two seconds off every selection.
                    self.mic_name = (
                        label + " · selected (speak to test)")
                    # This route proved itself. Only now is its
                    # combination worth remembering for the next reopen.
                    kept = self._arecord_confirm()
                    if kept:
                        self.mic_trail.append(
                            "  remembered %s for card %d (proved live, "
                            "peak %d)" % (kept[1], kept[0], floor))
                    self._dump_selection(label, True, t_walk)
                    return cap
                # Measured, and there was nothing there. Evict the
                # combination rather than letting a cache hand back the
                # same silence on every reopen for the rest of the
                # process's life.
                dropped = self._arecord_reject()
                if dropped:
                    self.mic_trail.append(
                        "  forgot %s for card %d (opened, heard nothing)"
                        % (dropped[1], dropped[0]))
                close_capture(cap)
                if first_openable is None:
                    first_openable = (label, opener)
                if floor >= ROUTE_LIVE_PEAK and speakerish:
                    if live_speaker is None:
                        live_speaker = (label, opener)
            choice = live_speaker or first_openable
            is_live = bool(live_speaker)
            if not choice:
                self.mic_trail.append("no capture route opened at all")
                # NOT ONE route opened. A dead pipewire-pulse is one of
                # the few things that does this, so drop the
                # service-kick and mixer caches: the next attempt pays
                # for a real `systemctl start` and a real unmute sweep
                # rather than skipping them because a healthy run
                # skipped them minutes ago. Caching is for the happy
                # path; this is not it.
                self._kicked_at = 0.0
                self._mixer_cache_clear()
                self._dump_selection(None, False, t_walk)
                return None
            cap = choice[1]()
            if cap:
                # Honest label: the idle floor is NOT proof the mic
                # hears you — only the live meter (speaking) is. So we
                # never claim 'hearing OK' from selection; the meter's
                # Loudest value is the sole verdict.
                self.mic_name = choice[0] + " · selected (speak to test)"
            self._dump_selection(choice[0] if cap else None,
                                 is_live, t_walk)
            return cap

        stream = open_capture()
        if stream is None:
            self.available = False
            self.reason = "microphone failed to open"
            return

        last_audio = time.time()
        last_devscan = 0.0
        last_reselect = time.time()
        dev_sig = None
        while not self._stop.is_set():
            # (The heartbeat has its own thread now — see start(). It
            # used to be written from here, and therefore stopped being
            # written for the whole of every turn.)
            # Pause: the full self-test needs exclusive access to every
            # capture device, so it closes our stream and idles here
            # until the test is done, then reopens.
            if self._pause_capture:
                if stream is not None:
                    close_capture(stream)
                    stream = None
                self._paused_ack = True   # device is now released
                time.sleep(0.2)
                continue
            self._paused_ack = False
            if stream is None:
                stream = open_capture()
                last_audio = time.time()
                if stream is None:
                    time.sleep(1)
                    continue
            # Hot-plug watch: a USB/Bluetooth mic or speaker appearing
            # (or vanishing) changes the device fingerprint — redo
            # selection immediately so new hardware just works.
            now = time.time()
            if now - last_devscan > 8 and self.state == "idle":
                last_devscan = now
                sig = self._audio_sig()
                if (sig is not None and dev_sig is not None
                        and sig != dev_sig):
                    # devices changed (plug/unplug OR a USB port swap
                    # that reassigns card numbers) — drop any pinned
                    # card and re-find the mic wherever it now lives
                    self._out_cache = None
                    # Back to the PIN if one is set, otherwise to
                    # auto-selection. A replug must not leave us on a
                    # card that was chosen by inference last time.
                    self._forced_card = MIC_CARD_PIN
                    self._force_reopen = True
                    # New hardware, or the same hardware on a different
                    # card number. Either way the mixer cache is about a
                    # machine that no longer exists — and a card NUMBER
                    # can be reused by a different device across a
                    # replug, so a per-card skip would be actively wrong
                    # rather than merely stale.
                    self._mixer_cache_clear()
                    # Same reason: the remembered arecord combination is
                    # per card NUMBER, and that number may now belong to
                    # a different microphone.
                    try:
                        self._arecord_win = {}
                    except Exception:
                        pass
                    self._note_reopen(
                        "audio device fingerprint changed (hot-plug)")
                if sig is not None:
                    dev_sig = sig
                # a silent mic: keep re-trying — the live one may
                # have just been plugged in
                if (any(k in (self.mic_name or "")
                        for k in ("(no signal", "SILENT"))
                        and now - last_reselect > 20):
                    self._force_reopen = True
                    self._note_reopen(
                        "mic_name says no signal (%r) — re-selecting"
                        % (self.mic_name or "",)[:60])
            if self._force_reopen:
                self._force_reopen = False
                # COUNT IT. This path reopens the capture exactly like
                # the dead-capture watchdog does, but only that other
                # path incremented the counter — so the heartbeat read
                # "capture reopens: 0" while the silence ladder had just
                # torn the stream down three times. A diagnostic that
                # under-reports is worse than one that is absent,
                # because it is believed.
                self._capture_restarts = getattr(
                    self, "_capture_restarts", 0) + 1
                close_capture(stream)
                stream = open_capture()
                last_audio = time.time()
                last_reselect = time.time()
                self._last_live_peak_ts = time.time()
                self._hb_live_blocks = 0   # new stream, new evidence
                self._hb_live_blocks = 0   # new stream, new evidence
                if stream is None:
                    time.sleep(3)
                    continue
            # PUSH-TO-TALK: holding the Dose logo starts a listening
            # session directly, no wake word needed — the reliable way
            # in when "Hey Dose" isn't being detected.
            # the guided self-test takes a turn through the SAME path
            if getattr(self, "_st_want", False) and self.state == "idle":
                self._drain(rec)
                self._set_ui_state("listening")
                text = self._listen_command(rec, timeout=12)
                blocks = max(1, getattr(self, "_turn_blocks", 1))
                self._st_result = {
                    "heard": text or "",
                    "vosk": getattr(self, "_raw_vosk", ""),
                    "fast_text": getattr(self, "_raw_fast", ""),
                    "slow_text": getattr(self, "_raw_slow", ""),
                    "fast": getattr(self, "_t_fast", 0.0),
                    "slow": getattr(self, "_t_slow", 0.0),
                    "total": getattr(self, "_t_endpoint", 0.0)
                    + getattr(self, "_t_fast", 0.0)
                    + getattr(self, "_t_slow", 0.0),
                    "secs": getattr(self, "_turn_secs", None)
                    if getattr(self, "_turn_secs", None) is not None
                    else round(blocks * BLOCK_SIZE
                               / float(SAMPLE_RATE), 1),
                    "peak": getattr(self, "_turn_peak", 0),
                    "clip_pct": round(100.0
                                      * getattr(self, "_turn_clip", 0)
                                      / blocks),
                    "snr": round(getattr(self, "_turn_snr", 0.0), 1),
                }
                self._set_ui_state("idle")
                continue

            # ── A TEST HOOK, OFF BY DEFAULT ──────────────────────
            #
            # Everything about a turn's timing — endpointing, the fast
            # pass, the escalation, time to first sound — is only
            # measurable on a REAL turn, and a real turn starts when
            # somebody holds the logo. Ryan's standing instruction is
            # that nothing may type or tap on the Pi's touchscreen, so
            # until now the only way to measure a turn was to ask him
            # to stand in front of it.
            #
            # With DOSE_TEST_HOOKS=1, touching voice/ptt_request starts
            # a turn exactly as a held logo does — same code path, same
            # measurements, no special case anywhere below this line.
            #
            # It is OFF unless that variable is set, and the flag lives
            # inside APP_DIR, which is under a home directory with mode
            # 0700: only the kiosk user and root can create it. It adds
            # no listener, no port and no network path. On a station in
            # somebody's kitchen the variable is absent and this is
            # four lines that never run.
            if TEST_HOOKS and self.state == "idle":
                try:
                    hook = os.path.join(VOICE_DIR, "ptt_request")
                    if os.path.exists(hook):
                        os.remove(hook)
                        self._ptt_requested = True
                except Exception:
                    pass
            if self._ptt_requested and self.state == "idle":
                self._ptt_requested = False
                self._drain(rec)
                self._set_ui_state("listening")
                acks = getattr(self, "_ack_files", [])
                if acks:
                    line, path = random.choice(acks)
                    self._last_reply = line
                    self._set_ui_state("speaking", reply_text=line)
                    self._play_wav(path)
                    self._set_ui_state("listening")
                else:
                    self._chime()
                command = self._listen_command(rec)
                if command:
                    self._handle_exchange(rec, command)
                else:
                    # HEARD NOTHING. This is the failure that matters
                    # most and it used to leave no trace at all — the
                    # log would simply have a gap where a turn should
                    # be. Now it is recorded with the audio conditions,
                    # so "it never hears me" can be read as numbers.
                    blocks = max(1, getattr(self, "_turn_blocks", 1))
                    self._log_turn({
                        "at": time.strftime("%H:%M:%S"),
                        "heard": "", "vosk": getattr(self, "_partial", ""),
                        "fast_text": "", "slow_text": "",
                        "engine": "-", "model": "-", "intent": "-",
                        "understood": False,
                        "reply": "NOTHING HEARD",
                        "endpoint": 0, "fast": 0, "slow": 0,
                        "speak": 0, "total": 0,
                        "room": round(getattr(self, "_nfloor", 0)),
                        "voice": round(getattr(self, "_speech_level", 0)),
                        "peak": getattr(self, "_turn_peak", 0),
                        "clip_pct": round(
                            100.0 * getattr(self, "_turn_clip", 0)
                            / blocks),
                        "snr": round(getattr(self, "_turn_snr", 0.0), 1),
                        "secs": getattr(self, "_turn_secs", None)
                        if getattr(self, "_turn_secs", None) is not None
                        else round(blocks * BLOCK_SIZE
                                   / float(SAMPLE_RATE), 1),
                    })
                    self._speak("I didn't catch that, Ryan. "
                                "Tap the logo and try again.")
                    self._set_ui_state("idle")
                self._drain(rec)
                continue
            # CAPTURE WATCHDOG — "the device stopped delivering", NOT
            # "nobody has spoken".
            #
            # This used to key off last_audio, which is only stamped
            # when a block comes OFF the queue. In a quiet room the
            # gate means nothing reaches the queue, so a perfectly
            # healthy microphone looked dead after ten seconds and the
            # loop tore it down and reopened it. Reopening takes longer
            # than ten seconds on this hardware, so the clock was
            # already expired when the new stream came up and it did it
            # again immediately. Measured on the device: the capture
            # was ABSENT for two thirds of a four-minute window,
            # cycling the whole time — and every one of those teardowns
            # was another chance to strand the PCM.
            #
            # _last_block_ts is stamped in ingest() for every block the
            # device hands us, before any gate, mute or speaking check.
            # That is the real liveness signal. A Bluetooth mic that
            # drops, or a USB mic that stops delivering, still trips
            # this; a silent room no longer does.
            lb = getattr(self, "_last_block_ts", 0.0) or last_audio
            if (time.time() - lb > CAPTURE_DEAD_AFTER
                    and self.state == "idle"
                    and self._capture_restart_allowed()):
                # SAY WHY. A reopen counter tells you a station is
                # cycling; it does not tell you what tore the capture
                # down, and there are four different things that can.
                # Without the reason, the only way to tell "the device
                # stopped delivering blocks" from "a hot-plug was
                # detected" from "the self-test asked for the device"
                # is to instrument a live process, which is how this
                # afternoon was spent.
                self._note_reopen("no audio block for %.1fs "
                                  "(threshold %.0fs)"
                                  % (time.time() - lb, CAPTURE_DEAD_AFTER))
                close_capture(stream)
                stream = open_capture()
                last_audio = time.time()
                self._last_block_ts = time.time()
                self._last_live_peak_ts = time.time()
                self._hb_live_blocks = 0   # new stream, new evidence
                if stream is None:
                    time.sleep(3)
                    continue
            # DIGITAL-SILENCE WATCHDOG — "the device is delivering, and
            # every sample is zero". Completely different fault from the
            # one above, invisible to it, and the reason this station
            # spent an evening reporting HEARING: YES while deaf. See
            # SILENT_CAPTURE_AFTER.
            self._silence_note_level(getattr(self, "_hb_peak", 0))
            _sstep = self._silence_due()
            if _sstep is not None:
                self._silence_recover(_sstep)
            try:
                data = self._audio_q.get(timeout=0.5)
                last_audio = time.time()
            except queue.Empty:
                continue
            if self._muted:
                continue

            # dedicated wake-word detector (when a model is configured):
            # a hit starts a listening session exactly like hold-to-talk
            if self.state == "idle" and self._oww_feed(data):
                self._ptt_requested = True
                continue

            # ── THE BIGGEST THING THIS DEVICE WAS DOING ──────────
            # Below is a full speech recogniser, and it was running on
            # EVERY audio block, forever, for one purpose: to notice
            # the words "hey dose". A continuous ASR decode is roughly
            # a quarter of a Pi core, permanently — for a wake word
            # that never worked reliably and that nobody uses, because
            # the way into a conversation is tapping the logo.
            #
            # So it does not run unless the wake word is actually
            # switched on. Tapping the logo is handled above and is
            # free: it sets a flag. When idle with no wake word, this
            # thread costs a queue read and an RMS.
            if self.state == "idle" and not WAKE_WORD:
                continue

            # AND NOT WHILE WE ARE ANSWERING.
            #
            # Vosk exists to put words on screen while somebody is
            # SPEAKING. Once endpointing has fired, the words are
            # already captured and its output is thrown away — but it
            # kept decoding every 21 ms block right through the
            # transcription and the reply, on the same four cores
            # Piper renders on.
            #
            # The device made the cost visible once render_to_cache
            # timed its own parts:
            #
            #   tts: {'cache': 0.0002, 'hit': 0, 'synth': 1.22, ...}
            #   tts: {'cache': 0.001,  'hit': 0, 'synth': 4.37, ...}
            #
            # The lookup and the write are microseconds; it is all
            # synthesis. And the same sentence measured 0.72 s
            # standalone with the app running — but IDLE, which is the
            # flaw in that comparison. During a turn this loop is
            # decoding continuously, and in this room 54% of blocks now
            # carry signal, so it is decoding hard.
            #
            # Nothing is lost: a block arriving while the station is
            # thinking or talking is not part of the question that was
            # asked, and barge-in does not use Vosk — it runs its own
            # detector in ingest() and never reaches this queue.
            if self.state in ("thinking", "speaking"):
                continue

            got_final = rec.AcceptWaveform(data)
            if got_final:
                text = json.loads(rec.Result()).get("text", "").strip()
            else:
                text = json.loads(rec.PartialResult()).get(
                    "partial", "").strip()

            wake_rest = self._match_wake(text)
            if wake_rest is None:
                continue

            # Wake word heard. If the same utterance already carries a
            # command ("hey dose what's next"), wait for the final and
            # use the remainder directly.
            if not got_final:
                text = self._finish_utterance(rec, first_wait=2.5)
                wake_rest = self._match_wake(text)
                if wake_rest is None:
                    wake_rest = text or ""

            if wake_rest.strip():
                self._handle_exchange(rec, wake_rest.strip())
            else:
                self._set_ui_state("listening")
                acks = getattr(self, "_ack_files", [])
                if acks:
                    line, path = random.choice(acks)
                    self._last_reply = line
                    self._set_ui_state("speaking", reply_text=line)
                    self._play_wav(path)
                    self._set_ui_state("listening")
                else:
                    self._chime()
                command = self._listen_command(rec)
                if command:
                    self._handle_exchange(rec, command)
                else:
                    self._speak("Standing by, Ryan.")
                    self._set_ui_state("idle")
            self._drain(rec)

        # Non-blocking teardown, for the same reason as everywhere else
        # in this file: a Pa_StopStream that does not return here leaves
        # this thread unable to exit, systemd's stop times out, the app
        # is SIGKILLed, and a SIGKILLed capture strands the ALSA PCM —
        # which is the fault that started this whole audit.
        try:
            self._shut_stream(stream, "engine-exit")
        except Exception:
            pass
        # And take the synthesis worker with us. An orphaned worker
        # holds an ONNX session and its share of the board for nothing,
        # and this project already has one hard-won lesson about child
        # processes outliving their parent and holding a device hostage.
        self.stop_piper_worker()

    def _match_wake(self, text):
        """Return the words after the wake phrase, or None."""
        if not text:
            return None
        t = " " + text.lower() + " "
        # Vosk frequently hears "hey" as "hay" / "hey day" etc.
        t = t.replace(" hay ", " hey ").replace(" hae ", " hey ")
        t = t.replace(" they dose ", " hey dose ")
        t = t.replace(" a those ", " a dose ")
        for pat in WAKE_PATTERNS:
            idx = t.find(" " + pat + " ")
            if idx == -1:
                idx = t.find(" " + pat)
                if idx == -1 or len(t) - idx > len(pat) + 3:
                    continue
            return t[idx + len(pat) + 1:].strip()
        return None

    def _finish_utterance(self, rec, first_wait=2.5):
        """Keep feeding audio until Vosk closes the utterance."""
        deadline = time.time() + first_wait
        while time.time() < deadline:
            try:
                data = self._audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if rec.AcceptWaveform(data):
                return json.loads(rec.Result()).get("text", "").strip()
        return json.loads(rec.FinalResult()).get("text", "").strip()

    def _endpoint_wait(self, text):
        """How long to keep waiting after the person goes quiet.

        This is the difference between an assistant that talks over you
        and one that feels like it is listening. A finished command
        commits almost immediately; a sentence that is obviously still
        being formed gets real time. See the constants above for the
        measured figures and why the mid-thought one is so long."""
        t = (text or "").strip().lower()
        if not t:
            return ENDPOINT_UNPARSED
        words = t.split()
        dangling = (words[-1] in HANGING_WORDS
                    or (len(words) <= SHORT_FRAGMENT_MAX
                        and words[-1] in SHORT_FRAGMENT_WORDS))

        if _nlu_mod is None:
            return ENDPOINT_DANGLING if dangling else ENDPOINT_UNPARSED
        try:
            intent = _nlu_mod.parse(t, self._med_names())
        except Exception:
            return ENDPOINT_DANGLING if dangling else ENDPOINT_UNPARSED

        # never make someone in trouble say it twice
        if intent.name in ("crisis", "emergency"):
            return 0.0

        # ASK THE MATCHER THAT WILL ACTUALLY ANSWER IT.
        #
        # The endpointer was consulting only the pattern set, while the
        # replies come from a wider matcher — navigation, the time, the
        # personality lines, the add-medication flow all live there. So
        # "open storage" and "how many pills do i have" were judged
        # incomplete and sat waiting, even though the station knew
        # perfectly well how to answer them the moment they were said.
        complete = intent.complete
        if not complete:
            try:
                norm = " " + re.sub(r"[^a-z0-9' ]", " ",
                                    t.lower()).strip() + " "
                norm = re.sub(r"\s+", " ", norm)
                hit = self._match_builtin(norm)
                complete = bool(hit and hit[0])
            except Exception:
                pass

        # A FRAGMENT IS NOT A SENTENCE, however well it parses.
        #
        # "What" on its own matches the pattern for "say that again"
        # and therefore counted as a COMPLETE command — so the turn
        # committed 0.35 s after it, and "what time is it" was answered
        # as "what". That is the exact failure reported from the
        # device.
        #
        # Length decides. One to three words ending on a function word
        # is somebody mid-sentence, whatever it happens to match; four
        # or more that parse cleanly can be trusted to stand alone. It
        # also fixes the other direction — "how many pills do i have"
        # ends on "have" and was waiting the full 2.2 s for no reason.
        # Judged on the STRICT parse. The wider matcher below includes
        # a phonetic fallback, and letting a loose sound-alike match
        # shorten the mid-thought grace is how a half-finished
        # sentence gets answered.
        if dangling and (len(words) < 4 or not intent.complete):
            return ENDPOINT_DANGLING

        # room to change their mind: "my metformin — no wait, the other"
        if intent.name in ("dispense", "cancel"):
            return ENDPOINT_CORRECTION
        # a complete, unambiguous command: answer now
        if complete:
            return ENDPOINT_STABLE
        return ENDPOINT_UNPARSED

    def _listen_command(self, rec, timeout=COMMAND_TIMEOUT):
        """Capture one utterance; empty string on timeout.

        Conversational endpointing: Vosk's own end-of-utterance is
        conservative and can sit on a finished sentence for a second or
        more, which is exactly what makes an assistant feel like it is
        making you wait. So we watch the mic's energy directly — once
        the user has actually said something and the input has been
        back at the ambient floor for ENDPOINT_SILENCE, we close the
        utterance ourselves and start thinking immediately."""
        self._set_ui_state("listening")
        # audio quality for THIS turn — is it hearing clean speech or
        # distorted mush?
        self._turn_clip = 0
        self._turn_blocks = 0
        self._turn_peak = 0
        self._turn_snr = 0.0
        self._t_spec_wait = 0.0
        self._turn_secs = None
        self._cap_note = ""
        deadline = time.time() + timeout
        buf = bytearray()
        heard = False
        speech_started = time.time()
        final_parts = []      # what the live listener has finalised
        # speculation is keyed on the LAST MOMENT REAL SPEECH WAS HEARD,
        # not on the byte count: the buffer keeps growing with silence
        # while we wait, and trailing silence cannot change what was
        # said. So as long as no new speech has arrived, a speculation
        # started during this pause is still valid.
        spec = {}        # {"voice_ts": float, "done": Event, "text": str}
        # ignore any speech energy from before this turn started
        self._last_voice_ts = 0.0

        def speculate(snapshot, hint, voice_ts):
            """Transcribe what we have so far, on a worker, while we
            are still listening. Discarded for free if more speech
            turns up."""
            ev = threading.Event()
            # `remote` is recorded HERE, at the start, not read from
            # the result. finish() has to decide whether this pass is
            # worth waiting for BEFORE it has finished — the endpoint
            # fires 0.35 s after the last voice and this starts at
            # 0.18 s, so there is a sixth of a second of head start on
            # a round trip that takes the best part of a second.
            #
            # I got that wrong once already: the reuse test asked who
            # ANSWERED it, which is unknowable until it has, so the
            # answer was always "nobody" and every turn paid for a
            # second full pass. The device read `spec hits: 0` and
            # stt 1.08-1.49 s against a 0.85 s baseline.
            box = {"voice_ts": voice_ts, "done": ev, "text": "",
                   "remote": bool(_remote_stt is not None
                                  and _remote_stt.available())}

            def work():
                # DO NOT DEPRIORITISE THE THREAD WE THEN WAIT ON.
                #
                # This used to call os.nice(5), reasoning that a missed
                # QR decode was worse than "a few milliseconds of extra
                # speech latency" and that the work was speculative
                # anyway. Both halves stopped being true.
                #
                # The speculation is not discarded in the common case —
                # finish() WAITS for it and uses its answer, and the
                # device logs three hits to two misses. So the thread
                # the turn blocks on was the one thread told to yield.
                #
                # And it is not milliseconds. Measured on the station,
                # with the stage timings now in turns.jsonl:
                #
                #     fast 4.31s   of which decode 4.08s   audio 1.84s
                #
                # while the same model on the same board, measured
                # standalone, runs at roughly 0.7x real time. Being
                # niced against Vosk (which decodes every block at
                # normal priority) is most of that gap.
                #
                # The camera argument is also gone: pyzbar is
                # duty-cycled to an eight-frame burst every five
                # minutes, so it is idle for essentially all of any
                # turn. Nothing is being protected by this any more.
                pass
                try:
                    # THIS PASS DOES NOT GET TO WRITE THE TURN LOG.
                    # See _recording(): it runs concurrently with the
                    # real pass, and whichever finishes last was
                    # stamping its name on the turn. The Mac answered
                    # three turns; the log credited it with one.
                    _TL.speculative = True
                    # NO CLOUD, BUT YES THE MAC.
                    #
                    # This pass stays off the metered cloud because it
                    # may be discarded if more speech arrives, and
                    # spending a free-tier quota on a throwaway is
                    # wasteful. That reasoning has nothing to do with
                    # a Mac sitting idle on this LAN: it has no quota,
                    # it is doing nothing between turns, and the whole
                    # point of speculating is to have the answer
                    # already in hand when the person stops talking.
                    #
                    # With it local, every turn in the device's log
                    # shows `spec_hit 0` and pays 0.8-1.0 s of STT
                    # after the endpoint fires. With it on the Mac,
                    # that work happens DURING the pause.
                    box["text"] = self._better_transcribe(
                        snapshot, hint, allow_cloud=False,
                        allow_remote=True, fast_remote=True)
                    # WHO ANSWERED IT, so finish() can tell whether
                    # this is worth reusing. Not self._last_engine —
                    # that is guarded by _recording() precisely so this
                    # thread cannot write it, which is the right rule
                    # and the reason a separate channel is needed.
                    box["by"] = getattr(_TL, "engine", "")
                except Exception:
                    box["text"] = ""
                finally:
                    _TL.speculative = False
                ev.set()
            threading.Thread(target=work, daemon=True,
                             name="stt-speculate").start()
            return box

        def finish(final_buf, hint):
            """The transcript for this turn.

            When cloud STT is the primary path, do the real cloud pass
            here — the local speculation was only for the live on-screen
            text and is not as accurate. When offline (or in local
            mode), reuse the local speculation if no new speech arrived
            since it started: that is the latency win, and it is only
            safe to claim when we were not going to call the cloud."""
            # ANSWER NOW IF THE LIVE TRANSCRIPT ALREADY SAYS ENOUGH.
            # See _quick_answer(). This is the difference between a
            # station that replies in about a second and one that
            # replies in seven, on the commands people actually use.
            #
            # BUT NOT WHEN THE MAC IS THERE.
            #
            # This shortcut is why the Mac's hit counter sat at zero
            # while the Mac was paired, running and answering in 1.76 s:
            # Vosk's live text got there first on exactly the phrases
            # people say most, so the better recogniser was never asked
            # anything. Ryan: "have it put more emphasis so that it
            # focuses on the mac first more heavily."
            #
            # The Mac is asked when the Mac is there. The shortcut is
            # what happens when it is not — which is the same shape as
            # every other decision in this file: the local path is the
            # parachute, not the plan. It costs about a second on those
            # phrases and buys a recogniser several sizes larger, and
            # `_remote_ready()` is a cached flag, not a network call, so
            # asking it here costs nothing.
            quick = None if self._remote_ready() else self._quick_answer(hint)
            if quick:
                self._t_fast = 0.0
                self._t_slow = 0.0
                self._raw_fast = ""
                self._raw_slow = ""
                self._last_engine = "vosk (live)"
                self._stt_note = "answered from the live transcript"
                return quick
            # THE MAC COUNTS AS "SOMEWHERE BETTER TO ASK".
            #
            # This asked only about the cloud, so with no cloud
            # credential it was always False — and the branch below
            # then RETURNED the local speculation, which is
            # whisper-tiny.en running on the Pi. _better_transcribe,
            # where the Mac is asked, was never reached.
            #
            # That is why the device read, after pairing, health at
            # 71 ms and everything green:
            #
            #   Mac speech server: MAC   turns answered by Mac: 0
            #   ...
            #   heard='what do i take today'  engine=whisper-tiny.en
            #   heard='how many pills do i have'  fast=8.40 total=12.55
            #
            # Four real turns, every one correct, every one transcribed
            # on the Pi in eight to twelve seconds, with an M1 Pro
            # seventy-one milliseconds away doing nothing. The
            # speculation is the parachute; it is not supposed to win
            # the race by starting first.
            going_remote = (self._remote_ready()
                            or (self._cloud_enabled() and self._is_online()))
            # ...AND THEN THAT RULE INVERTED, because the speculation
            # is no longer the weaker thing.
            #
            # `not going_remote` was written when this pass ran
            # whisper-tiny.en on the Pi: reusing it would have thrown
            # away a much better recogniser sitting on the LAN, so the
            # turn was always re-done properly. Correct then.
            #
            # The speculation now uses the Mac itself. When it does,
            # its answer IS what a fresh Mac call would return — the
            # same audio, the same model, already finished — and
            # re-asking is a second round trip for an identical
            # string. The device showed the cost of not noticing:
            #
            #   speculation hits: 0      stt 1.17-1.49s
            #
            # against 0.80-0.96 s before the speculation started
            # calling the Mac, because every turn was now making TWO
            # requests and the second queued behind the first. I made
            # it slower by half a second and the counter said so.
            #
            # So: reuse it when the audio has not changed AND the Mac
            # is who answered it. A locally-speculated answer still
            # defers to the Mac, exactly as before.
            # Whether the speculation was POINTED at the Mac, decided
            # when it started — not who answered it, which is not
            # known yet and was my mistake the first time.
            _spec_remote = bool(spec and spec.get("remote"))
            # SAY WHICH BRANCH RAN. Two attempts at this reuse have now
            # gone wrong in ways that looked identical from the row —
            # `spec_hit 0` covers "never started", "audio changed" and
            # "refused", and those need three different fixes.
            if not spec:
                self._spec_why = "no speculation ran"
            elif spec.get("voice_ts") != self._last_voice_ts:
                self._spec_why = "more speech arrived after it started"
            elif not (_spec_remote or not going_remote):
                self._spec_why = "it was local and the Mac is up"
            else:
                self._spec_why = "reusing it"
            if spec and spec.get("voice_ts") == self._last_voice_ts \
                    and (_spec_remote or not going_remote):
                # WAIT THE TURN BUDGET, NOT A ROUND NUMBER.
                #
                # This used to wait six seconds and then, if the
                # speculation had not finished, transcribe the whole
                # buffer AGAIN — six seconds of waiting followed by the
                # full cost, for audio a worker was already most of the
                # way through. That is the worst of both paths.
                #
                # The speculation is running on the SAME audio. Once it
                # has started, nothing we can do is faster than letting
                # it finish, so the only sensible question is how long
                # the turn is allowed to take at all.
                t_wait = time.time()
                spec["done"].wait(timeout=STT_TURN_BUDGET)
                waited = time.time() - t_wait
                if spec.get("text"):
                    self._spec_hits = getattr(self, "_spec_hits", 0) + 1
                    self._t_spec_wait = waited
                    # NAME THE ENGINE ON THIS PATH TOO.
                    #
                    # It returned without touching _last_engine, so
                    # the row carried whatever the PREVIOUS turn left
                    # there. The device printed `mac (unclear)` beside
                    # two turns it had understood perfectly — a label
                    # from a turn that had finished a minute earlier.
                    #
                    # Seventh instance of a field that is not written
                    # on one path being read as though it were. The
                    # others were all two threads; this one is two
                    # code paths, which is the same mistake standing
                    # slightly differently.
                    self._t_fast = waited
                    self._t_slow = 0.0
                    self._last_engine = ("mac (early)"
                                         if spec.get("remote")
                                         else "local (early)")
                    self._stt_note = (
                        "transcribed while he was still talking; "
                        "waited %.2fs for it at the end" % waited)
                    return spec["text"]
                # It really did run out of budget. Count it: a station
                # whose speculation never lands is doing every turn
                # twice, and that is invisible without this number.
                self._spec_misses = getattr(self, "_spec_misses", 0) + 1
            return self._better_transcribe(final_buf, hint)

        while time.time() < deadline and not self._stop.is_set():
            try:
                data = self._audio_q.get(timeout=0.05)
            except queue.Empty:
                data = None
            if data:
                buf += data
                if len(buf) > SAMPLE_RATE * 2 * 30:   # 30 s hard cap
                    del buf[:len(buf) - SAMPLE_RATE * 2 * 30]
                if rec.AcceptWaveform(data):
                    # The live listener thinks the utterance ended.
                    # That is NOT a decision to answer.
                    #
                    # This used to return immediately, which meant the
                    # whole endpointing policy below was bypassed
                    # whenever the listener endpointed first — and it
                    # does that on any brief pause. "What time ... is
                    # it" was answered after "what time". It looks
                    # exactly like a bad microphone, but the audio was
                    # fine; the turn was simply cut.
                    #
                    # Its finals are now just more transcript. We keep
                    # listening, and OUR policy decides when the person
                    # has actually finished.
                    done_part = json.loads(
                        rec.Result()).get("text", "").strip()
                    if done_part:
                        final_parts.append(done_part)
                        heard = True
                        self._partial = " ".join(final_parts)
                        self._set_ui_state("listening",
                                           user_text=self._partial)
                        if time.time() - speech_started \
                                < ENDPOINT_MAX_UTTERANCE:
                            deadline = max(deadline, time.time() + 4.0)
                else:
                    partial = json.loads(
                        rec.PartialResult()).get("partial", "")
                    if partial:
                        if not heard:
                            speech_started = time.time()
                        heard = True
                        # the running transcript is everything the
                        # listener has finalised PLUS what it is
                        # hearing right now
                        self._partial = " ".join(
                            final_parts + [partial]).strip()
                        self._set_ui_state("listening",
                                           user_text=self._partial)
                        # keep the turn open while they are still
                        # talking, but not indefinitely: a television
                        # never stops, and we must not listen forever
                        if time.time() - speech_started \
                                < ENDPOINT_MAX_UTTERANCE:
                            deadline = max(deadline, time.time() + 4.0)

            lv = self._last_voice_ts
            if not lv:
                continue
            quiet = time.time() - lv

            # (a) they have paused — start recognising in the background
            # ONE IN FLIGHT AT A TIME.
            #
            # With somebody talking in the room, _last_voice_ts moves
            # constantly, so this fires a fresh speculation every time
            # it changes — and each one is a request to the Mac. They
            # do not run in parallel there: one model, and the
            # requests queue. The device measured the pile-up, with a
            # person audible in the transcripts:
            #
            #   2.62s of audio in 3.87s
            #   2.62s of audio in 6.19s
            #   2.94s of audio in 9.88s
            #
            # Three seconds of speech taking ten to transcribe, on a
            # machine that does it in half a second when asked once.
            # The queue was the whole of it.
            #
            # So a new one starts only when the last has finished. The
            # cost of skipping is that finish() may do a full pass
            # instead of reusing — one request — which is exactly what
            # it did before any of this existed.
            _busy = bool(spec and not spec["done"].is_set())
            if quiet >= SPECULATE_AFTER and spec.get("voice_ts") != lv \
                    and len(buf) > SAMPLE_RATE \
                    and not _busy:                   # >0.5 s of audio
                spec = speculate(bytes(buf),
                                 getattr(self, "_partial", ""), lv)

            # (b) are they done? The wait depends on what they said.
            need = self._endpoint_wait(getattr(self, "_partial", ""))
            if (heard or lv) and quiet >= need:
                try:
                    tail = json.loads(
                        rec.FinalResult()).get("text", "").strip()
                except Exception:
                    tail = ""
                if tail:
                    final_parts.append(tail)
                text = " ".join(final_parts).strip()
                self._turn_stopped_at = lv
                self._t_endpoint = time.time() - lv
                # PUT IT ON THE SCREEN NOW. The person has stopped
                # speaking and we are about to spend seconds deciding
                # what they said. Showing the live transcript at this
                # moment is what tells them they were heard — waiting
                # until the answer is ready means several seconds of a
                # screen that says nothing, which reads as "it missed
                # me" and makes people repeat themselves.
                if text:
                    self._set_ui_state("listening", user_text=text)
                # SECONDS FROM BYTES, NOT FROM BLOCK COUNT.
                #
                # turns.jsonl reported a 70.9-second utterance on a
                # turn whose listen timeout is twelve. The number was
                # blocks * BLOCK_SIZE / SAMPLE_RATE, but ingest()
                # counts a block when the DEVICE hands one over — 1024
                # samples at 48 kHz — and then resamples it to 16 kHz
                # before it reaches this buffer. So every figure was
                # three times too long, and I read one of them as
                # evidence that endpointing had run away.
                self._turn_secs = round(
                    len(buf) / 2.0 / float(SAMPLE_RATE), 1)
                got = finish(self._cap_audio(buf), text)
                if got:
                    return got
                # nothing recognisable — keep listening, don't re-fire
                self._last_voice_ts = 0.0
                spec = {}
                final_parts = []
                self._partial = ""
        return ""

    def _drain(self, rec):
        try:
            while True:
                self._audio_q.get_nowait()
        except queue.Empty:
            pass
        try:
            rec.Reset()
        except Exception:
            pass

    # ── audio output ──────────────────────────────────────────────────
    @staticmethod
    def _cap_onnx_threads(n=INFER_THREADS):
        """Stop ONNX Runtime taking the whole board for TTS.

        MEASURED ON THE DEVICE: with the microphone finally streaming,
        the app sat at 392-398% CPU — all four cores — at 73.5 C, load
        4.97. py-spy found the reason:

            Thread (active): "tts-prewarm"
                run (onnxruntime/.../onnxruntime_inference_collection.py)
                phoneme_ids_to_audio (piper/voice.py)
                synthesize (piper/voice.py)
                render_to_cache (dose_voice.py)

        Piper's ONNX session, plus three native ORT worker threads at
        ~100% each. The prewarm renders every fixed line at startup, so
        this lands squarely on top of launch, when the capture stream is
        also coming up. The capture then cannot be drained in time and
        the PCM reports XRUN — which is dropped audio, which is
        misrecognition. A TTS cache warm-up must never be able to starve
        the microphone.

        INFER_THREADS is already 2, and OMP_NUM_THREADS and friends are
        already exported to match (see the block near the top of this
        file). It made no difference, because a stock onnxruntime wheel
        is NOT built with OpenMP: it uses its own thread pool, sized to
        the core count, and the ONLY lever is
        SessionOptions.intra_op_num_threads. piper always passes a
        default SessionOptions, whose intra_op_num_threads is 0 ("pick
        for me"), so the env vars were never going to be read.

        So we wrap InferenceSession and fill in that 0 before the
        session is built. An explicit non-zero value set by any caller
        is left alone — this only supplies a number where ORT would
        otherwise have helped itself to the machine."""
        try:
            import onnxruntime as ort
        except Exception:
            return
        if getattr(ort, "_dose_thread_cap", 0):
            return
        orig = ort.InferenceSession

        def _capped(*args, **kwargs):
            try:
                so = kwargs.get("sess_options")
                if so is None and len(args) > 1 and hasattr(
                        args[1], "intra_op_num_threads"):
                    so = args[1]
                if so is None:
                    so = ort.SessionOptions()
                    kwargs["sess_options"] = so
                # 0 means "ORT decides", which on a 4-core Pi means all
                # of them. Only fill in a value nobody chose.
                if getattr(so, "intra_op_num_threads", 0) == 0:
                    so.intra_op_num_threads = n
                if getattr(so, "inter_op_num_threads", 0) == 0:
                    so.inter_op_num_threads = 1
            except Exception:
                pass          # never block synthesis over a tuning knob
            return orig(*args, **kwargs)

        ort.InferenceSession = _capped
        ort._dose_thread_cap = n

    def _load_piper(self):
        if self._piper_voice is None:
            # Cap BEFORE piper builds its session — thread counts cannot
            # be changed after an InferenceSession exists.
            self._cap_onnx_threads()
            from piper import PiperVoice
            self._piper_voice = PiperVoice.load(self._piper_path)
        return self._piper_voice

    # ── There is no second synthesis engine. Kokoro and the
    #    moonshine-voice TTS are gone: both were slower than real time
    #    on a Pi 4, and having two engines meant the station could
    #    answer in two different voices depending on which one loaded.
    #    One voice, one engine, one file.
    # ── Piper in a separate process ───────────────────────────────────
    # ONNX Runtime aborts this application. SIGABRT from native code,
    # which NOTHING in Python can catch — see tools/piper_worker.py for
    # the stack and the reasoning. The abort is survivable only if the
    # thing that aborts is not the app, so synthesis is handed to a
    # persistent child that loads the model once and then waits.
    #
    # Every failure mode here falls back to in-process synthesis, which
    # is exactly today's behaviour. That is deliberate: this ships to a
    # station that has a pitch to get through, and the worst case must
    # be "no better than before", never "worse than before".
    #
    # DOSE_PIPER_WORKER=0 disables it outright, so if it misbehaves on
    # the device it can be switched off without a code change.
    def _worker_enabled(self):
        # DEFAULT OFF, DELIBERATELY, UNTIL MEASURED ON HARDWARE.
        #
        # This shipped default-on and the station went unreachable twice
        # within the hour, needing a physical restart both times. The
        # cause was almost certainly a second resident ONNX session (see
        # the note in _synth) and that is now fixed — but "almost
        # certainly" is not a standard worth holding when the failure
        # mode is a device that has to be unplugged, and a pitch is
        # coming.
        #
        # So the station goes back to exactly the behaviour it had this
        # morning: in-process synthesis, with the ONNX abort handled by
        # the supervisor's ~20 s restart. A known 20-second recovery
        # beats an unknown hard lockup.
        #
        # DOSE_PIPER_WORKER=1 turns it on for a measured soak. When a
        # soak shows total RSS at or below the in-process baseline, this
        # default flips back and the abort stops costing 20 seconds.
        return os.environ.get("DOSE_PIPER_WORKER", "0") in (
            "1", "true", "yes", "on")

    def _piper_worker(self):
        """The live worker, started if needed. None if unavailable."""
        p = getattr(self, "_pw_proc", None)
        if p is not None and p.poll() is None:
            return p
        if p is not None:
            # It died. Almost certainly the abort this exists to
            # contain, so say so — a silent respawn would hide the very
            # event we built this to observe.
            self._pw_deaths = getattr(self, "_pw_deaths", 0) + 1
            try:
                rc = p.poll()
            except Exception:
                rc = None
            last = ""
            try:
                buf = getattr(self, "_pw_errs", None) or []
                if buf:
                    last = "  | said: " + buf[-1].split("  ", 1)[-1][:90]
            except Exception:
                pass
            self._note_tts("piper worker died (rc=%s, death #%d) — "
                           "respawning%s" % (rc, self._pw_deaths, last))
            try:
                if p.stdin:
                    p.stdin.close()
            except Exception:
                pass
        if not self._worker_enabled() or not self._piper_path:
            return None
        # Do not respawn in a tight loop: a model that aborts on load
        # would otherwise fork forever.
        now = time.time()
        last = getattr(self, "_pw_spawned_at", 0.0)
        if last and now - last < PIPER_WORKER_MIN_GAP:
            return None
        self._pw_spawned_at = now
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "tools", "piper_worker.py")
        if not os.path.exists(script):
            return None
        try:
            p = subprocess.Popen(
                [sys.executable, script, self._piper_path,
                 str(INFER_THREADS)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                # KEEP ITS STDERR. It was DEVNULL, and the worker is
                # the one experiment that would settle where the last
                # two seconds of every reply go — so the one thing it
                # must be able to do is say why it failed.
                #
                # Enabled on the device, it spawned at 138 MB, never
                # answered a single request, and every render reported
                # by=in-process. TWO children were alive at once, which
                # means one died and another replaced it. The fallback
                # worked exactly as designed, so nothing broke and
                # nothing was learned.
                #
                # This is the same blind spot as `-q` on arecord, which
                # cost a day: the recorder was dying thirty times a
                # minute and the log said "unknown" because the flag
                # that would have explained it was suppressing the
                # explanation. Bounded to the last 40 lines in memory,
                # drained on a thread so a full pipe can never stall
                # the child.
                stderr=subprocess.PIPE, text=True,
                # Its own session: a signal aimed at our process group
                # must not take the synthesiser with it.
                start_new_session=True)
        except Exception as e:
            self._note_tts("piper worker would not start: %r" % (e,))
            return None

        def _drain_worker_err(proc=p):
            try:
                for raw in iter(proc.stderr.readline, ""):
                    line = (raw or "").strip()
                    if not line:
                        continue
                    buf = getattr(self, "_pw_errs", None)
                    if buf is None:
                        buf = self._pw_errs = []
                    buf.append("%s  %s" % (time.strftime("%H:%M:%S"),
                                           line[:160]))
                    del buf[:-40]
            except Exception:
                pass

        threading.Thread(target=_drain_worker_err, daemon=True,
                         name="piper-worker-stderr").start()
        # Wait for READY, so the first real sentence does not pay the
        # model load inside its own timeout budget.
        try:
            p.stdout.readline()
        except Exception:
            pass
        if p.poll() is not None:
            last = ""
            try:
                buf = getattr(self, "_pw_errs", None) or []
                if buf:
                    last = ": " + buf[-1].split("  ", 1)[-1][:110]
            except Exception:
                pass
            self._note_tts("piper worker exited during load%s" % last)
            return None
        self._pw_proc = p
        self._note_tts("piper worker ready")
        return p

    def release_audio(self):
        """Give the microphone back, on purpose, before the process ends.

        WHY THIS EXISTS. dose_app.py restarts itself with os.execv()
        after an update. execv REPLACES THE PROCESS IMAGE: no finally
        runs, no atexit fires, every thread simply ceases. The capture
        recorder is a child process in its own session, so it survives
        all of that — still holding the USB device — and the process
        that takes our place finds the microphone unavailable.

        Measured on the device. Two selections, both "#1 since start",
        54 seconds apart, while systemd reported ZERO service restarts
        (because the app restarted ITSELF, not the unit):

            22:11:51  took 2.0s   chose card 5,0   peak 9542, 88 blocks
            22:12:45  took 63.8s  chose NOTHING    every route, 0 blocks

        "audio blocks delivered since start: 0" is the whole story. Not
        a measurement problem, not a silent microphone — no audio
        reached the new process at all, because the old process's
        recorder still had the device.

        So the microphone is handed back deliberately: stop the loop,
        TERM the recorder so it can release the ALSA PCM cleanly, reap
        it, and take the synthesis worker too. Never raises — a failure
        to tidy up must not stop the restart it precedes.
        """
        try:
            self._stop.set()
        except Exception:
            pass
        p = getattr(self, "_cap_proc", None)
        self._cap_proc = None
        if p is not None:
            try:
                if p.poll() is None:
                    # TERM, never KILL first: a killed recorder does not
                    # run its cleanup and strands the PCM, which is the
                    # fault this project spent a whole session on.
                    p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.wait(timeout=2)
                except Exception:
                    pass
            try:
                if p.stdout:
                    p.stdout.close()
            except Exception:
                pass
        try:
            self.stop_piper_worker()
        except Exception:
            pass

    def stop_piper_worker(self):
        """End the synthesis worker. Safe to call repeatedly.

        TERM first so it can close its own ONNX session, then KILL, and
        always reap — the same discipline close_capture() had to learn
        the hard way when unreaped recorders left the microphone
        unopenable.
        """
        p = getattr(self, "_pw_proc", None)
        self._pw_proc = None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                pass

    def _note_tts(self, msg):
        """Record a synthesis event. Bounded, never raises."""
        try:
            lst = getattr(self, "_tts_events", None)
            if lst is None:
                lst = self._tts_events = []
            lst.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
            del lst[:-20]
        except Exception:
            pass

    def _synth_via_worker(self, text, wav):
        """True if the worker produced the wav. False to fall back.

        Serialised on _pw_lock: see that lock's comment for the two bugs
        this prevents, both of which a hardware soak found immediately.
        """
        with self._pw_lock:
            return self._synth_via_worker_locked(text, wav)

    def _synth_via_worker_locked(self, text, wav):
        p = self._piper_worker()
        if p is None:
            return False
        req = json.dumps({"text": text, "wav": os.path.abspath(wav),
                          "length_scale": 1.0, "noise_scale": 0.62,
                          "noise_w": 0.75})
        try:
            p.stdin.write(req + "\n")
            p.stdin.flush()
        except Exception:
            # Pipe gone — the child died mid-write, which is the abort.
            self._pw_proc = p
            return False
        # A bounded read. A hung synthesiser must cost one timeout and
        # a fallback, not a station that never speaks again.
        done = threading.Event()
        box = {}

        def read():
            try:
                box["line"] = p.stdout.readline()
            except Exception as e:
                box["err"] = e
            done.set()

        threading.Thread(target=read, name="piper-wait",
                         daemon=True).start()
        if not done.wait(PIPER_WORKER_TIMEOUT):
            self._note_tts("piper worker did not answer in %.0fs — "
                           "killing it and falling back"
                           % PIPER_WORKER_TIMEOUT)
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                pass
            self._pw_proc = p
            return False
        line = (box.get("line") or "").strip()
        if not line:
            self._note_tts("piper worker closed its pipe (native abort "
                           "contained — the app survived)")
            return False
        try:
            resp = json.loads(line)
        except Exception:
            return False
        if resp.get("ok") and os.path.exists(wav) \
                and os.path.getsize(wav) > 44:
            return True
        err = resp.get("error")
        if err:
            self._note_tts("piper worker error: %s" % str(err)[:120])
        return False

    def _synth(self, voice, text, wav):
        """Synthesize to a PATH. `wav` is a filename, not an open wave.

        IT USED TO BE AN OPEN WAVE, AND THAT IS WHY THE WORKER WAS
        NEVER USED ONCE IN ITS LIFE.

        Every caller opened a wave.Wave_write and passed the object,
        because the in-process branch below hands it straight to piper,
        which wants exactly that. But the worker branch has to send a
        FILENAME down a pipe to another process, so it called
        os.path.abspath() on it and got:

            TypeError('expected str, bytes or os.PathLike object,
                       not Wave_write')

        caught by the `except Exception` two lines later and turned
        into a silent fall-back to in-process synthesis. Every reply,
        for the life of the feature.

        The two halves were each fixed to match the wrong neighbour:
        the child was opening nothing and being handed a path; the
        parent was opening a wave and handing the worker an object.
        Now the path is the interface — the worker sends it on, and the
        in-process branch opens it here, once, where the difference
        belongs.

        Synthesize with an EXTREMELY COMFORTING delivery — a soft
        female guardian: calm and unhurried, smooth and even, gentle
        and reassuring, with a little extra space between phrases so it
        feels soothing rather than rushed. This is an ORIGINAL voice
        character, not a copy of any specific game/film character or
        its voice actor. Falls back to the plain call on any Piper API
        difference."""
        # OUT OF PROCESS FIRST. If it works, a future ONNX abort costs a
        # respawn instead of the application. If anything about it does
        # not work, we are straight back to the in-process path below,
        # which is what this station does today.
        # TIMED, ONE LEVEL DOWN. render_to_cache said the whole cost is
        # in here (its cache lookup and write are microseconds), and
        # this method does three separable things: try an out-of-process
        # worker, resolve the voice, and synthesize. Five explanations
        # for this number have now been wrong, so it gets measured
        # rather than reasoned about.
        _s0 = time.time()
        _p = {}
        try:
            if self._synth_via_worker(text, wav):
                _p["worker"] = round(time.time() - _s0, 3)
                _p["by"] = "worker"
                if _recording():
                    self._t_synth = _p
                return
        except Exception as e:
            # AND PUT IT WHERE IT WILL BE READ.
            #
            # _note_tts writes somewhere that did not reach live.txt in
            # the run that needed it — the worker section came back
            # empty while every render still reported by=in-process
            # with the attempt taking 0.0 s. A path that fails
            # instantly and reports nowhere is the same shape as `-q`
            # on arecord and DEVNULL on this worker's stderr, twice
            # already fixed in this project.
            _p["raised"] = repr(e)[:90]
            self._note_tts("worker path raised, using in-process: %r"
                           % (e,))
        _p["worker"] = round(time.time() - _s0, 3)
        # Why the worker was not used, when it was not used. "0.0 s and
        # in-process" says the attempt returned immediately; it does
        # not say whether the child was missing, throttled, disabled,
        # or the call blew up before it started.
        try:
            _p["wstate"] = "%s/%s/%s" % (
                "on" if self._worker_enabled() else "off",
                "proc" if getattr(self, "_pw_proc", None) is not None
                else "none",
                getattr(self, "_pw_deaths", 0))
        except Exception:
            pass
        # THE PARENT'S MODEL IS LOADED HERE AND NOWHERE ELSE.
        #
        # Every caller used to do `voice = self._load_piper()` and hand
        # the result in, which meant the parent built a full Piper ONNX
        # session whether or not it was going to use it. With synthesis
        # moved into a worker, that made TWO complete sessions resident:
        # the child's, doing the work, and the parent's, doing nothing.
        #
        # On a 4 GB Pi already holding faster-whisper, Vosk, Silero VAD,
        # picamera2 and Tkinter — the app measured 33.5% of memory
        # BEFORE any of this — a second ONNX session is the difference
        # between tight and over. Ryan's station went unreachable twice
        # this evening and needed a physical restart, which is what
        # memory pressure looks like from outside and is NOT what a
        # Wi-Fi drop looks like.
        #
        # So the voice is resolved lazily, at the one point it is
        # actually needed: after the worker has declined. A station
        # whose worker is healthy never builds this session at all.
        _l0 = time.time()
        if voice is None:
            voice = self._load_piper()
        _p["load"] = round(time.time() - _l0, 3)
        # Newer piper-tts: SynthesisConfig(length_scale, noise_scale,...)
        _y0 = time.time()
        _w = wave.open(wav, "wb")
        try:
            from piper import SynthesisConfig
            cfg = SynthesisConfig(length_scale=1.0,    # natural, quick
                                  noise_scale=0.62,     # smooth, warm
                                  noise_w_scale=0.75)
            voice.synthesize_wav(text, _w, syn_config=cfg)
            _w.close()
            _p["synth"] = round(time.time() - _y0, 3)
            _p["chars"] = len(text or "")
            _p["by"] = "in-process"
            if _recording():
                self._t_synth = _p
            return
        except Exception:
            pass
        # Older piper-tts: keyword args. Same open wave — reopening it
        # here would truncate whatever the attempt above wrote.
        try:
            voice.synthesize_wav(text, _w, length_scale=1.0,
                                 noise_scale=0.62, noise_w=0.75)
            _w.close()
            return
        except Exception:
            pass
        try:
            voice.synthesize_wav(text, _w)   # plain fallback
        finally:
            try:
                _w.close()
            except Exception:
                pass

    @staticmethod
    def _audio_env():
        """Environment that reaches the user's PipeWire session —
        the same one desktop apps (YouTube) use. XDG_RUNTIME_DIR is
        the key: without it pw-play/parec/pactl silently fail."""
        env = dict(os.environ)
        if not env.get("XDG_RUNTIME_DIR"):
            env["XDG_RUNTIME_DIR"] = "/run/user/%d" % os.getuid()
        return env

    def _kick_audio_services(self, force=False):
        """Make sure the user audio services are actually running —
        a dead pipewire-pulse means silence in BOTH directions.

        `systemctl start` on an already-running unit is a no-op that
        still costs a process spawn and can block for up to its
        timeout, and this ran on every single capture open. The
        services do not stop spontaneously; when one does die, capture
        fails and the failure path passes force=True.
        """
        if not force:
            last = getattr(self, "_kicked_at", 0.0)
            if last and time.time() - last < SERVICE_KICK_AFTER:
                return
        self._kicked_at = time.time()
        try:
            subprocess.run(["systemctl", "--user", "start",
                            "pipewire", "pipewire-pulse",
                            "wireplumber"],
                           capture_output=True, timeout=15,
                           env=self._audio_env())
        except Exception:
            pass

    def _play_wav(self, path):
        """Play a wav on whatever the system's ACTIVE output is.
        Order matters: pw-play/paplay follow PipeWire's current sink
        (AirPods, USB — the same route YouTube uses). aplay goes to
        the legacy ALSA default, which on a Pi is often the silent
        HDMI port while still reporting success — so it is only a
        fallback. PortAudio last. A concrete speaker (USB first) is
        targeted explicitly when one exists, so plugging in a USB
        speaker works instantly regardless of the mic."""
        target = self._pick_output_target()
        routes = []
        if target:
            routes += [(["pw-play", "--target", target, path],
                        "PipeWire → " + target),
                       (["paplay", "-d", target, path],
                        "Pulse → " + target)]
        routes += [(["pw-play", path], "PipeWire (pw-play)"),
                   (["paplay", path], "Pulse (paplay)")]
        # Remember which player actually worked. Probing a dead player
        # costs a process spawn and a failure every single sentence —
        # on a Pi that is a visible stutter between sentences, so the
        # known-good route is tried first and the rest stay as fallback.
        won = getattr(self, "_play_route", None)
        if won:
            routes.sort(key=lambda r: r[0][0] != won)
        for cmd, label in routes:
            try:
                # Popen, not run(): playback has to be INTERRUPTIBLE so
                # barge-in can cut her off the moment you start talking.
                # subprocess.run() gives no handle to stop.
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=self._audio_env())
                self._play_proc = proc
                try:
                    rc = proc.wait(timeout=120)
                except Exception:
                    # SAME BUG AS THE CAPTURE TEARDOWN, OTHER END OF THE
                    # PIPELINE. kill() without a following wait() leaves
                    # the player as a zombie forever — the device grew a
                    # "[aplay] <defunct>" within seconds of a restart.
                    # TERM first so the player can release the audio
                    # device cleanly, KILL only if it will not go, and
                    # REAP either way.
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=3)
                        except Exception:
                            pass
                    rc = -1
                finally:
                    self._play_proc = None
                if self._barge:
                    return label          # stopped on purpose
                if rc == 0:
                    self._play_route = cmd[0]
                    return label
            except Exception:
                continue
        try:
            r = subprocess.run(["aplay", "-q", path],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=120,
                               env=self._audio_env())
            if r.returncode == 0:
                return "legacy ALSA (aplay) — may be routed to HDMI"
        except Exception:
            pass
        try:
            with wave.open(path) as w:
                rate = w.getframerate()
                ch = w.getnchannels()
                data = w.readframes(w.getnframes())
            with self._sd.RawOutputStream(samplerate=rate, channels=ch,
                                          dtype="int16") as out:
                out.write(data)
            return "PortAudio default output"
        except Exception:
            return False

    def _chime(self):
        def go():
            self._play_wav("/usr/share/sounds/alsa/Front_Center.wav")
        threading.Thread(target=go, daemon=True).start()

    def _cache_path(self, text):
        """Cache key includes the voice file and speed, so changing
        either regenerates the audio instead of playing a stale clip."""
        import hashlib
        key = "%s|%s" % (os.path.basename(self._piper_path or ""), text)
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        d = os.path.join(VOICE_DIR, "cache")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "say_%s.wav" % h)

    def _purge_foreign_cache(self):
        """Delete every pre-rendered clip that was not made in HER
        voice. Belt and braces on top of the voice-keyed cache: this
        runs at startup, compares a stamp against the voice actually
        loaded, and wipes the whole cache if they differ. It means no
        ordering mistake in an update — and no half-finished migration
        — can leave a clip of an older voice on the device.

        AN UNRESOLVED VOICE IS NOT A DIFFERENT VOICE.

        `self._piper_path` is filled in by the preflight, and this runs
        at startup, so it can be None here — in which case `now` was
        the empty string, every stamp differed from it, and the whole
        cache went. The device counted it twice in one evening:

            221 ended with  61 clips   ->  222 started and found 35
            223 started with 116 clips ->  60 s later there were 22

        Every restart threw the prewarm away and paid for it again, in
        minutes of synthesis at 68 °C, for a directory whose contents
        were perfectly good. Purging is for a voice that CHANGED; not
        knowing which voice we have is a reason to leave the cache
        alone and let the voice-keyed lookup do its job — a clip in
        the wrong voice can never be selected anyway, which is what
        the key is for."""
        try:
            cache = os.path.join(VOICE_DIR, "cache")
            os.makedirs(cache, exist_ok=True)
            stamp = os.path.join(cache, ".voice")
            if not self._piper_path:
                # Cheap: this is a filename, not a model load. The
                # preflight sets it too, but it has not necessarily
                # run yet, and the cache key uses the same value — so
                # resolving it here keeps the purge honest AND keeps
                # every key written before the preflight identical to
                # the keys looked up after it.
                try:
                    found = sorted(glob.glob(
                        os.path.join(VOICE_DIR, "%s*.onnx" % VOICE_NAME)))
                    if found:
                        self._piper_path = found[0]
                except Exception:
                    pass
            now = os.path.basename(self._piper_path or "")
            if not now:
                # Still unknown. Leave the cache alone: see above.
                self._cache_purge_skipped = True
                return
            was = ""
            try:
                with open(stamp) as f:
                    was = f.read().strip()
            except Exception:
                pass
            if was != now:
                n = 0
                for f in glob.glob(os.path.join(cache, "*.wav")):
                    try:
                        os.unlink(f)
                        n += 1
                    except Exception:
                        pass
                with open(stamp, "w") as f:
                    f.write(now)
                self._cache_purged = was or "(unstamped)"
                # A BREADCRUMB ON DISK, NOT AN ATTRIBUTE.
                #
                # The heartbeat reported "kept" while 126 clips
                # disappeared across a restart. An attribute on `self`
                # only speaks for THIS object in THIS process — and
                # the app builds more than one DoseVoice, so a purge
                # on a probe instance is invisible to the instance
                # writing the heartbeat. Three deleters, two wrong
                # guesses, and the reason it stayed unfindable is that
                # every witness I asked was the wrong one.
                #
                # A file outlives the object, the process and the
                # restart. dose_app's _retire_other_voices and
                # DOSE.sh's retirement branch write to the same one,
                # so whichever fires is named by its own hand.
                _cache_purge_note("dose_voice._purge_foreign_cache: "
                                  "%d clips, stamp was %r, now %r"
                                  % (n, was, now))
        except Exception:
            pass

    def render_to_cache(self, text):
        """Render one sentence into the cache if it isn't there yet.
        Returns its path (or None). Safe to call from a worker.

        TIMED, BECAUSE THE TURN LOG AND THE BENCH DISAGREE BY TWO
        SECONDS AND I HAVE GUESSED WRONG FOUR TIMES.

        The turn records 2.78 s for the first chunk. Measured on the
        device, in the app's own interpreter, with the app running:

            to_speech()                          0.000 s
            synthesize_wav, no config            0.699 s
            synthesize_wav, the app's config     0.719 s
            ONNX thread cap (2 vs 4)             0.18 s of the total
            CPU contention, app running vs not   15%

        Every layer I could measure from outside is small, so the
        remaining ~2 s is inside this method or inside _synth, and the
        only honest way to find it is to make the program report it.
        Guessing produced: a background thread that was not the cause,
        a chunker that was already splitting correctly, contention that
        was 15%, and a thread cap worth 0.18 s.
        """
        t0 = time.time()
        parts = {}
        try:
            path = self._cache_path(text)
            parts["cache"] = time.time() - t0
            if os.path.exists(path):
                parts["hit"] = 1
                if _recording():
                    self._t_tts = parts
                return path
            parts["hit"] = 0
            t1 = time.time()
            tmp = path + ".tmp"
            self._synth(None, to_speech(text), tmp)
            parts["synth"] = time.time() - t1
            t2 = time.time()
            os.replace(tmp, path)
            parts["write"] = time.time() - t2
            parts["total"] = time.time() - t0
            # THE BACKGROUND RENDER OVERWRITES THIS TOO.
            #
            # One turn reported speak=0.67 next to synth=3.02, which
            # cannot both describe the same render. render_to_cache is
            # called once on the critical path and again, on a worker
            # thread, for every later chunk — and the worker wrote its
            # own numbers over the first chunk's. Exactly the bug
            # _last_engine had, in the diagnostic added to chase it.
            if _recording():
                self._t_tts = parts
            return path
        except Exception as e:
            parts["failed"] = str(e)[:60]
            parts["total"] = time.time() - t0
            if _recording():
                self._t_tts = parts
            return None

    @staticmethod
    def _sentences(text):
        """Split a reply into speakable chunks. Short replies stay
        whole; longer ones are split on sentence ends so the first one
        can start playing while the rest is still rendering."""
        text = (text or "").strip()
        if len(text) <= 60:
            return [text] if text else []
        parts, buf = [], ""
        for tok in re.split(r"(?<=[.!?…])\s+", text):
            tok = tok.strip()
            if not tok:
                continue
            # keep very short fragments attached to the previous chunk
            # so we never chop a sentence into stutters
            if buf and len(buf) < 25:
                buf = buf + " " + tok
            else:
                if buf:
                    parts.append(buf)
                buf = tok
        if buf:
            parts.append(buf)
        return parts or [text]

    @staticmethod
    def _split_first(chunks):
        """Make the FIRST chunk short, so the station starts talking
        sooner. Everything after it is left exactly as it was.

        TIME-TO-FIRST-SOUND is the whole of perceived response time,
        and until now it was the render time of a whole sentence.
        _sentences() only ever splits on sentence ends, so a reply like

            "You have two doses left today, Ryan, and the next one
             is at six."

        is ONE chunk: nothing is audible until the entire thing has
        been synthesized. Piper runs about three times real time on
        this board, so a four-second sentence is well over a second of
        silence before the first syllable — and the person is watching
        a screen that says nothing is happening.

        Splitting it at the comma costs nothing. Piper puts a small
        pause at a comma anyway, so the seam is inaudible, and by the
        time the first fragment has finished playing the remainder has
        long since rendered on the worker.

        The rules are all about NOT making it worse:
        - only boundaries a speaker would pause at, ranked by how hard
          the pause is: a sentence end beats a comma, a comma beats a
          conjunction. Never a space in the middle of a phrase.
        - the STRONGEST boundary in the window, not the latest one.
          Taking the latest split "I didn't catch that, Ryan. Tap the
          logo and try again." across the word "and" — straight over a
          full stop that was sitting right there.
        - both halves must be long enough to be worth saying, or the
          reply opens with a one-word stutter, which sounds broken in
          a way that half a second of silence does not.
        - if the sentence has no boundary inside the window, overshoot
          to the first one that IS available rather than give up: the
          case that needs this most is one long clause, and a 50-
          character opening beats a 90-character one.
        - if nothing qualifies at all, hand back the original
          untouched.

        A short leading fragment is also far more likely to be a cache
        hit next time ("Sure, Ryan," "You have two doses left today,")
        which makes the second occurrence instant rather than fast.
        """
        if not chunks:
            return chunks
        head = chunks[0]
        def _declared(rest_of):
            """Fall back to a hand-declared opening (see
            INVARIANT_OPENINGS) when the ordinary rules find nothing.

            LAST RESORT, NOT FIRST CHOICE. Applying this ahead of the
            boundary search turned

                "You have two doses left today, Ryan, and the next
                 one is at six."

            from a clean break at the comma into "You have" — a
            two-word opening where a perfectly good one was already
            available. A real pause a speaker would make beats a
            prefix I happen to have cached.

            It earns its place on the replies that have no boundary at
            all. "The time is 4:48 PM." is twenty characters, so the
            window check below returns it untouched, and it cost
            3.20 s to render on the device EVERY time, because the
            minute is different every time and no cache can hold it.
            """
            for opening in INVARIANT_OPENINGS:
                if not head.startswith(opening):
                    continue
                rest = head[len(opening):].strip()
                if len(rest) < TTS_OPENING_MIN_REST:
                    return None      # nothing but punctuation left
                return [opening, rest] + list(rest_of)
            return None

        if len(head) <= TTS_FIRST_CHUNK_MAX:
            return _declared(chunks[1:]) or chunks
        cands = []
        for m in re.finditer(
                r"(?:[.!?…]\s)|(?:[,;:]\s)|(?:\s[-–—]\s)"
                r"|(?:\s(?:and|but|so|then|because|which|while)\s)", head):
            tok = m.group(0)
            if tok[0] in ".!?…":
                rank, cut = 0, m.end()
            elif tok[0] in ",;:":
                rank, cut = 1, m.end()
            elif tok.strip() in ("-", "–", "—"):
                rank, cut = 1, m.start()
            else:
                rank, cut = 2, m.start()
            if cut < TTS_FIRST_CHUNK_MIN:
                continue
            if len(head) - cut < TTS_FIRST_CHUNK_MIN:
                continue
            cands.append((rank, cut))
        if not cands:
            return _declared(chunks[1:]) or chunks
        inside = [c for c in cands if c[1] <= TTS_FIRST_CHUNK_MAX]
        if inside:
            # strongest boundary; among equals, as much as fits
            best = min(inside, key=lambda rc: (rc[0], -rc[1]))[1]
        else:
            # nothing inside the window — overshoot to the earliest
            # usable boundary, but not indefinitely
            over = [c for c in cands
                    if c[1] <= TTS_FIRST_CHUNK_MAX * 2]
            if not over:
                return chunks
            best = min(over, key=lambda rc: (rc[0], rc[1]))[1]
        return [head[:best].strip(), head[best:].strip()] + list(chunks[1:])

    def _speak(self, text, user_text=""):
        """Say one line, conversationally.

        The whole point here is TIME-TO-FIRST-SOUND. A cached line
        plays instantly. Anything new is split into sentences: we
        render only the FIRST one, start playing it, and render the
        rest on a worker while that audio is in the air. Because the
        voice is ~3x faster than real time, every later sentence is
        ready long before the previous one finishes, so it comes out
        as one continuous reply with no gap in the middle."""
        self._last_reply = text
        self._barge = False
        self._barge_frames = 0
        self._set_ui_state("speaking", user_text=user_text,
                           reply_text=text)
        try:
            # 1) whole line already cached (every fixed reply is) —
            #    nothing to synthesize, just play it
            whole = self._cache_path(text)
            if os.path.exists(whole):
                self._t_first_sound = 0.0     # already rendered
                self._t_cached = True
                # SAY SO IN THE ROW. This path never calls
                # render_to_cache(), so _t_tts kept whatever the last
                # render left there — and the device printed three
                # turns at speak 0.00 beside hit=0, which is not a
                # thing that can happen. The best outcome the cache
                # has was being reported as its worst.
                if _recording():
                    self._t_tts = {"hit": 1, "whole": 1}
                self._play_wav(whole)
                return

            chunks = self._sentences(text)
            if not chunks:
                return
            # Start talking sooner: only the first chunk is rendered
            # before any sound comes out, so only the first chunk's
            # length is on the critical path. See _split_first().
            chunks = self._split_first(chunks)

            # 2) render the rest in the background, starting NOW, so it
            #    overlaps with playback of the first chunk
            ready = {}
            done = [threading.Event() for _ in chunks]

            def render_rest():
                # NOT THE PASS THE TURN REPORTS. render_to_cache records
                # its own timings, and this thread calls it once per
                # remaining chunk — so without this the worker's numbers
                # land on the first chunk's row. One turn reported
                # speak=0.67 beside synth=3.02, which cannot both
                # describe the same render. Same shape as _last_engine:
                # two threads, one attribute, last writer wins.
                _TL.speculative = True
                try:
                    os.nice(5)      # never at the camera's expense
                except Exception:
                    pass
                # never let a synthesizer fault escape onto stderr —
                # the fallback below handles it, and a traceback in the
                # log looks like a crash when nothing actually broke
                try:
                    for i, c in enumerate(chunks[1:], 1):
                        if self._stop.is_set():
                            break
                        try:
                            ready[i] = self.render_to_cache(c)
                        except Exception:
                            ready[i] = None
                        done[i].set()
                finally:
                    for e in done:
                        e.set()

            # 3) first chunk: render and speak immediately
            #
            # THE BACKGROUND RENDER USED TO START BEFORE THIS ONE.
            #
            # The intent was right — overlap the rest with playback of
            # the first chunk — but starting the thread here overlapped
            # it with the RENDER of the first chunk instead, which is
            # the one piece of work on the critical path. On four cores
            # running ONNX that is direct competition for the thing the
            # person is waiting for.
            #
            # The device measured it plainly. `speak` is the first
            # chunk's render time and nothing else, so it should be
            # roughly CONSTANT no matter how long the reply is. It was
            # not:
            #
            #   reply                                  speak
            #   "The time is 1:18 PM."                  2.00
            #   "I could not find aspirin, Ryan."       2.31
            #   "No medications are in view today..."   3.14
            #   "Current inventory: New Medication..."  4.55
            #
            # It scales with the length of the WHOLE reply, which a
            # first-chunk render cannot do on its own. The extra is the
            # background thread rendering chunks two onward, and a
            # longer reply has more of them.
            #
            # So: render the first chunk, note the time, THEN start the
            # rest, then play. The overlap that was wanted is still
            # there — it now overlaps playback, which is what the
            # comment above always said it did.
            _t_speak0 = time.time()
            self._t_cached = False
            try:
                first = self.render_to_cache(chunks[0])
            except Exception:
                first = None
            self._t_first_sound = time.time() - _t_speak0
            if len(chunks) > 1:
                threading.Thread(target=render_rest, daemon=True,
                                 name="tts-stream").start()
            if first:
                self._play_wav(first)
            else:
                self._speak_uncached(chunks[0])

            # 4) the remainder, each as soon as it exists — unless
            #    they have started talking, in which case the rest of
            #    the sentence is no longer wanted
            for i in range(1, len(chunks)):
                if self._stop.is_set() or self._barge:
                    break
                done[i].wait(timeout=20)
                path = ready.get(i)
                if path:
                    self._play_wav(path)
                else:
                    self._speak_uncached(chunks[i])
        except Exception:
            pass

    def _speak_uncached(self, text):
        """Last resort: synthesize straight to a temp file and play."""
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".wav",
                                       dir=TMP_AUDIO_DIR)
            os.close(fd)
            self._synth(None, to_speech(text), tmp)
            self._play_wav(tmp)
        except Exception:
            pass
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    # ── the exchange ──────────────────────────────────────────────────
    # Ways of saying "we're done here". Saying any of these ends the
    # conversation immediately — as does tapping anywhere outside the
    # panel, which the UI turns into a close request.
    DONE_PHRASES = (
        "im done", "i'm done", "im done talking", "that's all",
        "thats all", "that is all", "nothing else", "no thanks",
        "no thank you", "never mind", "nevermind", "stop listening",
        "stop talking", "goodbye", "good bye", "bye", "thanks thats all",
        "we're done", "were done", "all done", "that's it", "thats it",
        "quiet", "be quiet", "cancel", "exit", "close",
    )

    def _is_done_talking(self, text):
        """Did they just say the conversation is over?

        Matched WHOLE, not as a prefix. "That's all" ends it; "that's
        all I take in the morning" is a sentence about medication, and
        "bye the way, what's next" is a mis-transcription of "by the
        way" — neither should hang up on someone. Only trailing
        politeness is ignored."""
        t = " ".join((text or "").lower().replace("'", "").split())
        if not t:
            return False
        words = t.split()
        while words and words[-1] in ("please", "thanks", "thank", "you",
                                      "now", "ok", "okay", "then"):
            words.pop()
        t = " ".join(words)
        return t in {d.replace("'", "") for d in self.DONE_PHRASES}

    # ── THE TURN LOG ─────────────────────────────────────────────────
    # Every single thing said to this station, with what it heard, what
    # it made of it, what it said back, and how long each stage took.
    # Bounded, on disk, and included in the audit report — so a run of
    # bad turns can be read rather than described.
    TURN_LOG_MAX = 60

    def _turn_log_path(self):
        return os.path.join(VOICE_DIR, "turns.jsonl")

    def _log_turn(self, rec):
        """Append one turn. Never allowed to break a conversation."""
        try:
            path = self._turn_log_path()
            os.makedirs(VOICE_DIR, exist_ok=True)
            line = json.dumps(rec, default=str)[:2000]
            lines = []
            if os.path.exists(path):
                with open(path) as f:
                    lines = f.read().splitlines()[-(self.TURN_LOG_MAX - 1):]
            lines.append(line)
            with open(path, "w") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def turn_log(self, limit=25):
        """The most recent turns, newest last."""
        try:
            with open(self._turn_log_path()) as f:
                rows = f.read().splitlines()[-limit:]
            out = []
            for r in rows:
                try:
                    out.append(json.loads(r))
                except Exception:
                    continue
            return out
        except Exception:
            return []

    def turn_log_summary(self):
        """How badly is it doing? Counts, not impressions."""
        rows = self.turn_log(self.TURN_LOG_MAX)
        if not rows:
            return {"turns": 0}
        miss = sum(1 for r in rows if not r.get("understood"))
        silent = sum(1 for r in rows if not (r.get("heard") or "").strip())
        clipped = sum(1 for r in rows if (r.get("clip_pct") or 0) > 2)
        disagreed = sum(1 for r in rows
                        if r.get("slow_text")
                        and r.get("fast_text")
                        and r["slow_text"] != r["fast_text"])
        slow = sum(1 for r in rows if (r.get("total") or 0) > 1.5)
        esc = sum(1 for r in rows if (r.get("slow") or 0) > 0)
        tot = [r.get("total") or 0 for r in rows]
        tot.sort()
        return {
            "turns": len(rows),
            "not_understood": miss,
            "heard_nothing": silent,
            "distorted": clipped,
            "engines_disagreed": disagreed,
            "slow": slow,
            "escalated": esc,
            "p50": tot[len(tot) // 2] if tot else 0,
            "worst": tot[-1] if tot else 0,
        }

    # ── GUIDED SELF-TEST ─────────────────────────────────────────────
    # A scripted run: the station asks for a phrase, listens, and
    # records EXACTLY what happened — what each recogniser heard, what
    # the language layer made of it, how long every stage took, and
    # what the microphone was getting while you said it. At the end it
    # scores itself and says which part is at fault.
    #
    # The phrases are chosen to separate the failure modes: a bare
    # command, a medication name, a navigation request, a
    # conversational reply, and a safety phrase that must never be
    # mishandled.
    SELF_TEST = (
        ("what time is it", "time", "a plain command"),
        ("what do I take today", "remaining_today", "the daily schedule"),
        ("how many pills do I have left", "count", "an inventory question"),
        ("open storage", "nav:storage", "navigation"),
        ("go to settings", "nav:settings", "navigation"),
        ("did I take my medicine today", "taken_today", "a status question"),
        ("what is next", "next_dose", "the next dose"),
        ("how am I doing", "adherence", "adherence"),
        ("yes", "small_talk", "a one-word reply"),
        ("thank you", "thanks", "conversational"),
    )

    def _st_capture(self):
        """Wait for the capture loop to hand back ONE turn.

        The self-test deliberately goes through the real listening
        path — same microphone, same gate, same endpointer, same
        recognisers. A test that used a private shortcut would prove
        nothing about the thing people actually talk to."""
        self._st_result = None
        self._st_want = True
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._st_result is not None:
                got = self._st_result
                self._st_result = None
                self._st_want = False
                return got
            if not self._st or self._st.get("phase") != "run":
                break
            time.sleep(0.05)
        self._st_want = False
        return {}

    def selftest_state(self):
        st = getattr(self, "_st", None)
        if not st:
            return {"phase": "idle", "index": 0,
                    "total": len(self.SELF_TEST), "results": []}
        return st

    def start_selftest(self):
        self._st = {"phase": "run", "index": 0,
                    "total": len(self.SELF_TEST), "results": [],
                    "started": time.time()}
        threading.Thread(target=self._run_selftest, daemon=True,
                         name="selftest").start()
        return True

    def cancel_selftest(self):
        st = getattr(self, "_st", None)
        if st:
            st["phase"] = "cancelled"
        self._st = None

    def _run_selftest(self):
        """Drive the whole script on a worker. Uses the real capture
        path — there is no point testing anything else."""
        st = self._st
        try:
            self._st_request = True     # ask the capture loop for turns
            temp_max = 0.0
            try:
                temp_max = pi_health().get("temp_c", 0.0) or 0.0
            except Exception:
                pass
            for i, (phrase, want, why) in enumerate(self.SELF_TEST):
                if not self._st or self._st.get("phase") != "run":
                    return
                st["index"] = i
                st["prompt"] = phrase
                st["why"] = why
                self._speak("Please say: %s" % phrase)
                got = self._st_capture()
                st["results"].append(self._score_selftest(
                    phrase, want, why, got))
                # watch the temperature climb across the whole test —
                # "never overheats" is one of the benchmarks
                try:
                    temp_max = max(temp_max,
                                   pi_health().get("temp_c", 0.0) or 0.0)
                except Exception:
                    pass
            st["temp_max"] = temp_max
            st["phase"] = "done"
            st["finished"] = time.time()
        except Exception as e:
            st["phase"] = "failed"
            st["error"] = str(e)[:120]
        finally:
            self._st_request = False

    def _score_selftest(self, phrase, want, why, got):
        """One line of the report: did it hear it, and did it act?"""
        heard = (got or {}).get("heard", "")
        rec = {
            "asked": phrase, "why": why, "want": want,
            "heard": heard,
            "vosk": (got or {}).get("vosk", ""),
            "fast": (got or {}).get("fast_text", ""),
            "slow": (got or {}).get("slow_text", ""),
            "secs": (got or {}).get("secs", 0),
            "peak": (got or {}).get("peak", 0),
            "clip_pct": (got or {}).get("clip_pct", 0),
            "snr": (got or {}).get("snr", 0),
            "t_fast": (got or {}).get("fast", 0),
            "t_slow": (got or {}).get("slow", 0),
            "t_total": (got or {}).get("total", 0),
        }
        if not heard:
            rec.update(intent="-", ok=False, words=0,
                       fault="HEARD NOTHING")
            return rec
        try:
            intent = _nlu_mod.parse(heard, self._med_names()).name \
                if _nlu_mod else "?"
        except Exception:
            intent = "?"
        # what the station would actually DO with it
        acted = "?"
        try:
            norm = " " + re.sub(r"[^a-z0-9' ]", " ",
                                heard.lower()).strip() + " "
            hit = self._match_builtin(re.sub(r"\s+", " ", norm))
            if hit and hit[0]:
                acted = hit[0]
            elif self._match_small_talk(heard):
                acted = "small_talk"
            elif intent != "unknown":
                acted = intent
        except Exception:
            pass
        rec["intent"] = acted
        # word-level accuracy of the transcript against what was asked
        a = set(phrase.lower().split())
        b = set(heard.lower().split())
        rec["words"] = round(100.0 * len(a & b) / max(1, len(a)))
        rec["ok"] = (acted == want) or (
            want == "small_talk" and acted in ("small_talk", "thanks"))
        if rec["ok"] and rec["words"] < 60:
            # It got there anyway — the phonetic matcher recovered a
            # bad transcript. Worth knowing: the ACTION was right but
            # the recogniser was not.
            rec["fault"] = "recovered from a mishear"
        elif rec["ok"]:
            rec["fault"] = ""
        elif rec["words"] < 60:
            rec["fault"] = "MISHEARD"
        else:
            rec["fault"] = "heard it, did not act"
        return rec

    def selftest_report(self):
        """Score, verdict, and what to fix — as text."""
        st = getattr(self, "_st", None) or {}
        rows = st.get("results", [])
        if not rows:
            return "No self-test has been run."
        n = len(rows)
        good = sum(1 for r in rows if r.get("ok"))
        misheard = sum(1 for r in rows if r.get("fault") == "MISHEARD")
        recovered = sum(1 for r in rows
                        if r.get("fault") == "recovered from a mishear")
        silent = sum(1 for r in rows
                     if r.get("fault") == "HEARD NOTHING")
        inact = sum(1 for r in rows
                    if r.get("fault") == "heard it, did not act")
        words = [r.get("words", 0) for r in rows if r.get("heard")]
        wacc = round(sum(words) / max(1, len(words)))
        times = sorted(r.get("t_total", 0) for r in rows)
        p50 = times[len(times) // 2] if times else 0
        worst = times[-1] if times else 0
        clip = max([r.get("clip_pct", 0) for r in rows] or [0])
        snr = min([r.get("snr", 0) for r in rows if r.get("heard")]
                  or [0])

        out = ["## SELF-TEST RESULT",
               "  score            %d/%d  (%d%%)" % (good, n,
                                                     100 * good // n),
               "  word accuracy    %d%%" % wacc,
               "  typical turn     %.2f s" % p50,
               "  worst turn       %.2f s" % worst,
               "  heard nothing    %d" % silent,
               "  misheard         %d" % misheard,
               "  heard, no action %d" % inact,
               "  recovered        %d  (acted right despite a bad "
               "transcript)" % recovered,
               "  worst clipping   %d%%" % clip,
               "  worst SNR        %.1f" % snr,
               ""]

        # ── THE BENCHMARK: the three targets, PASS/FAIL ──
        # These are the numbers to hit: recognise it every time, answer
        # in under half a second, and never cook the Pi.
        acc = 100.0 * good / n
        temp_max = st.get("temp_max", 0.0) or 0.0
        def _pf(ok):
            return "PASS" if ok else "FAIL"
        out.append("## TARGETS (the benchmark)")
        out.append("  accuracy   %5.1f%%      target >= %.1f%%   %s"
                   % (acc, TARGET_ACCURACY, _pf(acc >= TARGET_ACCURACY)))
        out.append("  latency    %5.2fs p50   target <  %.2fs    %s"
                   % (p50, TARGET_LATENCY, _pf(p50 < TARGET_LATENCY)))
        out.append("  latency    %5.2fs worst target <  %.2fs    %s"
                   % (worst, TARGET_LATENCY_WORST,
                      _pf(worst < TARGET_LATENCY_WORST)))
        out.append("  thermal    %5.1fC peak  target <  %.0fC     %s"
                   % (temp_max, TARGET_TEMP_MAX,
                      _pf(0 < temp_max < TARGET_TEMP_MAX)))
        allpass = (acc >= TARGET_ACCURACY and p50 < TARGET_LATENCY
                   and worst < TARGET_LATENCY_WORST
                   and 0 < temp_max < TARGET_TEMP_MAX)
        out.append("  OVERALL    %s" % ("ALL TARGETS MET"
                                        if allpass else "NOT YET"))
        out.append("")

        # ── the verdict: WHICH PART is at fault ──
        out.append("## WHAT TO FIX")
        if silent > n // 3:
            out.append("  AUDIO PATH. It heard nothing on %d of %d. The "
                       "microphone is not reaching the recogniser — "
                       "check the device in Settings > Audio, and run "
                       "TUNE ROOM." % (silent, n))
        elif clip > 5:
            out.append("  INPUT TOO HOT. %d%% of blocks clipped. The "
                       "capture level is overdriven; a clipped "
                       "waveform carries less than a quiet one."
                       % clip)
        elif snr and snr < 4:
            out.append("  ROOM TOO NOISY. Best signal-to-noise was "
                       "%.1f. Speech has to stand clear of the room; "
                       "this is a microphone-placement problem, not a "
                       "software one." % snr)
        elif misheard > inact and misheard:
            out.append("  RECOGNITION. %d of %d were misheard (word "
                       "accuracy %d%%). The audio is fine and the "
                       "rules are fine; the model is the limit."
                       % (misheard, n, wacc))
        elif inact:
            out.append("  VOCABULARY. %d were heard correctly and still "
                       "not acted on — the words reached the station "
                       "and it did not know what they meant. That is "
                       "fixable in the rules." % inact)
        elif worst > 2.0:
            out.append("  SPEED. Everything was understood, but the "
                       "worst turn took %.1f s." % worst)
        else:
            out.append("  Nothing. %d/%d, typical turn %.2f s."
                       % (good, n, p50))

        h = pi_health()
        out.append("")
        out.append("## HARDWARE DURING THE TEST")
        out.append("  load %.2f on %d cores · %d MHz (%s) · %.1f C%s%s"
                   % (h["load"], h["cores"], h["mhz"],
                      h["governor"] or "?", h["temp_c"],
                      " · THROTTLING" if h["throttled"] else "",
                      " · UNDER-VOLTAGE" if h["under_voltage"] else ""))
        out.append("  speech threads %d continuous / %d burst"
                   % (INFER_THREADS, STT_THREADS))
        _fe = self._fast_engine()
        if _fe == "moonshine":
            _fast_lbl = "moonshine %s" % (
                (self._ms_arch_used or "?").split("_")[0].lower())
        else:
            _fast_lbl = "whisper %s" % getattr(
                self, "_whisper_fast_size", FAST_WHISPER_MODEL)
        _msr = getattr(self, "_ms_race_secs", None)
        _fwr = getattr(self, "_fw_race_secs", None)
        if _msr is not None and _fwr is not None:
            def _rf(x):
                return ("%.2fs" % x) if x and x < float("inf") else "-"
            _fast_lbl += " (race ms %s/wh %s)" % (_rf(_msr), _rf(_fwr))
        out.append("  fast model %s · backup %s"
                   % (_fast_lbl, self._whisper_size or "?"))

        out.append("")
        out.append("## EVERY PHRASE")
        for r in rows:
            out.append("  [%s] asked : %r  (%s)"
                       % ("OK " if r.get("ok") else "BAD",
                          r.get("asked"), r.get("why")))
            out.append("       heard : %r  (%d%% of the words)"
                       % (r.get("heard"), r.get("words", 0)))
            if r.get("vosk") or r.get("fast") or r.get("slow"):
                out.append("       live=%r fast=%r slow=%r"
                           % (r.get("vosk"), r.get("fast"),
                              r.get("slow")))
            out.append("       action: %s (wanted %s)%s"
                       % (r.get("intent"), r.get("want"),
                          "  <-- " + r["fault"] if r.get("fault")
                          else ""))
            out.append("       audio : %.1fs peak %s clip %s%% snr %s"
                       % (r.get("secs", 0) or 0, r.get("peak"),
                          r.get("clip_pct"), r.get("snr")))
            out.append("       timing: fast %.2fs slow %.2fs total %.2fs"
                       % (r.get("t_fast", 0), r.get("t_slow", 0),
                          r.get("t_total", 0)))
            out.append("")
        return "\n".join(out)

    def turn_report(self):
        """What the last turn actually cost, stage by stage.

        Not estimates — the real clock, from this device. This is what
        the audit page shows, so a slow assistant can be diagnosed
        from a photograph of the screen instead of guessed at."""
        t = getattr(self, "_turn", None) or {}
        rows = []

        def row(label, val, good, detail=""):
            rows.append((label, val, good, detail))

        if not t:
            return [("No turn yet", "tap the logo and say something",
                     True, "")]
        row("You stopped talking", "%.2f s wait" % t.get("endpoint", 0),
            t.get("endpoint", 0) <= 1.0, "policy")
        row("Heard by", t.get("engine", "-"), True,
            t.get("model", ""))
        row("  fast model", "%.2f s" % t.get("fast", 0),
            t.get("fast", 0) < 0.8, "moonshine")
        if t.get("slow", 0) > 0:
            row("  escalated", "%.2f s" % t.get("slow", 0),
                t.get("slow", 0) < 2.0, "whisper")
        row("Understood", "%.3f s" % t.get("think", 0),
            t.get("think", 0) < 0.1, "on-device")
        row("First words out", "%.2f s" % t.get("speak", 0),
            t.get("speak", 0) < 1.0,
            "cached" if t.get("cached") else "synthesized")
        total = t.get("total", 0)
        row("TOTAL", "%.2f s" % total, total < 1.5, "stop -> reply")
        row("You said", (t.get("text") or "-")[:38], True, "")
        return rows

    def _handle_exchange(self, rec, text):
        """A conversation, not a single question.

        This used to answer once and stop unless a flow explicitly held
        the mic open, so a follow-up question went nowhere — you had to
        start again for every sentence. Now the mic stays open after
        every answer and only closes when you say so, or when you stop
        talking for a while.

        It also waits for her to actually FINISH before listening
        again. Without that she hears her own voice through the speaker
        and answers herself."""
        self._closed.clear()
        while True:
            t_stop = getattr(self, "_turn_stopped_at", time.time())
            # WHERE THE HALF-SECOND NOBODY MEASURED IS GOING.
            #
            # total is (t1 - t_stop) + the first chunk's render, and
            # with the speculation landing the device reads:
            #
            #   endpoint 0.46   stt 0.00   speak 0.00   TOTAL 1.07
            #
            # 0.46 + 0.00 + 0.00 does not make 1.07. Six tenths of a
            # second happen between the person stopping and the reply
            # being chosen, and not one line of this program measured
            # them — so every explanation of them would have been a
            # guess, and this file records what guessing has cost.
            #
            #   pre    stopping -> arriving here (endpoint, transcript,
            #          and the listen loop's own overhead)
            #   ui     the "thinking" state reaching the screen, which
            #          marshals onto the Tk thread and WAITS for it
            #   think  respond() — the language layer itself
            t_enter = time.time()
            self._set_ui_state("thinking", user_text=text)
            t_ui = time.time() - t_enter
            t0 = time.time()
            reply, keep_listening = self.respond(text)
            t_think = time.time() - t0
            t1 = time.time()
            self._speak(reply, user_text=text)
            # Everything above, measured. turn_report() renders it.
            self._turn = {
                "endpoint": getattr(self, "_t_endpoint", 0.0),
                "fast": getattr(self, "_t_fast", 0.0),
                "slow": getattr(self, "_t_slow", 0.0),
                "engine": getattr(self, "_last_engine", "-"),
                "model": (self._ms_arch_used or "").split("_")[0].lower()
                or self._whisper_size,
                "think": t_think,
                # See the comment in _handle_exchange: total is not
                # endpoint + stt + speak, and the difference was
                # unmeasured until now.
                "pre": t_enter - t_stop,
                "ui": t_ui,
                "speak": getattr(self, "_t_first_sound", 0.0),
                # WHERE THE FIRST CHUNK'S TIME WENT. `speak` above is
                # one number and it has disagreed with every bench I
                # ran by about two seconds. This is the breakdown from
                # inside render_to_cache: the cache lookup, whether it
                # was a hit, the synthesis, and the write.
                "tts": getattr(self, "_t_tts", {}),
                "cached": getattr(self, "_t_cached", False),
                "total": (t1 - t_stop) + getattr(
                    self, "_t_first_sound", 0.0),
                "text": text,
                # WHY this turn cost what it cost. A 25-second turn and
                # a 2-second turn look identical in a log that records
                # only the total, and the difference between them is a
                # decision the station made — escalate or not, reuse the
                # speculation or redo it. Record the decision next to
                # its price.
                "stt_note": getattr(self, "_stt_note", ""),
                # Where the fast pass spent its time, so a 31-second
                # one can be explained instead of guessed at.
                "fw": {k: round(getattr(self, "_t_fw_" + k, 0.0), 2)
                       for k in ("wav", "prompt", "call", "decode",
                                 "total", "audio")},
                "spec_hit": getattr(self, "_spec_hits", 0),
                "spec_why": getattr(self, "_spec_why", ""),
                "spec_miss": getattr(self, "_spec_misses", 0),
                "spec_wait": round(getattr(self, "_t_spec_wait", 0.0), 2),
                "budget": STT_TURN_BUDGET,
            }
            # Log it. "understood" is the thing that matters most: did
            # the station work out what was being asked, or did it fall
            # through to a shrug?
            low = (reply or "").lower()
            understood = not any(
                p in low for p in ("instruction unclear",
                                   "did not copy", "didn't catch",
                                   "insufficient data",
                                   "that instruction is unclear"))
            # What the language layer made of it — did it know how to
            # respond, or did it fall through?
            intent = "?"
            try:
                if _nlu_mod is not None:
                    intent = _nlu_mod.parse(text, self._med_names()).name
            except Exception:
                pass
            blocks = max(1, getattr(self, "_turn_blocks", 1))
            self._log_turn({
                "at": time.strftime("%H:%M:%S"),
                # EACH recogniser's own answer, so a disagreement is
                # visible instead of averaged away
                "vosk": (getattr(self, "_raw_vosk", "") or "")[:60],
                "fast_text": (getattr(self, "_raw_fast", "") or "")[:60],
                "slow_text": (getattr(self, "_raw_slow", "") or "")[:60],
                "heard": text,
                "engine": self._turn["engine"],
                "model": self._turn["model"],
                "intent": intent,
                "understood": understood,
                "reply": (reply or "")[:120],
                "endpoint": round(self._turn["endpoint"], 2),
                "fast": round(self._turn["fast"], 2),
                "slow": round(self._turn["slow"], 2),
                "speak": round(self._turn["speak"], 2),
                "total": round(self._turn["total"], 2),
                # The three that add up to the difference between
                # total and (endpoint + stt + speak). See the comment
                # in _handle_exchange.
                "pre": round(self._turn.get("pre", 0.0), 2),
                "ui": round(self._turn.get("ui", 0.0), 2),
                "think": round(self._turn.get("think", 0.0), 2),
                # audio quality: is it hearing clean speech, or mush?
                "room": round(getattr(self, "_nfloor", 0)),
                "voice": round(getattr(self, "_speech_level", 0)),
                "peak": getattr(self, "_turn_peak", 0),
                "clip_pct": round(
                    100.0 * getattr(self, "_turn_clip", 0) / blocks),
                "snr": round(getattr(self, "_turn_snr", 0.0), 1),
                "secs": getattr(self, "_turn_secs", None)
                if getattr(self, "_turn_secs", None) is not None
                else round(blocks * BLOCK_SIZE / float(SAMPLE_RATE), 1),
                # WHERE THE FAST PASS SPENT ITS TIME. These were added
                # to self._turn, which turn_report() renders on screen —
                # and NOT to the row that is written to turns.jsonl, so
                # the one place anyone actually reads afterwards did not
                # have them. A diagnostic in the wrong dictionary is a
                # diagnostic that does not exist.
                "fw": {k: round(getattr(self, "_t_fw_" + k, 0.0), 2)
                       for k in ("wav", "prompt", "call", "decode",
                                 "total", "audio")},
                # AND THE SAME MISTAKE, IN THE SAME FILE, TO THE SAME
                # DICTIONARY, BY THE SAME PERSON WHO WROTE THE COMMENT
                # DIRECTLY ABOVE.
                #
                # render_to_cache's breakdown went into self._turn,
                # which the on-screen report renders, and not here. The
                # device duly logged `tts=None` on every row of the run
                # that existed to read it. The paragraph above is about
                # exactly this and I read it while writing the bug.
                "tts": getattr(self, "_t_tts", None),
                # One level down again: the worker attempt, resolving
                # the voice, and the synthesis itself, separately.
                "synth": getattr(self, "_t_synth", None),
                "stt_note": getattr(self, "_stt_note", ""),
                "spec_hit": getattr(self, "_spec_hits", 0),
                "spec_why": getattr(self, "_spec_why", ""),
                "spec_miss": getattr(self, "_spec_misses", 0),
                "quick": getattr(self, "_quick_hits", 0),
                "warmed": bool(getattr(self, "_warmed", False)),
                "esc_loaded": getattr(self, "_whisper", None) is not None,
            })

            # Stamp when this turn ended, so background work (the
            # escalation model's load, above all) can tell the
            # difference between "idle" and "idle for a moment between
            # two sentences".
            self._last_turn_end = time.time()
            # she has stopped speaking; clear whatever the microphone
            # picked up of her own voice before listening again
            self._drain(rec)
            self.reset_vad()
            self._last_voice_ts = 0.0
            if self._barge:
                # They cut her off. That is not a failure to handle —
                # it is them taking their turn, so take it immediately
                # rather than waiting out a pause they already filled.
                self._barge = False
                self._barge_frames = 0

            if self._closed.is_set():
                break

            # A flow (adding a medication) asks its own questions and
            # gets the full flow timeout. Otherwise this is an open
            # conversation: keep listening for a follow-up, but not
            # forever.
            wait = FLOW_TIMEOUT if keep_listening else FOLLOWUP_TIMEOUT
            text = self._listen_command(rec, timeout=wait)

            if not text:
                if keep_listening:
                    self._flow = None
                    self._speak("Standing by, Ryan.")
                break                      # silence simply ends it

            if self._is_done_talking(text):
                self._flow = None
                self._speak("Okay, Ryan.")
                break
            if self._closed.is_set():
                break
        self._flow = None
        self._set_ui_state("idle")

    def close_conversation(self):
        """Called from the UI when the screen is tapped outside the
        voice panel. Ends the conversation at the next safe point."""
        self._closed.set()

    # ══════════════════════════════════════════════════════════════════
    #  LEARNING — corrections teach phrase→intent mappings and
    #  medication-name aliases; instance-based, persisted, offline
    # ══════════════════════════════════════════════════════════════════
    def _learn_load(self):
        try:
            with open(LEARN_PATH) as f:
                data = json.load(f)
            data.setdefault("phrases", {})
            data.setdefault("aliases", {})
            data.setdefault("stats", {"corrections": 0, "praise": 0})
            return data
        except Exception:
            return {"phrases": {}, "aliases": {},
                    "stats": {"corrections": 0, "praise": 0}}

    def _learn_save(self):
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            with open(LEARN_PATH, "w") as f:
                json.dump(self._learn, f, indent=1)
        except Exception:
            pass

    def _learn_phrase(self, text, intent_id, arg):
        text = text.strip()[:200]           # bounded storage
        if arg:
            arg = str(arg)[:80]
        phrases = self._learn["phrases"]
        phrases[text] = {"intent": intent_id, "arg": arg}
        while len(phrases) > MAX_LEARNED:
            phrases.pop(next(iter(phrases)))
        self._learn["stats"]["corrections"] += 1
        self._learn_save()

    def _learn_alias(self, heard, canonical):
        heard = (heard or "").strip()
        if heard and canonical and heard.lower() != canonical.lower():
            self._learn["aliases"][heard.lower()] = canonical
            self._learn_save()

    def _learned_lookup(self, t):
        """Fuzzy match against everything the user has taught us."""
        text = t.strip()
        phrases = self._learn.get("phrases", {})
        if text in phrases:
            e = phrases[text]
            return e["intent"], e.get("arg")
        best, best_score = None, 0.0
        for known, e in phrases.items():
            score = difflib.SequenceMatcher(None, text, known).ratio()
            if score > best_score:
                best, best_score = e, score
        if best and best_score >= LEARN_FUZZ:
            return best["intent"], best.get("arg")
        return None

    # ── dispatcher: every functional intent has a stable id, so both
    #    the built-in matcher and learned phrases route the same way ──
    def _dispatch(self, intent_id, arg=None):
        if intent_id == "remaining_today":
            return self._intent_remaining_today()
        if intent_id == "next_dose":
            return self._intent_next_dose()
        if intent_id == "count":
            return self._intent_count(arg)
        if intent_id == "schedule":
            return self._intent_schedule(arg)
        if intent_id == "med_info":
            return self._intent_med_info(arg)
        if intent_id == "adherence":
            return self._intent_adherence()
        if intent_id == "taken_today":
            return self._intent_taken_today()
        if intent_id == "taken_check":
            return self._intent_taken_check(arg)
        if intent_id == "dispense":
            return self._intent_dispense(arg)
        if intent_id == "addmed":
            return self._flow_start_addmed()
        if intent_id == "time":
            ts = self._fmt_now(datetime.now())
            return f"The time is {ts}.", False
        if intent_id == "date":
            now = datetime.now()
            return ("Today is %s, %s %d." % (
                now.strftime("%A"), now.strftime("%B"), now.day)), False
        if intent_id.startswith("nav:"):
            target = intent_id.split(":", 1)[1]
            self._ui(lambda: self.app._nav(target))
            return {"home": "Home screen, Ryan.",
                    "storage": "Opening storage.",
                    "settings": "Opening settings.",
                    "user": "Here is your record, Ryan."}.get(
                        target, "Done."), False
        return "Instruction unclear. Standing by.", False

    # ── SMALL TALK ───────────────────────────────────────────────────
    # Bare acknowledgements, matched WHOLE.
    #
    # The conversation stays open for several seconds after every
    # answer, so people fill that space the way they do with a person:
    # "yeah", "ok", "sure", "got it", "no", "bye". Sixteen of twenty
    # such words were coming back as "instruction unclear" and being
    # counted as failures — which is most of the "1 of 8 understood"
    # the device reported. They are not failures. They are someone
    # being polite to a machine that had nothing to say back.
    SMALL_TALK = {
        "affirm": ("yeah", "yea", "yep", "yup", "yes", "ok", "okay",
                   "okey", "alright", "all right", "sure", "got it",
                   "understood", "gotcha", "cool", "nice", "great",
                   "good", "perfect", "fine", "right", "correct",
                   "makes sense", "sounds good", "will do"),
        "decline": ("no", "nope", "nah", "naw", "not now", "no thanks",
                    "im good", "i'm good", "its fine", "it's fine"),
        "greet": ("hi", "hey", "yo", "hiya", "howdy", "morning",
                  "good morning", "afternoon", "good afternoon",
                  "evening", "good evening"),
        "farewell": ("bye", "goodbye", "good bye", "see you", "see ya",
                     "later", "good night", "goodnight", "night"),
        "sorry": ("sorry", "my bad", "oops", "whoops", "excuse me",
                  "pardon me"),
        # Asked constantly when it is not working, and it used to be
        # the one question it could not answer.
        "hearme": ("can you hear me", "do you hear me", "are you there",
                   "are you listening", "you there", "hello are you "
                   "there", "can you hear me now", "did you hear me"),
        "filler": ("um", "uh", "erm", "hmm", "hm", "er", "well",
                   "so", "anyway"),
    }

    def _match_small_talk(self, t):
        """Whole-phrase only — 'no thanks' ends a conversation, 'no I
        meant the blue one' does not."""
        phrase = " ".join((t or "").lower().replace("'", "").split())
        if not phrase or len(phrase.split()) > 5:
            return None
        for kind, words in self.SMALL_TALK.items():
            if phrase in {w.replace("'", "") for w in words}:
                return kind
        return None

    def _small_talk_reply(self, kind):
        import random as _r
        if kind == "affirm":
            return _r.choice(["Okay, Ryan.", "Understood.",
                              "Got it.", "Right you are."]), True
        if kind == "decline":
            return _r.choice(["Okay.", "No problem, Ryan.",
                              "Understood."]), True
        if kind == "greet":
            return _r.choice(["Hello, Ryan.", "Hi Ryan — what do you "
                              "need?", "Morning, Ryan."]), True
        if kind == "farewell":
            return "Goodbye, Ryan.", False
        if kind == "sorry":
            return "No need to apologise, Ryan.", True
        if kind == "hearme":
            return "I can hear you, Ryan. Go ahead.", True
        # filler — they are still thinking; say as little as possible
        return "Go on.", True

    def _match_builtin(self, t):
        """Match functional intents only. Returns (intent_id, arg)."""
        def has(*phrases):
            return any(p in t for p in phrases)

        def med_route(intent_id, name):
            """A route that depends on a medication NAME.

            Several patterns here happily pull a "drug name" out of a
            sentence — "what's next" gives "nekst", "did I take my
            medisin" gives "medisin". If that name is not actually a
            medication, this was not a question about one, and
            answering "that medication is not in my database" is both
            wrong and a dead end. Returning None instead lets the
            phonetic command matcher below have a go at what they
            probably meant."""
            name = (name or "").strip()
            if not name:
                return (intent_id, None)
            try:
                key, md = self._find_med(name)
                if md:
                    return (intent_id, name)
            except Exception:
                return (intent_id, name)
            # Not a medication we have. Two very different cases:
            #
            #   "how many BANANA pills do I have"  — they named a real
            #       thing we do not stock. Say so, so they can correct
            #       it and teach us the right name.
            #   "what's NEKST"                     — that is not a name
            #       at all, it is a mis-transcribed command word.
            #
            # Only the second should fall through. The test is whether
            # the extracted name itself sounds like part of a command.
            if _nlu_mod is not None:
                try:
                    if _nlu_mod.match_choice(
                            name, {"cmd": COMMAND_WORDS},
                            threshold=80.0):
                        return None
                except Exception:
                    pass
            return (intent_id, name)

        if has(" add a new medication", " add new medication",
               " add a medication", " add medication", " new medication ",
               " add a new pill", " add a prescription",
               " register a medication", " add a med ", " add a new med "):
            return ("addmed", None)

        m = re.search(r"(?:dispense|give me)(?: my| the| some)? (.+)", t)
        if m:
            return ("dispense", m.group(1))

        if has(" adherence ", " my score ", " how am i doing ",
               " doing this week ", " how have i been ",
               " have i been taking ", " track record ", " performance "):
            return ("adherence", None)

        # "did i take my MEDICINE today" is not a question about a drug
        # called "medicine" — it means "have I taken everything I was
        # supposed to". Generic words are caught first, so the drug
        # matcher is never handed one to guess at.
        GENERIC_MEDS = (
            "medicine", "medicines", "medication", "medications",
            "meds", "med", "pills", "pill", "tablets", "tablet",
            "dose", "doses", "everything", "them all", "them", "it all",
            "my stuff", "anything", "drugs",
        )
        m = re.search(r"(?:did|have) i (?:already )?(?:take|taken|had|have)"
                      r" (?:my |the |any |all (?:of )?my )?([a-z ]+?)"
                      r"(?: today| yet| already| this morning| tonight"
                      r"| this evening)* $", t)
        if m:
            what = m.group(1).strip()
            if what in GENERIC_MEDS:
                return ("taken_today", None)
            # a generic word, misheard — "did i take my medisin" is
            # still a question about the whole day, not about a drug
            # named "medisin". Checked BEFORE the medication route,
            # which would otherwise answer first and never reach here.
            if _nlu_mod is not None:
                try:
                    if _nlu_mod.match_choice(what, {"g": GENERIC_MEDS},
                                             threshold=80.0):
                        return ("taken_today", None)
                except Exception:
                    pass
            r = med_route("taken_check", what)
            if r:
                return r
        if has(" did i take everything ", " have i taken everything ",
               " am i up to date ", " am i caught up ",
               " did i miss anything ", " have i missed anything "):
            return ("taken_today", None)

        m = re.search(r"how many (?:pills? |tablets? )?(?:of )?"
                      r"([a-z ]+?)(?: pills| tablets)?"
                      r"(?: do i have| are)? (?:left|remaining) ?", t)
        if not m:
            m = re.search(r"how (?:many|much) ([a-z ]+?) "
                          r"(?:do i have|is left|left) ", t)
        if m:
            r = med_route("count", m.group(1))
            if r:
                return r
        if has(" how many pills ", " pill count ", " how many do i have "):
            return ("count", None)

        if has(" left today ", " still have today ", " remaining today ",
               " still need to take ", " still have to take ",
               " left for today ", " remain today ",
               " what pills do i still ", " more today "):
            return ("remaining_today", None)

        # Asking about TODAY is a question about the whole day, not
        # just the next one. "What medication do I need to take today"
        # used to fall through to the next-dose rule below and answer
        # with one item — or with "nothing further" once the last one
        # had passed, which is the opposite of helpful.
        if re.search(r"\b(what|which)\b.*\b(take|taking|have|having|"
                     r"need|due)\b.*\btoday\b", t) or \
                has(" my doses today ", " doses for today ",
                    " schedule for today ", " on my schedule today ",
                    " todays medications ", " todays meds "):
            return ("remaining_today", None)

        if has(" take next ", " next dose ", " next medication ",
               " next pill ", " whats next ", " what's next ",
               " what do i need to take ", " what do i take ",
               " what medication do i need ", " what should i take ",
               " due now ", " anything due ", " what is next "):
            return ("next_dose", None)

        m = re.search(r"when (?:do|should|will) i take "
                      r"(?:my |the )?([a-z ]+?) $", t)
        if m:
            r = med_route("schedule", m.group(1))
            if r:
                return r

        m = re.search(r"(?:how (?:do|should) i take|tell me about|"
                      r"what is|whats|what's) (?:my |the )?([a-z ]+?) $", t)
        if m:
            r = med_route("med_info", m.group(1))
            if r:
                return r

        if has(" what time is it ", " what time ", " the time "):
            return ("time", None)
        if has(" what day is it ", " what day ", " the date ",
               " todays date ", " today's date "):
            return ("date", None)

        # ── SCREENS. "go to X", "open X", "take me to X", "show me
        #    X", or just naming the screen. Every screen answers to
        #    several names because people don't know ours: the user
        #    screen is also "my profile", "my record", "my stats".
        nav_m = re.search(
            r"\b(?:go (?:back )?(?:to)?|open|show( me)?|"
            r"take me (?:back )?to|get me (?:back )?to|bring up|"
            r"pull up|switch to|jump to|navigate to|let'?s go to)\b"
            r"(?: the| my| a)?\s+([a-z ]+?)\s*$", t)
        nav_word = (nav_m.group(2).strip() if nav_m else "")
        nav_word = re.sub(r"\b(screen|page|tab|menu|view|section|app|"
                          r"apps)\b", " ", nav_word).strip()

        SCREENS = (
            ("home", ("home", "main", "front", "start", "dashboard")),
            ("storage", ("storage", "medications", "medication",
                         "meds", "medicine", "medicines", "bottles",
                         "cabinet", "inventory", "supply", "pills")),
            ("settings", ("settings", "setting", "options",
                          "preferences", "config", "configuration",
                          "setup", "system")),
            ("user", ("user", "profile", "me", "my record", "record",
                      "stats", "statistics", "adherence", "history",
                      "progress", "account")),
        )
        if nav_word:
            for screen, names in SCREENS:
                if nav_word in names:
                    return ("nav:" + screen, None)
            for screen, names in SCREENS:
                if any(n in nav_word for n in names):
                    return ("nav:" + screen, None)
            # Nothing matched letter for letter. Match on how it
            # SOUNDS instead — "open storge", "open storidge", "open
            # sturge" are all plainly "open storage", and the station
            # should not need a perfect transcript to act on an
            # unambiguous request. (Screens only; the medication
            # matcher stays strict, because showing the wrong page is
            # recoverable and acting on the wrong drug is not.)
            if _nlu_mod is not None:
                try:
                    hit = _nlu_mod.match_choice(
                        nav_word, {k: v for k, v in SCREENS})
                    if hit:
                        return ("nav:" + hit, None)
                except Exception:
                    pass

        if has(" go home ", " home screen ", " show home "):
            return ("nav:home", None)
        if has(" storage ", " my medications ", " my meds "):
            return ("nav:storage", None)
        if has(" settings "):
            return ("nav:settings", None)
        if has(" my stats ", " user screen ", " show my adherence ",
               " my profile ", " my record ", " my progress ",
               " my history ", " my account "):
            return ("nav:user", None)

        # ── NLU FALLBACK: nothing above understood this. The
        #    deterministic pattern set + phonetic drug matcher catch
        #    phrasings the keyword rules miss ("what medication do I
        #    need to take today"). Informational intents only — voice
        #    can never dispense; _intent_dispense just guides to the
        #    screen. Handlers take a spoken string, never None.
        nlu_i = self._nlu(t)
        if nlu_i is not None and nlu_i.name != "unknown":
            arg = nlu_i.med or ""
            mapping = {
                "schedule": ("remaining_today", None),
                "taken_today": ("taken_today", None),
                "next_dose": ("schedule", arg) if arg else ("next_dose", None),
                "pills_left": ("count", arg),
                "did_take": ("taken_check", arg),
                "dispense": ("dispense", arg),
                "time": ("time", None),
            }
            hit = mapping.get(nlu_i.name)
            if hit and (arg or nlu_i.name not in ("pills_left", "did_take",
                                                  "dispense")):
                return hit

        # ── SAFETY BEFORE GUESSWORK ──────────────────────────────
        #    If the pattern set thinks this is a medical question, a
        #    crisis, an emergency, or someone feeling unwell, the
        #    phonetic matcher below does NOT get a turn. Guessing is
        #    fine when the worst case is the wrong screen; it is not
        #    fine here.
        #
        #    This is not theoretical. Fuzz-testing the vocabulary found
        #    "should i stop taking this" matching the SETTINGS screen
        #    — a question about stopping a medication, answered by
        #    opening a menu. One leak in 32 safety phrases, and one is
        #    too many.
        if nlu_i is not None and nlu_i.name in (
                "medical_question", "crisis", "emergency", "unwell"):
            return None

        # ── TRULY LAST: what does this SOUND like?
        #    Everything above is exact-ish matching. This asks "which
        #    of the things I can actually do does this sound most
        #    like?", and it is the difference between a station that
        #    answers only to remembered phrases and one that acts on
        #    what you meant.
        #
        #    It runs dead last on purpose. It used to run earlier and
        #    hijacked "how many banana pills do I have left" into
        #    "opening storage" — which not only answered the wrong
        #    thing but broke the teaching flow, where the station is
        #    supposed to say it does not have that one so you can
        #    correct it and give it the real name.
        if _nlu_mod is not None and len(t.split()) <= 8:
            try:
                hit = _nlu_mod.match_choice(t, COMMAND_VOCAB,
                                            threshold=VOCAB_THRESHOLD)
                if hit:
                    return (hit, None)
            except Exception:
                pass
        return None

    # ══════════════════════════════════════════════════════════════════
    #  THE BRAIN — intent engine with BT-7274's personality
    # ══════════════════════════════════════════════════════════════════
    def _med_names(self):
        """Medication names loaded on THIS device — used as key terms for
        recognition and for the phonetic matcher."""
        try:
            return [md.get("name", "") for md in self.app.med_data.values()
                    if md.get("loaded") and md.get("name")]
        except Exception:
            return []

    def _nlu(self, text):
        """Deterministic understanding with phonetic drug matching that
        REFUSES to guess between similar names. None if unavailable."""
        if _nlu_mod is None:
            return None
        try:
            return _nlu_mod.parse(text, self._med_names())
        except Exception:
            return None

    def respond(self, text):
        """(reply_text, keep_listening). Pure logic — fully testable.

        SAFETY INVARIANTS (do not weaken):
        - The assistant can NEVER dispense, decrement a count, or log
          a dose. Only the physical on-screen flow can. Voice guides.
        - Medical-advice questions get a hard referral to a
          pharmacist/doctor — checked FIRST, before learning, so no
          learned phrase can ever shadow it.
        - Learning only remaps phrases to the same safe intents.
        """
        t = " " + re.sub(r"[^a-z0-9' ]", " ", text.lower()).strip() + " "

        # ── SAFETY GATE — always first ──
        # Self-harm is checked BEFORE the generic emergency line so it
        # gets the suicide & crisis lifeline (988), not just 911.
        _crisis = self._nlu(text) if _nlu_mod is not None else None
        if _crisis is not None and _crisis.name == "crisis":
            return ("I am really glad you told me, Ryan. You do not have "
                    "to go through this alone. You can call or text "
                    "9 8 8, the Suicide and Crisis Lifeline, any time, to "
                    "talk with someone right now. If you are in danger, "
                    "please call 9 1 1."), False

        if self._is_emergency(t):
            return ("This sounds like an emergency, Ryan. I am "
                    "only an assistant — please call 9 1 1, or "
                    "your local emergency number, right now. "
                    "Poison control in the U S is "
                    "1 800, 2 2 2, 1 2 2 2."), False

        if self._is_medical_question(t):
            return ("Safety protocol, Ryan: I cannot give medical "
                    "advice. Never change a dose on your own — "
                    "please contact your pharmacist or doctor. "
                    "Protocol three: protect the patient."), False

        # ── FEELING UNWELL ──────────────────────────────────────────
        # "I don't feel well", "I feel dizzy", "I'm nauseous" used to
        # fall all the way through to "I didn't catch that" — a shrug,
        # to someone telling a MEDICATION device that something is
        # wrong with them. Found by fuzzing the vocabulary, and it is
        # the worst thing in this file to have got wrong.
        #
        # It cannot diagnose and does not try. What it can do is take
        # it seriously, name the people who can help, and make sure
        # nobody in real trouble is left talking to a wall.
        if _crisis is not None and _crisis.name == "unwell":
            return ("I am sorry you are feeling that way, Ryan. I "
                    "cannot tell you what is causing it — please call "
                    "your pharmacist or doctor, and tell them what you "
                    "have taken today. If it is severe, if you are "
                    "struggling to breathe, or if you think you have "
                    "taken too much, call 9 1 1, or poison control at "
                    "1 800, 2 2 2, 1 2 2 2, right now."), False

        # ── STRONG NLU SAFETY UNION (adds to the gates above, never
        #    weakens them): self-harm crisis, emergency and medical
        #    advice are also caught by the deterministic pattern set,
        #    and a drug name we are not SURE of is never guessed.
        nlu_i = None if self._flow else self._nlu(text)
        if nlu_i is not None:
            if nlu_i.name == "emergency":
                return ("This sounds like an emergency, Ryan. I am only "
                        "an assistant — please call 9 1 1 right now."), False
            if nlu_i.name == "medical_question":
                return ("Safety protocol, Ryan: I cannot give medical "
                        "advice. Never change a dose on your own — "
                        "please contact your pharmacist or doctor."), False
            # Refuse to guess between similar medication names.
            if (nlu_i.suggestion and not nlu_i.med
                    and nlu_i.name in ("did_take", "pills_left",
                                       "dispense", "next_dose")):
                return ("I want to be certain before I answer, Ryan — "
                        "did you mean %s?" % nlu_i.suggestion), True

        if self._flow:
            if time.time() - self._flow.get("ts", time.time()) > 120:
                self._flow = None
                return ("That request expired, Ryan — nothing was "
                        "changed. Start again when you're ready."), False
            self._flow["ts"] = time.time()
            return self._flow_step(text, t)

        def has(*phrases):
            return any(p in t for p in phrases)

        # cancel / stop
        if has(" cancel ", " never mind ", " nevermind ", " stop ",
               " forget it ", " go to sleep "):
            return random.choice([
                "Acknowledged. Standing by.",
                "Understood, Ryan.",
                "Cancelling. I will be here."]), False

        # ── corrections: the Ryan teaches, the model learns ──
        if has(" that's wrong ", " thats wrong ", " that is wrong ",
               " you're wrong ", " youre wrong ", " not right ",
               " that's not what i ", " thats not what i ",
               " you got that wrong ", " incorrect ", " wrong answer ",
               " you misunderstood ", " misheard "):
            if not self._last_exchange:
                return ("I have nothing to correct yet, Ryan. "
                        "Give me an instruction first."), False
            self._flow = {"name": "correct", "ts": time.time(),
                          "prev": dict(self._last_exchange)}
            return random.choice([
                "Understood. Corrections improve my model. "
                "What did you mean?",
                "Copy. I will learn from this. Say it the way "
                "you meant it, Ryan.",
                "Recalibrating. What was the correct "
                "instruction?"]), True

        if has(" good job ", " well done ", " that's right ",
               " thats right ", " correct ", " good work ",
               " nice work ", " exactly "):
            self._learn["stats"]["praise"] = \
                self._learn["stats"].get("praise", 0) + 1
            self._learn_save()
            return random.choice([
                "Acknowledged. Reinforcement logged.",
                "Thank you, Ryan. I aim for precision.",
                "Good. My confidence in that pathway just "
                "went up."]), False

        if has(" forget everything ", " delete your training ",
               " delete what you learned ", " reset your learning ",
               " forget what you learned ", " wipe your memory ",
               " delete your memory "):
            n = len(self._learn.get("phrases", {})) + \
                len(self._learn.get("aliases", {}))
            self._learn = {"phrases": {}, "aliases": {},
                           "stats": {"corrections": 0, "praise": 0}}
            self._learn_save()
            return (f"Done. {n} learned item{'s' if n != 1 else ''} "
                    "erased. My factory training remains. Nothing "
                    "else is stored, Ryan."), False

        if has(" what have you learned ", " how much have you learned ",
               " what did you learn ", " your training "):
            st = self._learn.get("stats", {})
            n = len(self._learn.get("phrases", {}))
            a = len(self._learn.get("aliases", {}))
            return (f"Training report: {n} learned phrase"
                    f"{'s' if n != 1 else ''}, {a} vocabulary "
                    f"alias{'es' if a != 1 else ''}, "
                    f"{st.get('corrections', 0)} corrections "
                    "absorbed. Every one made me better, "
                    "Ryan."), False

        # ── learned phrases fire before the built-in matcher ──
        learned = self._learned_lookup(t)
        if learned:
            intent_id, arg = learned
            self._last_exchange = {"text": t, "intent": intent_id,
                                   "arg": arg}
            return self._dispatch(intent_id, arg)

        # ── personality / small talk ──
        if has(" who are you ", " what are you ", " your name ",
               " what is your name ", " whats your name ",
               " what's your name ", " introduce yourself "):
            return random.choice([
                "I am Dose, an artificial intelligence medication "
                "assistant. Not a person — but firmly on your side, "
                "Ryan.",
                "Designation: Dose. I am an A I assistant that "
                "manages your medications. I am also told I am "
                "good company.",
                "I am Dose, an A I assistant. I watch your "
                "schedule so you do not have to. Trust me."]), False

        if has(" protocol", " directives", " your mission ",
               " your purpose "):
            return ("I operate under three protocols. Protocol one: "
                    "link to the patient. Protocol two: uphold the "
                    "schedule. Protocol three: protect the patient."), False

        if has(" joke ", " funny ", " make me laugh "):
            return random.choice([
                "Analyzing humor database. Why did the pill go to "
                "school? To improve its cap-abilities. ... That one "
                "rated poorly in testing.",
                "I know eight hundred and seventy jokes about "
                "medication. Unfortunately, most have side effects.",
                "A skeleton walked into a pharmacy and asked for "
                "something for his body. I am still evaluating why "
                "that is funny."]), False

        if has(" how are you ", " hows it going ", " how's it going ",
               " how are things "):
            return random.choice([
                "All systems nominal, Ryan.",
                "Operational. Sensor sweep complete. Your schedule "
                "is under control.",
                "Functioning at one hundred percent. Thank you for "
                "asking."]), False

        if has(" thank ", " thanks "):
            return random.choice([
                "You're welcome, Ryan.",
                "Acknowledged. Protocol three: protect the patient.",
                "It's what I'm here for."]), False

        if has(" hello ", " hi there ", " good morning ",
               " good evening ", " good afternoon ", " hey there "):
            return random.choice([
                "Hello, Ryan. How can I assist?",
                "Greetings, Ryan. All systems nominal.",
                "Good to hear your voice, Ryan."]), False

        if has(" how do i set ", " how does this work ",
               " walk me through ", " getting started ",
               " get started ", " guide me ", " set up ", " setup ",
               " how do i use "):
            return ("Happy to walk you through it, Ryan. Place a "
                    "Dose bottle in the station with its Q R "
                    "sticker facing the camera — I recognize it in "
                    "seconds. Tap Storage to see it, and tap the "
                    "little clock to set its times and days. When "
                    "a dose is due, the screen and I will both let "
                    "you know. Dispensing is always yours: tap the "
                    "card, hold to confirm, spin the spindle, and "
                    "press confirm. Ask me anything along the "
                    "way."), False

        if has(" how do i dispense ", " how do i take a pill ",
               " how do i get my pill "):
            return ("Simple, Ryan. On the home screen, tap your "
                    "medication's card. Hold the screen to confirm "
                    "it is really you, spin the spindle until your "
                    "dose drops, then press confirm so it is "
                    "logged. I can never do that part for you — "
                    "by design."), False

        if has(" help ", " what can you do ", " what can you say ",
               " commands "):
            return ("I can report which doses remain today, what to "
                    "take next, pill counts, schedules, and your "
                    "adherence score. I can add a new medication by "
                    "voice, and if I get something wrong, say: that "
                    "is wrong — and I will learn. I never dispense: "
                    "that is always your hands, Ryan."), False

        # ── functional intents via the shared matcher ──
        route = self._match_builtin(t)
        if route:
            intent_id, arg = route
            # A capability question, hypothetical, or quotation is not
            # an instruction: describe the capability, do nothing
            if intent_id in ("addmed", "dispense") and \
                    self._is_indirect(t):
                if intent_id == "addmed":
                    return ("I can do that. When you're ready, just "
                            "say: add a new medication — and I'll "
                            "walk you through it."), False
                return ("I never dispense anything myself. I can "
                        "bring a medication up on screen, and the "
                        "rest is always your hands — tap, hold, and "
                        "spin. Nothing happens until you ask for "
                        "real."), False
            # Negation is never simplified away: "don't add..." acts on
            # nothing
            if intent_id in ("addmed", "dispense") and \
                    self._is_negated(t):
                return ("Understood — taking no action, Ryan."), False
            self._last_exchange = {"text": t, "intent": intent_id,
                                   "arg": arg}
            return self._dispatch(intent_id, arg)

        # ── SMALL TALK, before giving up ────────────────────────────
        # Checked here, after every real command has had its chance,
        # so "no" inside a confirmation still means no and "yes" still
        # confirms. Only a bare acknowledgement with nothing else in
        # it reaches this point.
        kind = self._match_small_talk(t)
        if kind:
            reply, keep = self._small_talk_reply(kind)
            self._last_exchange = {"text": t, "intent": "small_talk",
                                   "arg": kind}
            return reply, keep

        # fallback — BT never pretends to understand
        self._last_exchange = {"text": t, "intent": "fallback",
                               "arg": None}
        return random.choice([
            "I didn't catch that, Ryan. If I misheard, say: "
            "that's wrong — and teach me.",
            "Insufficient data. Try: what do I take next?",
            "That instruction is unclear. Say help, for what I "
            "can do."]), False

    INDIRECT_MARKERS = (
        " can you ", " could you ", " would you ", " are you able ",
        " is it possible ", " what if ", " if you ", " imagine ",
        " suppose ", " for example ", " he said ", " she said ",
        " they said ", " my friend said ", " someone said ",
        " i heard ", " hypothetically ", " do you know how to ",
        " would you ever ", " what happens if i say ")

    def _is_indirect(self, t):
        """Capability questions, hypotheticals, and quoted/reported
        speech are NOT instructions."""
        return any(m in t for m in self.INDIRECT_MARKERS)

    NEG_MARKERS = (" don't ", " do not ", " never ", " not going to ",
                   " no need to ")

    def _is_negated(self, t):
        return any(m in t for m in self.NEG_MARKERS)

    @staticmethod
    def _ampm_explicit(raw):
        """True when the utterance states AM/PM or a part of day —
        we never silently pick AM vs PM for a medication time."""
        r = " " + raw.lower() + " "
        return any(m in r for m in (
            " am ", " pm ", " a m ", " p m ", "morning", "evening",
            "night", "afternoon", "noon", "midnight"))

    def _is_emergency(self, t):
        risky = ("emergency", "call 911", "call nine one one",
                 "overdosed", "took too many", "swallowed too many",
                 "chest pain", "can't breathe", "cannot breathe",
                 "heart attack", "stroke", "unconscious",
                 "poisoned", "hurt myself", "kill myself",
                 "end my life", "suicide")
        return any(p in t for p in risky)

    def _is_medical_question(self, t):
        """Dose-change / interaction / medical-advice questions.
        Hard-checked before everything else."""
        # Question-context patterns: "can i take two" is a medical
        # question; "take two tablets at seven thirty" is label
        # dictation and must NOT trip the gate
        risky = ("double dose", "double the", "extra pill",
                 "extra dose", "can i take more", "can i take two",
                 "should i take more", "should i take two",
                 "if i take more", "if i take two", "twice the",
                 "overdose", "skip my", "skip a dose", "skip tonight",
                 "stop taking", "quit taking", "alcohol", "drink with",
                 "mix with", "mixing", "pregnant", "pregnancy",
                 "side effect", "is it safe to",
                 "increase my dose", "decrease my dose", "half a pill",
                 "crush", "expired")
        return any(p in t for p in risky)


    # ── shared helpers ────────────────────────────────────────────────
    def _fmt_now(self, now):
        try:
            return now.strftime("%-I:%M %p")
        except ValueError:
            return now.strftime("%I:%M %p").lstrip("0")

    def _loaded_meds(self):
        out = []
        for key, md in self.app.med_data.items():
            if md.get("loaded") and md.get("count", 0) >= 0:
                out.append((key, md))
        return out

    def _find_med(self, spoken):
        """Fuzzy-match a spoken name against loaded medications."""
        spoken = (spoken or "").strip().lower()
        spoken = re.sub(r"\b(pills?|tablets?|medications?|meds?|dose"
                        r"|the|my)\b", "", spoken).strip()
        if not spoken:
            return None, None
        # learned vocabulary first ("happy pills" -> Sertraline)
        for alias, canonical in self._learn.get("aliases", {}).items():
            if alias and (alias in spoken or difflib.SequenceMatcher(
                    None, spoken, alias).ratio() >= 0.8):
                spoken = canonical.lower()
                break
        best, best_score = None, 0.0
        for key, md in self._loaded_meds():
            name = md.get("name", "").lower()
            score = difflib.SequenceMatcher(None, spoken, name).ratio()
            if name and (name in spoken or spoken in name):
                score = max(score, 0.9)
            if score > best_score:
                best, best_score = (key, md), score
        if best and best_score >= 0.55:
            return best
        return None, None

    # ── intents ───────────────────────────────────────────────────────
    def _today_entries(self):
        return self._ui(lambda: self.app._get_today_schedule()) or []

    def _intent_remaining_today(self):
        entries = self._today_entries()
        if not entries:
            return ("No medications are in view today, Ryan. Place "
                    "a bottle in the station and I will track it."), False
        pending = []
        for e in entries:
            status = self._ui(lambda e=e: self.app._dose_status(
                e["key"], e["time"]))
            if status != "taken":
                pending.append(e)
        if not pending:
            return random.choice([
                "All doses complete. Outstanding work today, Ryan.",
                "Nothing remains. Every dose is logged. Protocol "
                "two is satisfied."]), False
        parts = [f"{e['name']} at {e['time']}"
                 for e in pending[:4]]
        lead = ("One dose remains today: " if len(pending) == 1 else
                f"{len(pending)} doses remain today: ")
        return lead + "; ".join(parts) + ".", False

    def _intent_taken_today(self):
        """'Did I take my medicine today?' — the whole day, not one
        drug. Answers with what IS logged and what is still waiting,
        because "yes" alone is useless when three of four are done."""
        entries = self._today_entries()
        if not entries:
            return ("Nothing is scheduled today, Ryan."), False
        taken, pending = [], []
        for e in entries:
            status = self._ui(lambda e=e: self.app._dose_status(
                e["key"], e["time"]))
            (taken if status == "taken" else pending).append(e)
        if not pending:
            names = ", ".join(sorted({e["name"] for e in taken}))
            return (f"Yes — everything today is logged: {names}. "
                    "Nothing is outstanding, Ryan."), False
        rest = "; ".join(f"{e['name']} at {e['time']}"
                         for e in pending[:4])
        if not taken:
            return (f"Not yet, Ryan. Still to take today: {rest}."), False
        done = ", ".join(sorted({e["name"] for e in taken}))
        return (f"Partly — {done} is logged. Still to take: "
                f"{rest}."), False

    def _intent_next_dose(self):
        due = self._ui(lambda: self.app._dose_due_map()) or {}
        if due:
            key = sorted(due)[0]
            name = self.app.med_data.get(key, {}).get("name", "medication")
            return (f"{name} is due now, Ryan. Scheduled for "
                    f"{due[key]}. The station is "
                    "ready when you are."), False
        entries = self._today_entries()
        now = datetime.now()
        upcoming = []
        for e in entries:
            try:
                dt = e.get("sort")
                if dt and dt > now:
                    status = self._ui(lambda e=e: self.app._dose_status(
                        e["key"], e["time"]))
                    if status != "taken":
                        upcoming.append(e)
            except Exception:
                continue
        if upcoming:
            e = upcoming[0]
            return (f"Next dose: {e['name']} at "
                    f"{e['time']}."), False
        return ("Nothing further is scheduled today, Ryan. "
                "Rest easy."), False

    def _intent_count(self, spoken):
        if spoken and re.sub(r"\b(pills?|tablets?|medications?|meds?"
                             r"|the|my|do|i|have)\b", "",
                             spoken).strip() == "":
            spoken = None
        if spoken:
            key, md = self._find_med(spoken)
            if md:
                c = md.get("count", 0)
                return (f"{md['name']}: {c} pill{'s' if c != 1 else ''} "
                        "remaining."), False
            return (f"I do not have a medication matching "
                    f"{spoken.strip()}, Ryan."), False
        meds = self._loaded_meds()
        if not meds:
            return "No medications are loaded, Ryan.", False
        parts = [f"{md['name']}, {md.get('count', 0)}"
                 for _, md in meds[:5]]
        return "Current inventory: " + "; ".join(parts) + ".", False

    def _intent_schedule(self, spoken):
        key, md = self._find_med(spoken)
        if not md:
            return (f"I could not find {spoken.strip()} in the "
                    "station, Ryan."), False
        times = md.get("dose_times") or [md.get("schedule_time",
                                                "8:00 AM")]
        spoken_times = " and ".join(times)
        days = md.get("schedule_days", [])
        if len(days) >= 7:
            day_part = "every day"
        elif days:
            day_part = "on " + " and ".join(days)
        else:
            day_part = ""
        line = f"{md['name']} is scheduled at {spoken_times} {day_part}"
        return " ".join(line.split()).rstrip(".") + ".", False

    def _intent_med_info(self, spoken):
        key, md = self._find_med(spoken)
        if not md:
            return ("That medication is not in my database, "
                    "Ryan."), False
        lines = None
        if hasattr(self.app, "_med_info_for"):
            lines = self._ui(lambda: self.app._med_info_for(
                md.get("name", "")))
        if not lines:
            lines = ["Follow the directions on your label"]
        joined = ". ".join(l.rstrip(".") for l in lines)
        # NEVER a recommendation from Dose — only attributed label
        # data, with an explicit no-advice boundary
        return (f"The stored label information for {md['name']} "
                f"says: {joined}. That is the label talking, not "
                "me — I cannot give medical advice. For anything "
                "more, ask your pharmacist, Ryan."), False

    def _intent_adherence(self):
        stats = self._ui(lambda: self.app._adherence_stats())
        if not stats:
            return "I do not have adherence data yet, Ryan.", False
        score = stats.get("score", 100)
        if score >= 90:
            grade = ("Exceptional. You would have made a fine "
                     "Ryan in any regiment.")
        elif score >= 75:
            grade = "Solid performance. Minor deviations noted."
        elif score >= 50:
            grade = ("We have work to do, Ryan. I will keep "
                     "the reminders coming.")
        else:
            grade = ("Protocol two is at risk. Let us rebuild "
                     "the routine together.")
        return (f"Adherence score: {score} percent. "
                f"{stats.get('on_time', 0)} on time, "
                f"{stats.get('late', 0)} late, "
                f"{stats.get('missed', 0)} missed. {grade}"), False

    def _intent_taken_check(self, spoken):
        key, md = self._find_med(spoken)
        if not md:
            return f"I could not find {spoken.strip()}, Ryan.", False
        times = md.get("dose_times") or []
        taken_any, pending = [], []
        for ts in times:
            status = self._ui(lambda ts=ts: self.app._dose_status(
                key, ts))
            (taken_any if status == "taken" else pending).append(ts)
        if taken_any and not pending:
            return (f"Affirmative. {md['name']} is logged for "
                    "today."), False
        if taken_any:
            return (f"Partially. {md['name']} at "
                    f"{pending[0]} is still "
                    "pending."), False
        return (f"Negative, Ryan. {md['name']} has not been "
                "dispensed today."), False

    def _intent_dispense(self, spoken):
        """SAFETY: the voice assistant never dispenses and never
        starts the dispense flow. It brings up the home screen and
        tells the Ryan where to press — every transaction requires
        the physical hold, spin, and confirm."""
        key, md = self._find_med(spoken)
        if not md:
            return (f"I could not find {spoken.strip()} in the "
                    "station, Ryan."), False
        if md.get("count", 0) <= 0:
            return (f"{md['name']} is empty. Please reload the "
                    "storage first."), False
        self._ui(lambda: self.app._nav("home"))
        return (f"Safety protocol: I never dispense medication "
                f"myself. {md['name']} is on the home screen — "
                "tap its card, hold to confirm, and spin. Your "
                "hands, your call, Ryan."), False


    # ── multi-turn flows ──────────────────────────────────────────────
    def _flow_start_addmed(self):
        demo = self.app.med_data.get("demo", {})
        if demo.get("loaded"):
            return ("The self-fill slot is already occupied by "
                    f"{demo.get('name', 'a medication')}. Remove it "
                    "first, Ryan."), False
        self._flow = {"name": "addmed", "step": "name", "data": {},
                      "ts": time.time()}
        return ("Understood. New medication intake. First: what is "
                "the medication called?"), True

    def _flow_step(self, raw, t):
        flow = self._flow

        if any(p in t for p in (" cancel ", " never mind ",
                                " nevermind ", " stop ", " forget it ")):
            self._flow = None
            return "Intake cancelled. Standing by, Ryan.", False

        if flow["name"] == "correct":
            self._flow = None
            route = self._match_builtin(t)
            if route is None:
                return ("I still do not recognize that instruction, "
                        "Ryan. No changes made — we will try "
                        "again another time."), False
            intent_id, arg = route
            prev = flow.get("prev") or {}
            prev_text = prev.get("text", "").strip()
            if prev_text:
                self._learn_phrase(prev_text, intent_id, arg)
            # vocabulary: if the old attempt had a med name I could
            # not resolve and the correction resolves one, alias it
            prev_arg = prev.get("arg")
            if prev_arg and arg:
                _, old_md = self._find_med(prev_arg)
                _, new_md = self._find_med(arg)
                if old_md is None and new_md is not None:
                    cleaned = re.sub(r"\b(pills?|tablets?|medications?"
                                     r"|meds?|the|my)\b", "",
                                     prev_arg).strip()
                    self._learn_alias(cleaned, new_md["name"])
            self._last_exchange = {"text": t, "intent": intent_id,
                                   "arg": arg}
            reply, keep = self._dispatch(intent_id, arg)
            return "Correction stored. " + reply, keep

        if flow["name"] != "addmed":
            self._flow = None
            return "Flow error. Standing by.", False

        step, data = flow["step"], flow["data"]

        if step == "name":
            name = " ".join(w.capitalize() for w in raw.split())[:40]
            if not name:
                return "I didn't catch the name. Say it again?", True
            data["name"] = name
            flow["step"] = "label"
            return (f"{name}. Copy. Now read me the label — the "
                    "directions, how many pills, and what time to "
                    "take it."), True

        if step == "label":
            data["label"] = raw
            ts = parse_spoken_time(raw)
            if ts:
                if self._ampm_explicit(raw):
                    data["time"] = ts
                else:
                    data["time_pending"] = ts
            # bottle quantity: dose amounts ("take ONE tablet") are
            # small — only counts of 5+ next to pills/count qualify
            for qm in re.finditer(
                    r"((?:\d+|(?:%s)(?: (?:%s))?)) "
                    r"(?:pills?|tablets?|capsules?|count)"
                    % ("|".join(NUM_WORDS), "|".join(NUM_WORDS)), t):
                q = words_to_number(qm.group(1))
                if q and 5 <= q <= 500:
                    data["qty"] = q
            if "qty" not in data:
                flow["step"] = "qty"
                return "Copy. How many pills are in the bottle?", True
            return self._addmed_after_qty(flow, data)

        if step == "qty":
            q = words_to_number(raw)
            if not q or not (1 <= q <= 500):
                return ("A number, Ryan. How many pills are in "
                        "the bottle?"), True
            data["qty"] = q
            return self._addmed_after_qty(flow, data)

        if step == "time":
            ts = parse_spoken_time(raw)
            if not ts:
                return ("I need a time, Ryan. For example: "
                        "eight AM, or seven thirty PM."), True
            if self._ampm_explicit(raw):
                data["time"] = ts
                flow["step"] = "confirm"
                return self._addmed_confirm_line(data), True
            data["time_pending"] = ts
            flow["step"] = "ampm"
            clock = ts.rsplit(" ", 1)[0]
            return f"{clock} — in the morning, or the evening?", True

        if step == "ampm":
            pending = data.get("time_pending", "8:00 AM")
            clock = pending.rsplit(" ", 1)[0]
            if any(m in t for m in (" am ", " a m ", " morning ")):
                data["time"] = clock + " AM"
            elif any(m in t for m in (" pm ", " p m ", " evening ",
                                      " night ", " afternoon ")):
                data["time"] = clock + " PM"
            else:
                return ("Morning or evening, Ryan? I never guess "
                        "with medication times."), True
            data.pop("time_pending", None)
            flow["step"] = "confirm"
            return self._addmed_confirm_line(data), True

        if step == "confirm":
            if any(p in t for p in (" yes ", " yeah ", " yep ",
                                    " confirm ", " correct ",
                                    " affirmative ", " right ",
                                    " save ", " sure ")):
                self._flow = None
                ok = self._ui(lambda: self._save_new_med(data))
                if ok:
                    return (f"{data['name']} is registered: "
                            f"{data['qty']} pill"
                            f"{'s' if data['qty'] != 1 else ''} at "
                            f"{data['time']} daily. "
                            "Protocol two is watching it now, "
                            "Ryan."), False
                return ("Registration failed — the self-fill slot "
                        "may be occupied. Check the screen, "
                        "Ryan."), False
            if any(p in t for p in (" no ", " nope ", " wrong ",
                                    " negative ", " start over ")):
                flow["step"] = "name"
                flow["data"] = {}
                return ("Understood, we will start over. What is "
                        "the medication called?"), True
            return "Yes to save, or no to start over, Ryan.", True

        self._flow = None
        return "Standing by.", False

    def _addmed_after_qty(self, flow, data):
        if "time" in data:
            flow["step"] = "confirm"
            return self._addmed_confirm_line(data), True
        if "time_pending" in data:
            flow["step"] = "ampm"
            clock = data["time_pending"].rsplit(" ", 1)[0]
            return (f"{clock} — in the morning, or the "
                    "evening?"), True
        flow["step"] = "time"
        return "And what time should you take it?", True

    def _addmed_confirm_line(self, data):
        return ("Confirm intake: %s, %d pills, take at %s, daily. "
                "Is that correct?" % (
                    data.get("name", "unknown"),
                    data.get("qty", 30),
                    data.get("time", "8:00 AM")))

    def _save_new_med(self, data):
        """Runs on the UI thread. Registers into the self-fill slot."""
        app = self.app
        md = app.med_data.get("demo")
        if md is None or md.get("loaded"):
            return False
        md["name"] = data.get("name", "New Medication")
        md["count"] = int(data.get("qty", 30))
        md["loaded"] = True
        md["dose_times"] = [data.get("time", "8:00 AM")]
        md["times_per_day"] = 1
        md["schedule_time"] = md["dose_times"][0]
        try:
            from dose_app import ALL_DAYS
            md["schedule_days"] = list(ALL_DAYS)
        except Exception:
            md["schedule_days"] = ["Mon", "Tue", "Wed", "Thu",
                                   "Fri", "Sat", "Sun"]
        md["tracking_since"] = datetime.now().isoformat()
        app._demo_registered = True
        app._save_med()
        try:
            app._draw_frame()
        except Exception:
            pass
        return True
