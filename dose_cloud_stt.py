"""Cloud speech-to-text for the DOSE Home Station — FREE tiers only.

The Raspberry Pi 4B is a poor place to run Whisper: a single utterance
cost 4.47 seconds on the device. This module moves transcription off the
Pi and onto a free cloud service when the internet is reachable, keeping
the local model only as an offline fallback. The Pi's job becomes what it
is good at — capturing clean audio, VAD, buffering, and playback.

STRICTLY FREE. There is no code path here that spends money:

  * Groq  — free-tier Whisper large-v3. On a rate-limit (429) it FAILS
            over; it never asks for more capacity.
  * Hugging Face — a free ZeroGPU Whisper Space via gradio_client, using
            the user's token for their free daily quota. On quota
            exhaustion it FAILS over. It never calls a paid Inference
            Endpoint.

Both providers are optional and are skipped unless a credential is
present. Everything degrades to the local model, and a cloud failure can
never crash the caller — every entry point returns a Result, never
raises.

Shared by the live assistant (dose_voice) and the diagnostic tool
(voice_diagnostics) so the exact same code is measured and shipped.
"""

import json
import os
import socket
import tempfile
import time
import wave

# ── keeping the free tier FREE ────────────────────────────────────────
# A free tier stops being free the moment something calls it in a loop.
# These are the guards, all env-tunable, all deliberately conservative.
#
# The single biggest saving is not a limit at all — it is not uploading
# six times more audio than Whisper can use. See _prepare_upload().
CLOUD_MIN_SECS = float(os.environ.get("DOSE_CLOUD_MIN_SECS", "0.4"))
CLOUD_MAX_SECS = float(os.environ.get("DOSE_CLOUD_MAX_SECS", "20"))
CLOUD_MAX_PER_MIN = int(os.environ.get("DOSE_CLOUD_MAX_PER_MIN", "12"))
CLOUD_MAX_PER_DAY = int(os.environ.get("DOSE_CLOUD_MAX_PER_DAY", "600"))
# After a 429 the polite thing — and the thing that keeps an account in
# good standing — is to stop asking for a while, not to retry harder.
CLOUD_COOLDOWN = float(os.environ.get("DOSE_CLOUD_COOLDOWN", "600"))
BUDGET_PATH = os.path.expanduser(
    os.environ.get("DOSE_CLOUD_BUDGET",
                   "~/dose-home-station/voice/cloud_budget.json"))


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


class Budget:
    """Rate and volume guard for the free cloud tiers.

    Persisted, because an app that restarts often would otherwise reset
    its daily count every launch and quietly blow through a quota it
    believed it was respecting. State is tiny and a corrupt or missing
    file simply starts fresh — this must never be a reason a turn fails.
    """

    def __init__(self, path=BUDGET_PATH):
        self.path = path
        self.state = {"day": _today(), "day_count": 0,
                      "recent": [], "cooldown_until": 0.0}
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                s = json.load(f)
            if isinstance(s, dict):
                self.state.update(s)
        except Exception:
            pass
        if self.state.get("day") != _today():      # new UTC day
            self.state["day"] = _today()
            self.state["day_count"] = 0

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except Exception:
            pass                                   # never fail a turn

    def allow(self, now=None):
        """(ok, reason). reason is for logs, not for the user."""
        now = now or time.time()
        if now < self.state.get("cooldown_until", 0):
            left = int(self.state["cooldown_until"] - now)
            return False, "cooling down after a rate-limit (%ds left)" % left
        if self.state.get("day") != _today():
            self.state["day"] = _today()
            self.state["day_count"] = 0
        if self.state.get("day_count", 0) >= CLOUD_MAX_PER_DAY:
            return False, "daily cap reached (%d)" % CLOUD_MAX_PER_DAY
        recent = [t for t in self.state.get("recent", []) if now - t < 60]
        self.state["recent"] = recent
        if len(recent) >= CLOUD_MAX_PER_MIN:
            return False, "per-minute cap reached (%d)" % CLOUD_MAX_PER_MIN
        return True, ""

    def record(self, now=None):
        now = now or time.time()
        self.state.setdefault("recent", []).append(now)
        self.state["recent"] = [t for t in self.state["recent"]
                                if now - t < 60]
        self.state["day_count"] = self.state.get("day_count", 0) + 1
        self._save()

    def note_rate_limited(self, now=None):
        now = now or time.time()
        self.state["cooldown_until"] = now + CLOUD_COOLDOWN
        self._save()

    def snapshot(self):
        return {"day": self.state.get("day"),
                "today": self.state.get("day_count", 0),
                "last_minute": len([t for t in self.state.get("recent", [])
                                    if time.time() - t < 60]),
                "cooling_down": time.time() < self.state.get(
                    "cooldown_until", 0)}


_BUDGET = None


def budget():
    global _BUDGET
    if _BUDGET is None:
        _BUDGET = Budget()
    return _BUDGET


def wav_duration(path):
    try:
        with wave.open(path) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def _prepare_upload(path):
    """Downmix and downsample to 16 kHz MONO before uploading.

    THE BIGGEST SAVING HERE, and it costs nothing in accuracy. This
    station's microphone only does 48 kHz stereo, and Whisper resamples
    everything to 16 kHz mono on arrival anyway — so uploading the raw
    capture means sending SIX TIMES the bytes for a result that is
    identical. On a domestic uplink that is also six times the wait
    before the answer starts coming back, which is the part a person
    actually feels.

    Returns (path_to_send, is_temp). Any failure returns the original
    file untouched: a tuning step must never be able to lose a turn.
    """
    try:
        with wave.open(path) as w:
            ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            if ch == 1 and sr == 16000 and sw == 2:
                return path, False            # already ideal
            frames = w.readframes(w.getnframes())
        if not frames:
            return path, False
        import audioop
        if sw != 2:
            frames = audioop.lin2lin(frames, sw, 2)
            sw = 2
        if ch == 2:
            frames = audioop.tomono(frames, 2, 0.5, 0.5)
            ch = 1
        if sr != 16000:
            frames, _ = audioop.ratecv(frames, 2, 1, sr, 16000, None)
            sr = 16000
        fd, out = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(out, "wb") as w2:
            w2.setnchannels(1)
            w2.setsampwidth(2)
            w2.setframerate(16000)
            w2.writeframes(frames)
        return out, True
    except Exception:
        return path, False


# ── credentials (never committed; read from env or the device) ────────
def _read_first(paths):
    for p in paths:
        try:
            p = os.path.expanduser(p)
            if os.path.isfile(p):
                v = open(p).read().strip()
                if v:
                    return v
        except Exception:
            continue
    return ""


def hf_token():
    """Hugging Face token, from the environment or the device. Used so
    ZeroGPU requests draw on the user's own free quota."""
    return (os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN")
            or _read_first(["~/dose-home-station/hf_token",
                            "~/.dose_hf_token"]))


def groq_key():
    """Groq API key (free tier), from the environment or the device."""
    return (os.environ.get("GROQ_API_KEY")
            or _read_first(["~/dose-home-station/groq_key",
                            "~/.dose_groq_key"]))


# Provider order. Groq first: its Whisper is genuinely fast (small
# network + inference), which is what a conversation needs. HF ZeroGPU
# second: free but slower and prone to cold starts. Override with
# DOSE_STT_CLOUD_ORDER="hf,groq" etc.
def provider_order():
    raw = os.environ.get("DOSE_STT_CLOUD_ORDER", "groq,hf")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


HF_SPACE = os.environ.get("DOSE_HF_SPACE", "hf-audio/whisper-large-v3")
GROQ_MODEL = os.environ.get("DOSE_GROQ_MODEL", "whisper-large-v3")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class Result:
    """One provider's answer. Never an exception — errors live in .error
    so the caller can log them and move on."""

    def __init__(self, engine, text="", secs=0.0, error="",
                 rate_limited=False):
        self.engine = engine
        self.text = (text or "").strip()
        self.secs = round(secs, 3)
        self.error = error
        self.rate_limited = rate_limited

    @property
    def ok(self):
        return bool(self.text) and not self.error

    def __repr__(self):
        return "Result(%s, %r, %.2fs, err=%r)" % (
            self.engine, self.text[:40], self.secs, self.error[:60])


def is_online(timeout=2.0, hosts=(("1.1.1.1", 53), ("8.8.8.8", 53))):
    """Is there working internet right now? A plain TCP connect to a
    DNS server — no DNS lookup, no HTTP, nothing that a captive portal
    or a blocked domain can fake. Cheap enough to gate every turn."""
    for host, port in hosts:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except Exception:
            continue
    return False


# ── Groq: free-tier Whisper large-v3 ──────────────────────────────────
def transcribe_groq(wav_path, api_key=None, model=None, language="en",
                    timeout=30):
    """Send a WAV to Groq's free Whisper endpoint. English is specified
    because the language is known, which is faster and more accurate.

    Returns a Result. A 429 is a rate-limit: marked, never retried with
    payment. Any other failure is returned as an error so the caller
    falls through to the next provider."""
    t0 = time.time()
    key = api_key or groq_key()
    if not key:
        return Result("groq", error="no GROQ_API_KEY configured")
    try:
        import requests
    except Exception as e:
        return Result("groq", error="requests missing: %s" % e)
    try:
        with open(wav_path, "rb") as f:
            # A FIXED, MEANINGLESS FILENAME. The real basename is a
            # temp name today, but it is the kind of thing that grows a
            # timestamp or a device id later and quietly starts
            # travelling. Nothing about this station needs to reach a
            # third party — they get audio, and that is all.
            files = {"file": ("audio.wav", f, "audio/wav")}
            data = {"model": model or GROQ_MODEL,
                    "response_format": "json",
                    "temperature": "0"}
            if language:
                data["language"] = language
            r = requests.post(
                GROQ_URL, headers={"Authorization": "Bearer " + key},
                files=files, data=data, timeout=timeout)
        if r.status_code == 429:
            return Result("groq", secs=time.time() - t0,
                          error="rate-limited (free tier) — falling over",
                          rate_limited=True)
        if r.status_code == 401:
            return Result("groq", secs=time.time() - t0,
                          error="unauthorized — check GROQ_API_KEY")
        if r.status_code >= 400:
            return Result("groq", secs=time.time() - t0,
                          error="HTTP %d: %s" % (r.status_code,
                                                 r.text[:120]))
        text = (r.json() or {}).get("text", "")
        return Result("groq", text=text, secs=time.time() - t0)
    except Exception as e:
        return Result("groq", secs=time.time() - t0,
                      error="%s: %s" % (type(e).__name__, str(e)[:100]))


# ── Hugging Face: free ZeroGPU Whisper Space via gradio_client ────────
_HF_CLIENT = {}       # cache clients per (space, token) — connecting is slow
_HF_CALL = {}         # cache the resolved (api_name, arg_template) per space


def _hf_client(space, token):
    key = (space, bool(token))
    cli = _HF_CLIENT.get(key)
    if cli is not None:
        return cli
    from gradio_client import Client
    cli = Client(space, hf_token=token or None, verbose=False)
    _HF_CLIENT[key] = cli
    return cli


def _hf_resolve_call(cli):
    """Inspect the Space's real API and work out how to call it, rather
    than assuming parameter names. Returns (api_name, param_infos)."""
    api = cli.view_api(return_format="dict", print_info=False)
    named = (api or {}).get("named_endpoints", {}) or {}
    unnamed = (api or {}).get("unnamed_endpoints", {}) or {}
    candidates = list(named.items()) + [
        ("/" + str(k) if not str(k).startswith("/") else str(k), v)
        for k, v in unnamed.items()]

    def looks_audio(param):
        blob = (str(param.get("python_type", "")) + " "
                + str(param.get("type", "")) + " "
                + str(param.get("label", "")) + " "
                + str(param.get("component", ""))).lower()
        return ("audio" in blob or "filepath" in blob or "file" in blob)

    # prefer an endpoint that clearly takes audio in and returns text
    best = None
    for name, info in candidates:
        params = info.get("parameters", []) or []
        if any(looks_audio(p) for p in params):
            best = (name, params)
            # a transcription-ish name wins outright
            if any(w in name.lower()
                   for w in ("transcri", "predict", "asr", "recogni")):
                return best
    if best:
        return best
    # fall back to the first named endpoint at all
    if candidates:
        name, info = candidates[0]
        return name, info.get("parameters", []) or []
    raise RuntimeError("the Space exposes no callable API")


def _hf_extract_text(result):
    """Pull the transcript out of whatever shape the Space returns."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for k in ("text", "transcription", "transcript", "output"):
            if isinstance(result.get(k), str):
                return result[k]
        # a dict of {value/label}: take the first string value
        for v in result.values():
            if isinstance(v, str):
                return v
    if isinstance(result, (list, tuple)):
        for item in result:
            t = _hf_extract_text(item)
            if t:
                return t
    return str(result)


def transcribe_hf_space(wav_path, space=None, token=None, timeout=90):
    """Transcribe a WAV on a free HF ZeroGPU Whisper Space.

    Uses gradio_client and INSPECTS the Space's schema (view_api) to
    build the call, because Space signatures differ and must not be
    assumed. Never touches a paid Inference Endpoint. Returns a Result;
    quota/cold-start failures come back as errors, not exceptions."""
    t0 = time.time()
    space = space or HF_SPACE
    token = token or hf_token()
    try:
        from gradio_client import handle_file
    except Exception as e:
        return Result("hf", error="gradio_client not installed: %s" % e)
    try:
        cli = _hf_client(space, token)
        api_name, params = _hf_call_cached(cli, space)

        def looks_audio(param):
            blob = (str(param.get("python_type", "")) + " "
                    + str(param.get("type", "")) + " "
                    + str(param.get("label", "")) + " "
                    + str(param.get("component", ""))).lower()
            return ("audio" in blob or "filepath" in blob
                    or "file" in blob)

        args = []
        filled_audio = False
        for p in params:
            if not filled_audio and looks_audio(p):
                args.append(handle_file(wav_path))
                filled_audio = True
            elif "default" in p and p["default"] is not None:
                args.append(p["default"])
            else:
                blob = (str(p.get("label", ""))
                        + str(p.get("python_type", ""))).lower()
                # a Whisper Space usually has a task selector
                if "task" in blob:
                    args.append("transcribe")
                elif "lang" in blob:
                    args.append("english")
                else:
                    args.append(None)
        if not filled_audio:
            # no obvious audio param — send the file as the first arg
            args = [handle_file(wav_path)] + args[1:]
        result = cli.predict(*args, api_name=api_name)
        text = _hf_extract_text(result)
        # some ZeroGPU spaces return a JSON string with a 'text' field
        if text.strip().startswith("{") and '"text"' in text:
            try:
                import json
                text = json.loads(text).get("text", text)
            except Exception:
                pass
        return Result("hf", text=text, secs=time.time() - t0)
    except Exception as e:
        msg = str(e)[:160]
        rl = any(w in msg.lower() for w in
                 ("quota", "gpu", "rate", "exceeded", "429", "limit"))
        return Result("hf", secs=time.time() - t0,
                      error="%s: %s" % (type(e).__name__, msg),
                      rate_limited=rl)


def _hf_call_cached(cli, space):
    got = _HF_CALL.get(space)
    if got is None:
        got = _hf_resolve_call(cli)
        _HF_CALL[space] = got
    return got


def _gradio_client_available():
    try:
        import gradio_client            # noqa: F401
        return True
    except Exception:
        return False


# ── the fallback chain ────────────────────────────────────────────────
def available_providers(anonymous_ok=False):
    """Which cloud providers can be used right now, in preference order.

    LIVE path (anonymous_ok=False, the default): a provider is only
    offered if it has a real credential — Groq needs its free key, HF
    needs a token. This matters because an ANONYMOUS Hugging Face
    ZeroGPU call goes through gradio_client, whose connect/cold-start
    can block for many seconds; putting that in the middle of a
    conversation made the station look deaf. So the live assistant only
    goes to the cloud when a credential makes it fast and reliable, and
    otherwise stays on the quick local model.

    DIAGNOSTIC path (anonymous_ok=True): the A/B tool may use HF
    anonymously, because that runs offline where a slow call is fine.

    Set DOSE_STT_MODE=local to force everything back onto the Pi."""
    have = []
    gk = groq_key()
    ht = hf_token()
    hf_ok = bool(ht) or (anonymous_ok and _gradio_client_available())
    for name in provider_order():
        if name == "groq" and gk:
            have.append("groq")
        elif name == "hf" and hf_ok:
            have.append("hf")
    return have


def hf_is_authenticated():
    """True when HF requests will draw on the user's own (larger) free
    quota rather than the shared anonymous pool."""
    return bool(hf_token())


def cloud_transcribe(wav_path, order=None, language="en",
                     hf_space=None, want_all=False):
    """Try each configured free provider in order until one succeeds.

    Returns (Result, [all_results]). The first ok() Result wins. If none
    succeed, the winning Result is the last attempt (carrying its
    error), so the caller can log why cloud failed before it falls back
    to the local model. want_all=True runs EVERY provider even after one
    succeeds — used by the A/B diagnostic, never in the hot path."""
    order = order or available_providers()
    results = []
    winner = None

    # ── GUARDS, before a single byte leaves the device ───────────────
    # want_all is the A/B diagnostic, which is run deliberately by a
    # human and must not be silently skipped.
    if not want_all:
        secs = wav_duration(wav_path)
        if secs and secs < CLOUD_MIN_SECS:
            # A fragment this short is a cough, a chair, or the tail of
            # a door. It cannot contain a command, and spending a
            # request on it is how a free tier evaporates.
            return (Result("cloud", error="too short for cloud (%.2fs)"
                           % secs), [])
        if secs > CLOUD_MAX_SECS:
            return (Result("cloud",
                           error="too long for cloud (%.1fs > %.0fs)"
                           % (secs, CLOUD_MAX_SECS)), [])
        ok, why = budget().allow()
        if not ok:
            # Not an error the user should ever see — the local model
            # answers instead. It IS worth logging.
            return (Result("cloud", error="budget: " + why), [])

    send_path, is_temp = _prepare_upload(wav_path)
    try:
        for name in order:
            if name == "groq":
                res = transcribe_groq(send_path, language=language)
            elif name == "hf":
                res = transcribe_hf_space(send_path, space=hf_space)
            else:
                res = Result(name, error="unknown provider")
            results.append(res)
            if getattr(res, "rate_limited", False):
                # Back off account-wide, not just for this provider —
                # retrying harder is how a free key gets suspended.
                budget().note_rate_limited()
            elif not want_all:
                budget().record()
            if res.ok and winner is None:
                winner = res
                if not want_all:
                    break
    finally:
        if is_temp:
            try:
                os.unlink(send_path)
            except Exception:
                pass
    if winner is None:
        winner = results[-1] if results else Result(
            "cloud", error="no cloud provider configured")
    return winner, results
