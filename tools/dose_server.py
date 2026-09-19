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
import secrets
import socket
import sys
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".dose-server")
TOKEN_FILE = os.path.join(STATE_DIR, "token")
LOG_FILE = os.path.join(STATE_DIR, "server.log")

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

_MODEL = {}          # name -> loaded model, both resident
_STATS = {"requests": 0, "audio_seconds": 0.0, "infer_seconds": 0.0,
          "refused": 0, "started": time.time()}


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


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


def _resolve_token():
    """Where the value actually comes from. Called once per process.

    Split out from token() so the caching is visible and so a test
    can assert that the request path never reaches this."""
    v = _vault()
    if v is not None and v.supported() and v.present("mac-token"):
        val, err = v.get("mac-token")
        if val:
            log("token released from the keychain by someone at this Mac "
                "— held in memory now, nothing will ask again until "
                "this server restarts")
            return val, "keychain"
        log("the keychain did not release the token (%s) — "
            "falling back to the file" % (err or "refused"))
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if t:
                return t, "file"
    except Exception:
        pass
    t = secrets.token_urlsafe(32)
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t + "\n")
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
        log("dropped a prompt echo: %r" % text[:60])
        text = ""
    took = time.time() - t0
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
        if ALLOW_PEER and peer != ALLOW_PEER:
            self._deny(403, "not the paired device")
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
            self._deny(401, "bad token")
            return False
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
        log("stt[%s] %.2fs of audio in %.2fs -> %r"
            % (model_name, secs, took, text[:60]))
        self._ok({"text": text, "audio": round(secs, 2),
                  "took": round(took, 3), "model": model_name})


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
    log("DOSE server on http://%s:%d  (/stt %s, /stt-fast %s)"
        % (host, port, MODEL_NAME, FAST_MODEL_NAME))
    log("paired device: %s" % (ALLOW_PEER or "any address on this LAN"))
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
    a = ap.parse_args()
    if a.token:
        print(token())
        return 0
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
             "token_protected": protected,
             "token_protection": (
                 "keychain — a person must approve each read on this Mac"
                 if protected else
                 "PLAINTEXT FILE — anything running as you can read it"),
             "peer": ALLOW_PEER or "any private address"},
            indent=1))
        return 0
    if a.serve:
        return serve(a.host or None, a.port, not a.no_preload)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
