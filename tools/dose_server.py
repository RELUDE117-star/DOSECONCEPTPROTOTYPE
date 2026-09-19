#!/usr/bin/env python3
"""DOSE server — the Mac does the listening, the Pi does the talking.

WHY THIS EXISTS
---------------
Speech recognition on a Pi 4 has a floor. The smallest sensible model,
given every core, is 2.12 s of pure inference, and Whisper pads every
utterance to a thirty-second window so a one-word question costs what a
sentence does. Under two seconds, end to end, is not reachable there.

The Mac in the same room is an M1 Pro with eight performance cores and
16 GB. It runs a model several sizes larger in a fraction of that time.
So the cabinet keeps the microphone, the speaker and every decision;
the Mac is handed audio and hands back text.

WHAT THIS IS ALLOWED TO DO, AND NOTHING ELSE
--------------------------------------------
Ryan's standing instruction is that nothing may be able to send
commands to his Mac — the Pi is a thing information is taken FROM, not
a thing that acts ON the Mac. A server the Pi connects to inverts the
direction of the connection, so the endpoint is built to be incapable
of anything else:

  * ONE working route: POST /stt, body is raw WAV bytes, response is
    {"text": "..."}. There is no route that takes a path, a filename,
    a command, a model name or a shell fragment.
  * The request body is decoded by the `wave` module and handed to a
    model. It is never written to a path derived from the request,
    never parsed as JSON, never interpolated into anything.
  * No subprocess, no eval, no exec, no import anywhere in the request
    path. tests/test_dose_server.py fails the build if any appear.
  * Bound to ONE LAN address, chosen explicitly. Never 0.0.0.0.
  * The peer's address must be in a private range (RFC1918), and by
    default must be the Pi's exact address.
  * A bearer token, generated on the Mac, required on every request.
  * A hard body cap. A slow-loris gets a timeout, not a thread.
  * Nothing outbound at request time. The model is fetched once, from
    HuggingFace, when the server is first installed.

It is a microphone cable with a recogniser in the middle. That is the
whole of it.

    python3 tools/dose_server.py --serve
    python3 tools/dose_server.py --token        # print, for the Pi
    python3 tools/dose_server.py --status
"""
import argparse
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
import ssl
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".dose-server")
TOKEN_FILE = os.path.join(STATE_DIR, "token")
LOG_FILE = os.path.join(STATE_DIR, "server.log")
# ── TLS ─────────────────────────────────────────────────────────────
#
# Ryan: "jsut make sure its encrypted", and in the same breath "The
# MAC has to hear the audio in order to do the computing so please
# make sure it stays that way".
#
# Both. The Mac still receives every byte of audio — that is the
# whole reason a turn takes 0.85s instead of nine. What changes is
# that the bytes cross his Wi-Fi inside TLS, so the audio, and
# therefore the words, are readable by the two machines and nobody
# else with a packet capture.
#
# The certificate is created by tools/dose_cert.py, NOT here. This
# file's one hard rule, with a test behind it, is that it runs no
# program and opens no path it does not own — and it stays true.
CERT_FILE = os.path.join(STATE_DIR, "server.crt")
KEY_FILE = os.path.join(STATE_DIR, "server.key")


def tls_ready():
    return os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)

# A three-second utterance at 16 kHz mono is about 96 KB. Twenty
# seconds of it is 640 KB. Two megabytes is generous and still small
# enough that nothing can be pushed through here at scale.
MAX_BODY = int(os.environ.get("DOSE_SERVER_MAX_BODY", 2 * 1024 * 1024))
MAX_SECONDS = float(os.environ.get("DOSE_SERVER_MAX_SECONDS", "30"))
MODEL_NAME = os.environ.get("DOSE_SERVER_MODEL", "small.en")
# THE FAST MODEL, FOR THE PASS THAT RUNS WHILE HE IS STILL TALKING.
#
# Measured on this Mac, same audio, same settings, six clips:
#
#     small.en          median 0.57s   every command phrase exact
#     distil-small.en   median 0.44s   every command phrase exact
#     base.en           median 0.21s   every command phrase exact
#     tiny.en           median 0.12s   every command phrase exact
#
# They agree completely on what the station is actually asked. They
# differ on the hard one — "a little dizzy after the metformin" came
# back as "medformin" from base.en and "med foreman" from tiny.en,
# while small.en and distil-small.en got the drug name right.
#
# So: base.en answers the speculative pass, which fires 0.18s into a
# pause and has to finish before the endpointer does at 0.45s. 0.21s
# fits; 0.57s does not, which is why turns without an internal pause
# were costing a second. If that transcript does not parse into an
# intent — which is exactly the case where the drug name matters —
# the station asks again on /stt and small.en answers properly.
#
# NOT A MODEL NAME IN THE REQUEST. The route is a constant, chosen
# here, like /stt. The Pi picks one of two URLs and never sends a
# name, so "no route takes a path, a filename, a command, a model
# name or a shell fragment" stays literally true.
#
# CHOSEN AGAIN, AFTER THE DEVICE DISAGREED WITH THE BENCHMARK.
#
# base.en gave a median turn of 0.61 s and six of eight under a
# second. It also transcribed "what do I take today" as "what two i
# take today tomorrow", three times in one run, and the station
# answered all three quickly and wrongly. Ryan's instruction covers
# exactly that case: "It needs to be under 1 second AND correct on
# what was actually said and what was transcribed and on how it
# responds." A fast wrong answer is not a win.
#
# distil-small.en was 0.44 s in the same benchmark and got the drug
# name right where base.en did not. The speculation starts 0.18 s into
# a pause and the endpointer fires at 0.45 s, so 0.44 s means waiting
# roughly 0.17 s at the end rather than nothing — a turn near 0.62 s
# instead of 0.47 s, and correct.
#
# That is the trade, made deliberately and in this direction.
FAST_MODEL_NAME = os.environ.get("DOSE_SERVER_FAST_MODEL",
                                 "distil-small.en")

# The device that may ask. Empty means "any private address", which is
# still a LAN-only rule; setting it pins the server to one machine.
ALLOW_PEER = os.environ.get("DOSE_SERVER_PEER", "").strip()

# ── ONE MACHINE MAY TALK TO THIS SERVER ─────────────────────────────
#
# Ryan: "nothing should be able to talk to the Mac besides the pi".
#
# The gate below has always existed and has always been OFF, because
# ALLOW_PEER comes from an environment variable nobody sets. So the
# rule in force was "any private address, with the token" — every
# phone, laptop, television and smart plug on his network was one
# stolen token away from a service that accepts audio.
#
# The token was never weak. But it lives in a file on the Pi, the Pi
# updates itself from a PUBLIC repository, and this station's whole
# threat model is that the Pi is the exposed end. "Hold the token"
# should not be sufficient; "hold the token AND be the cabinet"
# should be.
#
# TRUST ON FIRST USE, AND ONLY WITH THE TOKEN. There is no address to
# hardcode: the Pi is on DHCP, and a literal in this file is a thing
# that silently stops matching the day the router hands out a
# different lease. So the first caller that presents the CORRECT
# TOKEN is written down, and from then on it is the only address
# accepted. Nothing is pinned by merely connecting — an attacker who
# reaches the port first still needs the secret, and if he has the
# secret the pin was never what was protecting anything.
#
# WHEN THE LEASE CHANGES this refuses the real Pi, on purpose. That
# is a deliberate trade: a station that goes quiet and says why in
# one line beats a server that silently widens. The refusal names the
# address, the fix, and the command, so it is a minute of work rather
# than an afternoon.
PEER_FILE = os.path.join(STATE_DIR, "peer")
_PEER = {"addr": None, "read": False}


def pinned_peer():
    """The one address allowed to talk to this server, or None.

    The environment variable still wins when it is set — explicit
    configuration beats anything learned.
    """
    if ALLOW_PEER:
        return ALLOW_PEER
    if not _PEER["read"]:
        _PEER["read"] = True
        try:
            with open(PEER_FILE) as f:
                a = f.read().strip()
            _PEER["addr"] = a or None
        except Exception:
            _PEER["addr"] = None
    return _PEER["addr"]


def pin_peer(addr):
    """Write it down. Called only after a request proved the token."""
    try:
        ipaddress.ip_address(addr)
    except Exception:
        return False
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(PEER_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(addr + "\n")
    _PEER["addr"] = addr
    _PEER["read"] = True
    return True


def unpin_peer():
    """Forget the pinned address — by BLANKING the file, not deleting it.

    test_dose_server.py asserts that this program never calls
    os.remove, and it caught this function doing so. The rule is
    worth more than the tidiness: the one service on the Mac that the
    Pi can reach should not contain the ability to delete a file at
    all, so that no future bug, however clever, can be walked into
    one. An empty file reads as "not pinned" and costs nothing.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(PEER_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    _PEER["addr"] = None
    _PEER["read"] = True

_MODEL = {}          # name -> loaded model, both resident
_STATS = {"requests": 0, "audio_seconds": 0.0, "infer_seconds": 0.0,
          "refused": 0, "vad_rescued": 0, "bad_token": 0,
          "started": time.time()}


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── WHAT HE SAID DOES NOT GO ON THIS MACHINE'S DISK ─────────────────
#
# Ryan drew the line himself, and it is the right line:
#
#     "Keeping all sensitive information like medical on the pi where
#      its hold completely locally and then any unique talking info
#      thats not sensistve through the mac"
#
# This server has to SEE the words — transcribing them is the whole
# job, and it is the reason a turn takes 0.85s instead of nine. What
# it must not do is KEEP them. And it was keeping them: every
# transcript went into ~/.dose-server/server.log, in full, forever.
#
#     stt[small.en] 1.80s of audio in 0.67s -> 'Did I take my aspirin today?'
#
# That line is a medication record. It was useful — reading those
# logs is how a bad test run turned out to be a person talking in the
# room rather than a fault — and being useful is exactly how a health
# record accumulates in a place nobody thinks of as one.
#
# So: the metadata always (how long, how fast, how many words — which
# is what diagnosis actually needs), and the WORDS only when somebody
# turns them on deliberately, for one session, knowing what the log
# becomes. Off is the default and there is no way to reach it from
# the network.
LOG_TEXT = os.environ.get("DOSE_SERVER_LOG_TEXT", "").strip().lower() \
    in ("1", "true", "yes", "on")


def _say(text):
    """A transcript, rendered for the log — without the transcript.

    Returns the words themselves ONLY under DOSE_SERVER_LOG_TEXT.
    Otherwise it returns shape: how many words, how many characters,
    and whether anything came back at all. Every diagnosis this
    project has actually needed from these lines — was it empty, was
    it the prompt echoing, was somebody else talking — survives that,
    because they were all questions about SHAPE.
    """
    t = (text or "").strip()
    if LOG_TEXT:
        return repr(t[:60]) + "  [DOSE_SERVER_LOG_TEXT is on]"
    if not t:
        return "(nothing)"
    return "(%d words, %d chars)" % (len(t.split()), len(t))


def _load_composer():
    """tools/dose_reply.py, if it is beside us.

    Optional, exactly like the vault: a Mac that has not been updated
    yet must keep transcribing. When it is missing every turn comes
    back `reply_kind: "none"` and the Pi answers with its own
    `respond()`, which is the behaviour that existed before any of
    this — so the worst case is "no worse than yesterday".
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dose_reply import compose
        return compose
    except Exception:
        return None


_compose = _load_composer()


def _vault():
    """tools/dose_vault.py, if it is beside us. Optional on purpose:
    a station that has not been through the protection step must keep
    working exactly as before."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import dose_vault
        return dose_vault
    except Exception:
        return None


# THE VALUE, RESOLVED ONCE. Read the comment on token() before
# touching this. It is the difference between one password dialog a
# day and one every few seconds.
_TOKEN = {"value": "", "source": "", "at": 0.0}


def token_now():
    """The token WITHOUT ever asking anybody anything.

    This is what the request path uses. It cannot prompt, it cannot
    block, and it cannot touch the keychain — it returns what was
    resolved at startup or an empty string.

    IT EXISTS BECAUSE `_allowed()` CALLED `token()` ON EVERY REQUEST.
    Ryan protected his tokens, and then:

        "it keeps reasking a bunch of tiems is that normal"
        "i jsut exited the platform but it keeps asking for it"
        "as I exited but it still keeps asking"

    I blamed the panel's five-second status refresh, fixed that, and
    it was only half of it — and the smaller half. The station's
    heartbeat probes `/health` every few seconds while idle, and
    every voice turn is another request; each one went through
    `_allowed()`, which built `"Bearer " + token()`, which asked the
    keychain, which put a dialog on his screen. Closing the app
    changed nothing because it was never the app: it was the Pi,
    politely knocking.

    And the docstring on token() SAID "ONCE, when the server starts —
    not per request", which is what I believed while the code did the
    opposite four lines away. **A docstring is not an invariant.**
    `tests/test_dose_server.py` asserts this one from the syntax tree
    now, because the next person to add a call in a handler will be
    just as sure.
    """
    return _TOKEN["value"] or ""


def token(refresh=False):
    """The shared secret, resolved ONCE and remembered in memory.

    THE KEYCHAIN FIRST, AND IT PROMPTS — ONCE.

    Ryan: "you physically and not an autumn or ai needs to manually
    type in a password on the MacBook itself in order to ever get
    access to any of the token".

    When the token has been moved into the login keychain with no
    trusted applications, reading it here puts a dialog on this Mac
    and waits for him. That is the whole protection and it is
    supposed to cost him one password when the server starts. After
    that the value is held in this process's memory and every request
    compares against that copy; nothing asks again until the server
    is restarted, which in practice is about once a day.

    That is also the honest limit: a process that has been granted
    the token holds it until it exits. Anything that can read this
    process's memory has it. What the keychain stops is the thing he
    was actually worried about — a script, a download, an agent
    reading a file and walking off with the secret — and that it does
    stop, completely.

    A file is still read when the keychain has nothing, because a
    station that has not been through the protection step has to keep
    working. `--status` says which of the two is in force, in words,
    so nobody has to guess whether it is protected.
    """
    if not refresh and _TOKEN["value"]:
        return _TOKEN["value"]
    val, source = _resolve_token()
    _TOKEN["value"] = val
    _TOKEN["source"] = source
    _TOKEN["at"] = time.time()
    return val


# ── AN UNATTENDED RESTART MUST NOT PUT A DIALOG ON HIS SCREEN ───────
#
# Ryan, after a night of deploys:
#
#     "I got a couple more of those rerequesting for access so I jsut
#      typed password"
#     "I dont want to see any more passwords asks for 24 hours at
#      least I already typed it"
#     "I dont want have to keep doing it over and over"
#
# He is right, and the cause is not the protection — it is me. The
# server reads the keychain once at startup, which is correct and
# costs one password. It only became "over and over" because I
# restarted the server five times in one evening to deploy things.
#
# THE HONEST TRADE, stated rather than papered over: a secret that
# only a person can release cannot also be available to a process
# that starts while that person is asleep. Any cached copy on disk is
# readable by anything running as him, which is precisely what the
# keychain was adopted to stop. There is no arrangement that gives
# both.
#
# So this switch says which of the two THIS START is choosing, out
# loud, instead of a prompt nobody is there to answer timing out
# after two minutes and falling through to a file whose contents
# nobody has checked — which is exactly how the station spent an
# hour being refused by its own Mac tonight.
#
# It weakens nothing that was not already true: the file is on disk
# and readable either way. What it removes is a dialog with nobody in
# front of it.
TOKEN_FILE_ONLY = os.environ.get(
    "DOSE_SERVER_TOKEN_FILE_ONLY", "").strip().lower() \
    in ("1", "true", "yes", "on")


def _token_from_file_only():
    """The explicit no-prompt path. Its own function ON PURPOSE.

    test_vault.py asserts that _resolve_token() reaches the keychain
    BEFORE it reaches the file — "a protected token that is ignored
    is not protected" — and it caught this branch, which opens the
    file at the top, breaking that ordering in the source even
    though the normal path is unchanged. The test was right to
    complain: an ordering you have to read a flag to verify is one
    somebody will get wrong later. Kept separate, the ordering in
    _resolve_token() stays literally true.
    """
    log("DOSE_SERVER_TOKEN_FILE_ONLY is set: reading %s and NOT "
        "asking the keychain. Nobody will be prompted by this start. "
        "The keychain copy is untouched and the next ordinary "
        "restart uses it again." % TOKEN_FILE)
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
        if t:
            return t, "file (asked for explicitly, no prompt)"
    except Exception as e:
        log("...and there is no readable token file: %s" % e)
    return "", "none"


def _resolve_token():
    """Where the value actually comes from. Called once per process.

    Split out from token() so the caching is visible and so a test
    can assert that the request path never reaches this."""
    if TOKEN_FILE_ONLY:
        return _token_from_file_only()
    v = _vault()
    held = bool(v is not None and v.supported() and v.present("mac-token"))
    if held:
        val, err = v.get("mac-token")
        if val:
            log("token released from the keychain by someone at this Mac "
                "— held in memory now, nothing will ask again until "
                "this server restarts")
            return val, "keychain"
        # THE KEYCHAIN HOLDS IT AND WOULD NOT HAND IT OVER.
        #
        # Usually because nobody was at the Mac: the dialog waits two
        # minutes and an unattended restart at eleven at night times
        # out. That happened, and what followed is the reason this
        # comment is long.
        #
        # Falling back to the file is only correct if the file agrees
        # with the keychain. It did not, because an EARLIER build
        # minted a fresh token every time the keychain was denied —
        # so the file held a random value nobody else had ever seen.
        # The server came up happily on it and then refused the
        # cabinet, its own paired station, two hundred times:
        #
        #     refused /health from 192.168.4.154: bad token
        #
        # Every layer reported success. The server was listening, TLS
        # was up, the token "loaded". Only the station knew, and all
        # it could say was that the Mac would not talk to it.
        #
        # So the fallback stays — a station that has never been
        # through the protection step has to keep working — but it is
        # now LOUD, and the state is carried out to --status and
        # /health rather than living in one log line at startup.
        log("THE KEYCHAIN DID NOT RELEASE THE TOKEN (%s). Falling "
            "back to %s. If nobody was at this Mac, that is expected "
            "— but if that file disagrees with the station, every "
            "request will be refused as a bad token. Restart this "
            "server while you are at the Mac and answer the dialog."
            % (err or "refused", TOKEN_FILE))
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if t:
                return t, ("file (THE KEYCHAIN WAS NOT ANSWERED)"
                       if held else "file")
    except Exception:
        pass

    # MINTING A NEW TOKEN HERE UNPAIRS THE STATION, SILENTLY.
    #
    # This branch existed for one honest case: a Mac that has never
    # been paired, where any random secret is as good as any other
    # and the panel is about to show it to him.
    #
    # But it also ran every time the keychain REFUSED. Ryan spent an
    # evening pressing Deny on a dialog that would not stop — and
    # each Deny fell through to here, found the plaintext file gone
    # (the protection step moves it aside), minted a brand-new
    # secret, and wrote it to disk. The Pi still holds the original.
    # So every Deny quietly re-keyed the server against a station
    # that could no longer talk to it, and the symptom is a 401 that
    # looks exactly like the token never worked.
    #
    # A secret that is PRESENT but withheld is not a missing secret.
    # If the keychain holds the item and would not release it, the
    # honest outcome is no token and a server that says so — the
    # station falls back to its own models and keeps working, which
    # is slower and entirely correct. Inventing a new shared secret
    # for a pairing that already exists is not a fallback, it is a
    # break.
    if held:
        log("the keychain HOLDS the token and did not release it. "
            "NOT minting a new one — that would re-key this server "
            "against a station that is already paired. Restart the "
            "server and answer the dialog.")
        return "", "withheld"

    t = secrets.token_urlsafe(32)
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t + "\n")
    log("no token anywhere — minted a new one. The station must be "
        "paired again before it can be answered.")
    return t, "new file"


def lan_address():
    """This machine's address on the local network.

    Found by asking the routing table which source address it would use
    to reach a LAN destination — no packet is sent. Deliberately not
    0.0.0.0: binding to everything would also bind to any interface
    that might later be public, and this must never be reachable from
    outside the house.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 9))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def is_private(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private and not ip.is_multicast and not ip.is_reserved


def load_model(name=None):
    """Load one of the TWO models this file names, and keep it.

    `name` is never taken from a request — the callers pass
    MODEL_NAME or FAST_MODEL_NAME, both constants above. It is a
    parameter only so the two routes can share this function.
    """
    name = name or MODEL_NAME
    if name not in (MODEL_NAME, FAST_MODEL_NAME):
        raise ValueError("unknown model")      # belt and braces
    if _MODEL.get(name) is not None:
        return _MODEL[name]
    from faster_whisper import WhisperModel
    t0 = time.time()
    # int8 on the Pi is a compromise for a slow CPU. This machine has
    # eight performance cores and sixteen gigabytes.
    #
    # cpu_threads is stated because CTranslate2 picks its own default
    # and this box has eight performance cores. Worth about ten
    # percent, measured — not the answer to anything on its own, but
    # free.
    _MODEL[name] = WhisperModel(name, device="cpu",
                                compute_type="int8_float32",
                                cpu_threads=8)
    log("model %s loaded in %.1fs" % (name, time.time() - t0))
    return _MODEL[name]


PROMPT = ("Medication reminder device. Commands: what time is it, "
          "what do I take today, how many pills do I have left, "
          "did I take my medicine, open storage, open settings, "
          "go to user, next dose.")


def _norm_words(s):
    return "".join(c if (c.isalnum() or c == " ") else " "
                   for c in (s or "").lower()).split()


# The prompt's OPENING, which is not a thing anybody says to a
# medicine cabinet. The command phrases inside the prompt are exactly
# what people do say, so they cannot be used as a signature — "what
# time is it" is a contiguous substring of the prompt and also the
# most common real question this station gets.
_PROMPT_TELLS = (
    ("medication", "reminder", "device"),
    ("commands", "what", "time", "is", "it", "what", "do", "i", "take"),
)


def _is_prompt_echo(text):
    w = tuple(_norm_words(text))
    if not w:
        return False
    for tell in _PROMPT_TELLS:
        if w[:len(tell)] == tell:
            return True
    return False


def transcribe(wav_bytes, model_name=None):
    """WAV in, text out. The only thing this program does.

    `model_name` comes from the ROUTE, never from the body.
    """
    w = wave.open(BytesIO(wav_bytes), "rb")
    frames, rate = w.getnframes(), w.getframerate()
    secs = frames / float(rate or 16000)
    w.close()
    if secs > MAX_SECONDS:
        raise ValueError("audio too long: %.1fs" % secs)
    model = load_model(model_name)
    t0 = time.time()
    segs, _info = model.transcribe(
        BytesIO(wav_bytes), language="en", beam_size=1,
        vad_filter=True, condition_on_previous_text=False,
        initial_prompt=PROMPT)
    text = " ".join(s.text for s in segs).strip()

    # ── THE VAD THREW THE WHOLE TURN AWAY ───────────────────────────
    #
    # Roughly two turns in every run came back empty, and the log says
    # exactly what happened rather than leaving it to be guessed:
    #
    #     stt[distil-small.en] 2.22s of audio in 0.03s -> (nothing)
    #     stt[small.en]        2.73s of audio in 0.05s -> (nothing)
    #
    # **Three hundredths of a second for two seconds of audio.** The
    # model never ran. `vad_filter=True` decided there was no speech
    # in the clip and handed back nothing, and the station then said
    # "I didn't catch that" about audio it had captured perfectly.
    #
    # That is the exact failure Ryan warned about:
    #
    #     "if it says nothing and the response is nothing it might
    #      seem like it answered but really it was a null value so be
    #      aware of that"
    #
    # Silero's VAD is tuned for a person near a microphone. This
    # station's own test harness plays through a loudspeaker across
    # the room, and the AIRHUG has AI vocal isolation that suppresses
    # loudspeaker audio — so a quiet, real, perfectly intelligible
    # clip measures as "not speech" and never reaches the recogniser.
    #
    # ONE RETRY, WITHOUT THE FILTER, ONLY WHEN THE FIRST PASS FOUND
    # NOTHING. Not a model change — Ryan was explicit that the models
    # stay as they are, and this changes neither. It changes whether
    # the model is asked at all.
    #
    # The cost is bounded and lands only on turns that were already
    # lost: a clip the VAD rejects costs 0.03s, so the retry is the
    # first real decode of that turn, not a second one. A genuinely
    # silent clip comes back empty again and the station behaves
    # exactly as it did before.
    vad_rescue = False
    if not text:
        segs2, _i2 = model.transcribe(
            BytesIO(wav_bytes), language="en", beam_size=1,
            vad_filter=False, condition_on_previous_text=False,
            initial_prompt=PROMPT)
        text2 = " ".join(s.text for s in segs2).strip()
        if text2:
            text = text2
            vad_rescue = True
            _STATS["vad_rescued"] = _STATS.get("vad_rescued", 0) + 1

    if _is_prompt_echo(text):
        # THE MODEL HANDING THE PROMPT BACK.
        #
        # initial_prompt biases the decoder toward this station's
        # vocabulary, which is why every model got every command
        # exactly right. On audio it cannot make out, it sometimes
        # returns the prompt INSTEAD — and the device caught base.en
        # doing it on a real turn:
        #
        #     stt[base.en] 3.88s of audio -> 'Medication reminder device.'
        #
        # which the station then tried to answer. Ryan asked for
        # exactly this to be watched: "it needs to be correct on what
        # was actually said and what was transcribed".
        #
        # Empty is the honest answer, and it is also the useful one:
        # the Pi treats an unusable transcript as a reason to ask
        # /stt, where the stronger model gets a proper try.
        log("dropped a prompt echo: %s" % _say(text))
        text = ""
    took = time.time() - t0
    if vad_rescue:
        # SAY SO, EVERY TIME. A rescue is the recogniser disagreeing
        # with the voice-activity detector about whether anybody
        # spoke, and a station where that happens constantly has a
        # capture problem this is only papering over. The count is in
        # /health too, so it can be watched rather than assumed.
        log("the voice filter found no speech and the model did — "
            "rescued this turn (%d so far)" % _STATS["vad_rescued"])
    _STATS["audio_seconds"] += secs
    _STATS["infer_seconds"] += took
    return text, secs, took


class Handler(BaseHTTPRequestHandler):
    server_version = "dose/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                                   # we do our own

    def _deny(self, code, why):
        _STATS["refused"] += 1
        log("refused %s from %s: %s"
            % (self.path, self.client_address[0], why))
        body = json.dumps({"error": why}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        peer = self.client_address[0]
        if not is_private(peer):
            self._deny(403, "not a local address")
            return False
        # THE PIN IS CHECKED BEFORE THE TOKEN, when there is one.
        # Anything that is not the cabinet is refused without this
        # server ever looking at what it claims to hold — which is
        # the point of having the pin as well as the secret.
        _pin = pinned_peer()
        if _pin and peer != _pin:
            self._deny(403,
                       "not the paired device (this server answers "
                       "%s only). If the cabinet's address changed, "
                       "re-pin it: python3 tools/dose_server.py "
                       "--unpin" % _pin)
            return False
        auth = self.headers.get("Authorization", "")
        # token_now(), NEVER token(). See token_now()'s comment: this
        # line asked the keychain on every request, and the Pi's
        # idle /health probe alone was enough to put a password
        # dialog on Ryan's screen every few seconds.
        want = "Bearer " + token_now()
        if not want.strip() or want == "Bearer ":
            self._deny(503, "this server has no token yet")
            return False
        if not secrets.compare_digest(auth, want):
            # NAME WHAT THIS ACTUALLY MEANS AFTER A FEW OF THEM.
            #
            # "bad token" from an unknown address is an intruder.
            # "bad token" from the same address, over and over, is
            # the paired station holding a DIFFERENT secret than
            # this server — and that reads identically in the log
            # while meaning the opposite. Two hundred of these went
            # by saying nothing useful.
            _STATS["bad_token"] = _STATS.get("bad_token", 0) + 1
            if _STATS["bad_token"] in (3, 25, 100):
                log("%d requests refused as a bad token, all from "
                    "%s. That is not an intruder — that is your "
                    "station, presenting a secret this server does "
                    "not have. They are out of sync. This server's "
                    "token came from: %s. Fix: restart this server "
                    "at the Mac and answer the keychain dialog, or "
                    "re-pair the station from the DOSE panel."
                    % (_STATS["bad_token"], peer,
                       _TOKEN.get("source") or "unknown"))
            self._deny(401, "bad token")
            return False
        # THE TOKEN IS WHAT EARNS THE PIN, and it has just been
        # proved. Only reached when nothing was pinned yet.
        if not _pin:
            if pin_peer(peer):
                log("PAIRED: this server now answers %s and nothing "
                    "else. Everything on this network other than the "
                    "cabinet is refused from here on, whatever token "
                    "it holds. To undo: "
                    "python3 tools/dose_server.py --unpin" % peer)
        return True

    def do_GET(self):
        if self.path == "/health":
            # Deliberately says nothing a stranger could use: no
            # hostname, no paths, no version of anything but this.
            if not self._allowed():
                return
            up = time.time() - _STATS["started"]
            self._ok({"ok": True, "model": MODEL_NAME,
                      "fast_model": FAST_MODEL_NAME,
                      "uptime": round(up), "requests": _STATS["requests"],
                      # How often the recogniser disagreed with the
                      # voice filter about whether anybody spoke.
                      # Watched, not assumed: a station where this
                      # climbs has a capture problem the rescue is
                      # only papering over.
                      "vad_rescued": _STATS.get("vad_rescued", 0),
                      "bad_token": _STATS.get("bad_token", 0),
                      "rtf": round(_STATS["infer_seconds"]
                                   / max(0.001, _STATS["audio_seconds"]), 3)})
            return
        self._deny(404, "no such route")

    # TWO ROUTES, TWO CONSTANTS. Not one route with a parameter:
    # the request body stays pure audio and nothing in it selects
    # anything. See FAST_MODEL_NAME.
    ROUTES = {"/stt": MODEL_NAME, "/stt-fast": FAST_MODEL_NAME}

    def do_POST(self):
        model_name = self.ROUTES.get(self.path)
        if model_name is None:
            self._deny(404, "no such route")
            return
        if not self._allowed():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._deny(400, "bad length")
            return
        if length <= 0 or length > MAX_BODY:
            self._deny(413, "body out of range")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._deny(400, "short body")
            return
        try:
            text, secs, took = transcribe(body, model_name)
        except Exception as e:
            self._deny(400, "not usable audio: %s" % str(e)[:80])
            return
        _STATS["requests"] += 1

        # THE REPLY RIDES BACK WITH THE TRANSCRIPT. Not a second
        # request — Ryan asked for the Mac to decide what to say, and
        # the honest measurement is that deciding is already free on
        # the Pi (`think: 0.00`), so a second round trip would make
        # the station slower in exchange for nothing. In this
        # response it costs a few hundred microseconds of regex and
        # nothing on the wire.
        #
        # `compose()` answers conversation and NOTHING ELSE. Anything
        # touching medication comes back "defer" and the Pi answers
        # it from its own data, offline, as it always has. That
        # split is Ryan's:
        #
        #     "Keeping all sensitive information like medical on the
        #      pi ... and then any unique talking info thats not
        #      sensistve through the mac"
        say, kind, why = "", "none", "composer unavailable"
        if _compose is not None:
            try:
                say, kind, why = _compose(text)
            except Exception as e:
                say, kind, why = "", "none", "composer failed: %s" % \
                    str(e)[:60]
        # `why` is a category name, never his words. `_say()` keeps
        # the transcript itself off this disk.
        log("stt[%s] %.2fs of audio in %.2fs -> %s  reply=%s/%s"
            % (model_name, secs, took, _say(text), kind, why))
        self._ok({"text": text, "audio": round(secs, 2),
                  "took": round(took, 3), "model": model_name,
                  "reply": say, "reply_kind": kind, "reply_why": why})


# ANY LINE THAT ENDS IN QUOTED WORDS. Not a list of the formats I
# remember writing.
#
# The first version of this matched `stt[model] ... ->` and the prompt
# echo line, because those are the two shapes in the current source.
# It redacted 102 lines and reported success, and 261 were still
# there — written by an OLDER build whose format had no brackets:
#
#     stt 2.50s of audio in 0.68s -> 'What time is it?'
#
# A redaction that only knows today's format is not a redaction. The
# whole point of this file is the log that accumulated over WEEKS,
# across builds, which is exactly the material least likely to match
# the pattern I am looking at while I write it.
#
# So: an arrow followed by a quoted string, anywhere, whatever came
# before it. Over-matching here costs a log line's readability.
# Under-matching leaves his medication on disk.
# The arrow OR the prompt-echo colon: the echo line has no arrow and
# carries the transcript just as plainly. Caught by the round-trip
# test below rather than by me remembering it.
TRANSCRIPT_LINE = re.compile(
    r"^(?P<head>.*?(?:->|prompt echo:)\s*)(?P<body>['\"].*)$")


def redact_log():
    """Take the transcripts out of this Mac's log, keeping everything else.

    The log was written before the rule existed, so it holds months
    of lines like

        stt[small.en] 1.80s of audio in 0.67s -> 'Did I take my aspirin today?'

    which is a medication record in a file nobody thinks of as one.
    `_say()` stops new ones; this deals with the ones already there.

    IT DOES NOT DELETE ANYTHING. The original is moved aside to
    `server.log.with-transcripts` and left for Ryan — same rule as
    the plaintext token: I will not destroy a file of his to tidy up
    after myself, and a redaction he cannot check is not a
    redaction. Deleting that copy is one command and it is his to
    run.
    """
    # LOG_FILE, not an argument. The one rule this file has never
    # bent is that every path it opens is a constant it owns, and
    # test_dose_server.py reads that off the syntax tree — a
    # redaction helper is not worth the exception, and the exception
    # is the kind that gets reused later by something reachable from
    # the network.
    if not os.path.exists(LOG_FILE):
        print("no log at %s — nothing to redact" % LOG_FILE)
        return 0
    kept, redacted = [], 0
    with open(LOG_FILE, errors="replace") as f:
        for line in f:
            m = TRANSCRIPT_LINE.match(line.rstrip("\n"))
            if m:
                body = m.group("body")
                words = len(re.findall(r"[A-Za-z']+", body))
                kept.append("%s(%d words — redacted)" % (m.group("head"),
                                                         words))
                redacted += 1
            else:
                kept.append(line.rstrip("\n"))
    aside = LOG_FILE + ".with-transcripts"
    if os.path.exists(aside):
        aside = "%s.%d" % (aside, int(time.time()))
    os.rename(LOG_FILE, aside)
    fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(kept) + "\n")
    print("redacted %d transcript lines in %s" % (redacted, LOG_FILE))
    print("the ORIGINAL, still containing them, is at:")
    print("    %s" % aside)
    print("I have not deleted it. Delete it yourself when you have "
          "looked:")
    print("    rm %s" % aside)
    return 0


def serve(host=None, port=8765, preload=True):
    host = host or lan_address()
    if not is_private(host):
        log("REFUSING to bind %s — that is not a private address" % host)
        return 2
    # ONCE, HERE, BEFORE THE SOCKET IS OPEN. If the token is in the
    # keychain this is the moment the dialog appears, with nobody
    # waiting on the other end of a request. Every request afterwards
    # compares against this value through token_now(), which cannot
    # ask.
    t = token()
    log("token source: %s (asked once, at startup)" % _TOKEN["source"])
    if preload:
        # BOTH, and the fast one FIRST. It is the one a turn waits on,
        # and a station asking for it while it is still loading pays
        # the whole load on the critical path.
        for name in (FAST_MODEL_NAME, MODEL_NAME):
            try:
                load_model(name)
            except Exception as e:
                log("model %s not available yet: %s" % (name, e))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True

    # WRAP THE LISTENING SOCKET, OR SAY PLAINLY THAT IT IS NOT WRAPPED.
    #
    # No silent downgrade, in either direction. A server that quietly
    # serves plain HTTP when its certificate is missing is a server
    # that is one deleted file away from broadcasting his medication
    # to the network with nothing on screen to say so. So the state
    # goes in the log and in --status, in words, every single start.
    scheme = "http"
    if tls_ready():
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # TLS 1.2 is the floor. Everything below it is broken and
            # both ends here are modern; there is no old client to
            # accommodate, so there is no reason to accept one.
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(CERT_FILE, KEY_FILE)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
        except Exception as e:
            # REFUSE. Not "carry on in the clear" — he asked for this
            # to be encrypted, and a station that keeps working by
            # quietly dropping the thing he asked for is the failure
            # this project has written down three times in other
            # forms. The Pi falls back to its own models, which is
            # slower and completely safe.
            log("TLS is configured but could not be started: %s" % e)
            log("REFUSING to serve in the clear. Fix the certificate "
                "(python3 tools/dose_cert.py --status) or remove it "
                "deliberately.")
            return 2
    else:
        log("NO CERTIFICATE — serving plain HTTP. The audio crosses "
            "your network unencrypted. python3 tools/dose_cert.py "
            "--make")
    log("DOSE server on %s://%s:%d  (/stt %s, /stt-fast %s)"
        % (scheme, host, port, MODEL_NAME, FAST_MODEL_NAME))
    _p = pinned_peer()
    log("paired device: %s" % (_p or
        "NOT PINNED YET — the first caller with the right token "
        "becomes the only one accepted"))
    log("token is in %s — install it on the Pi, never anywhere else" % TOKEN_FILE)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("DOSE_SERVER_PORT", "8765")))
    ap.add_argument("--token", action="store_true",
                    help="print the shared secret, for installing on the Pi")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-preload", action="store_true")
    ap.add_argument("--pin", default="",
                    help="pin this server to one address (the cabinet)")
    ap.add_argument("--unpin", action="store_true",
                    help="forget the pinned address; the next caller "
                         "with the right token becomes the new one")
    ap.add_argument("--redact-log", action="store_true",
                    help="strip transcripts out of this Mac's server log")
    a = ap.parse_args()
    if a.token:
        print(token())
        return 0
    if a.pin:
        if pin_peer(a.pin.strip()):
            print("pinned: this server will answer %s and nothing "
                  "else." % a.pin.strip())
            print("Restart the server for it to take effect on a "
                  "running process.")
            return 0
        print("%r is not an address. Nothing was changed." % a.pin)
        return 2
    if a.unpin:
        was = pinned_peer()
        unpin_peer()
        print("unpinned (was %s)." % (was or "nothing"))
        print("The next caller presenting the correct token becomes "
              "the only accepted address. Restart the server, then "
              "let the cabinet make one request.")
        return 0
    if a.redact_log:
        return redact_log()
    if a.status:
        v = _vault()
        protected = bool(v is not None and v.supported()
                         and v.present("mac-token"))
        print(json.dumps(
            {"lan": lan_address(), "port": a.port,
             "model": MODEL_NAME, "fast_model": FAST_MODEL_NAME,
             "token_installed": os.path.exists(TOKEN_FILE),
             # SAY WHICH, IN WORDS. "token_installed: true" was true of
             # a plaintext file anything running as Ryan could read,
             # and it looked reassuring. Whether a person has to
             # approve the read is the thing worth reporting.
             "tls": tls_ready(),
             "encryption": ("the audio is encrypted in transit (TLS, "
                            "with a certificate the station pins)"
                            if tls_ready() else
                            "NOT ENCRYPTED — the audio crosses your "
                            "network in the clear"),
             "token_source": _TOKEN.get("source") or "(not read yet)",
             "token_protected": protected,
             "token_protection": (
                 "keychain — a person must approve each read on this Mac"
                 if protected else
                 "PLAINTEXT FILE — anything running as you can read it"),
             "peer": pinned_peer() or "",
             "peer_state": (
                 ("pinned to %s — nothing else on this network can "
                  "talk to this server" % pinned_peer())
                 if pinned_peer() else
                 "not pinned yet — the first caller with the correct "
                 "token becomes the only one accepted")},
            indent=1))
        return 0
    if a.serve:
        return serve(a.host or None, a.port, not a.no_preload)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
