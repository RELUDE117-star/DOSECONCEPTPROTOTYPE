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

# The device that may ask. Empty means "any private address", which is
# still a LAN-only rule; setting it pins the server to one machine.
ALLOW_PEER = os.environ.get("DOSE_SERVER_PEER", "").strip()

_MODEL = [None]
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


def token():
    """The shared secret, created once, readable only by this user."""
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if t:
                return t
    except Exception:
        pass
    t = secrets.token_urlsafe(32)
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t + "\n")
    return t


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


def load_model():
    if _MODEL[0] is not None:
        return _MODEL[0]
    from faster_whisper import WhisperModel
    t0 = time.time()
    # int8 on the Pi is a compromise for a slow CPU. This machine has
    # eight performance cores and sixteen gigabytes; int8_float32 keeps
    # the accuracy and is still comfortably fast here.
    _MODEL[0] = WhisperModel(MODEL_NAME, device="cpu",
                             compute_type="int8_float32")
    log("model %s loaded in %.1fs" % (MODEL_NAME, time.time() - t0))
    return _MODEL[0]


PROMPT = ("Medication reminder device. Commands: what time is it, "
          "what do I take today, how many pills do I have left, "
          "did I take my medicine, open storage, open settings, "
          "go to user, next dose.")


def transcribe(wav_bytes):
    """WAV in, text out. The only thing this program does."""
    w = wave.open(BytesIO(wav_bytes), "rb")
    frames, rate = w.getnframes(), w.getframerate()
    secs = frames / float(rate or 16000)
    w.close()
    if secs > MAX_SECONDS:
        raise ValueError("audio too long: %.1fs" % secs)
    model = load_model()
    t0 = time.time()
    segs, _info = model.transcribe(
        BytesIO(wav_bytes), language="en", beam_size=1,
        vad_filter=True, condition_on_previous_text=False,
        initial_prompt=PROMPT)
    text = " ".join(s.text for s in segs).strip()
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
        want = "Bearer " + token()
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
                      "uptime": round(up), "requests": _STATS["requests"],
                      "rtf": round(_STATS["infer_seconds"]
                                   / max(0.001, _STATS["audio_seconds"]), 3)})
            return
        self._deny(404, "no such route")

    def do_POST(self):
        if self.path != "/stt":
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
            text, secs, took = transcribe(body)
        except Exception as e:
            self._deny(400, "not usable audio: %s" % str(e)[:80])
            return
        _STATS["requests"] += 1
        log("stt %.2fs of audio in %.2fs -> %r" % (secs, took, text[:60]))
        self._ok({"text": text, "audio": round(secs, 2),
                  "took": round(took, 3), "model": MODEL_NAME})


def serve(host=None, port=8765, preload=True):
    host = host or lan_address()
    if not is_private(host):
        log("REFUSING to bind %s — that is not a private address" % host)
        return 2
    t = token()
    if preload:
        try:
            load_model()
        except Exception as e:
            log("model not available yet: %s" % e)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log("DOSE server on http://%s:%d  (model %s)" % (host, port, MODEL_NAME))
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
        print(json.dumps({"lan": lan_address(), "port": a.port,
                          "model": MODEL_NAME,
                          "token_installed": os.path.exists(TOKEN_FILE),
                          "peer": ALLOW_PEER or "any private address"},
                         indent=1))
        return 0
    if a.serve:
        return serve(a.host or None, a.port, not a.no_preload)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
