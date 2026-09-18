#!/usr/bin/env python3
"""Ask the Mac, if the Mac is there. Otherwise do it here.

Speech recognition on this Pi has a floor: 2.12 s of pure inference on
the smallest sensible model with every core, and Whisper pads every
utterance to a thirty-second window so a short question costs what a
long one does. The M1 Pro in the same room does the same work several
sizes larger in a fraction of the time.

So when the Mac is reachable the cabinet hands over the audio and gets
back text. When it is not — asleep, closed, taken to work — nothing
changes: the local models answer exactly as they do today. That is the
whole contract, and it is the reason this file is allowed to exist at
all. A medicine cabinet that stops understanding people because a
laptop left the house would be a worse machine than the one we have.

WHAT LEAVES THE DEVICE, AND WHERE IT GOES

  * Audio. Nothing else. No hostname, no identifier, no transcript
    history, no medication data.
  * To ONE address, read from a file on this device, which must be a
    private LAN address. A public address is refused before a socket
    is opened — this is a house, not the internet.
  * Over plain HTTP on the local network, with a bearer token, to a
    server whose only route accepts a WAV and returns a string.

`tests/test_egress.py` knows about this and asserts the private-address
rule, because "it only talks to the Mac" is the kind of claim that
stops being true quietly.

CONFIGURATION lives in APP_DIR/dose_server.conf, written by the DOSE
app on the Mac, mode 0600:

    192.168.4.21:8765
    <token>

No file, no remote. That is the off switch.
"""
import ipaddress
import json
import os
import time
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(APP_DIR, "dose_server.conf")

# A turn is already spending time; this may not add to it meaningfully.
# If the Mac cannot answer within this, the local models were the
# better choice anyway.
TIMEOUT = float(os.environ.get("DOSE_REMOTE_TIMEOUT", "4.0"))
# How long a "the Mac is not there" answer is trusted before trying
# again. Without this, every turn pays a connection timeout while the
# laptop is at the office.
DOWN_FOR = float(os.environ.get("DOSE_REMOTE_DOWN_FOR", "120"))
MAX_UPLOAD = 2 * 1024 * 1024

_STATE = {"down_until": 0.0, "conf": None, "conf_at": 0.0,
          "hits": 0, "misses": 0, "last_error": ""}


def _conf():
    """Address and token, re-read occasionally so pairing takes effect
    without a restart."""
    now = time.time()
    if _STATE["conf"] is not None and now - _STATE["conf_at"] < 30:
        return _STATE["conf"]
    _STATE["conf_at"] = now
    _STATE["conf"] = None
    try:
        with open(CONF) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if len(lines) < 2:
            return None
        hostport, token = lines[0], lines[1]
        host = hostport.rsplit(":", 1)[0]
        port = int(hostport.rsplit(":", 1)[1])
        # THE RULE, ENFORCED BEFORE A SOCKET EXISTS. A private address
        # is a machine in this house. Anything else is the internet,
        # and audio from this cabinet does not go to the internet.
        ip = ipaddress.ip_address(host)
        if not ip.is_private or ip.is_multicast or ip.is_reserved:
            _STATE["last_error"] = "refused: %s is not a local address" % host
            return None
        _STATE["conf"] = (host, port, token)
    except FileNotFoundError:
        _STATE["last_error"] = "no dose_server.conf — running locally"
    except Exception as e:
        _STATE["last_error"] = "bad dose_server.conf: %s" % str(e)[:60]
    return _STATE["conf"]


def available():
    """Cheap enough to ask on every turn."""
    if time.time() < _STATE["down_until"]:
        return False
    return _conf() is not None


def _mark_down(why):
    _STATE["down_until"] = time.time() + DOWN_FOR
    _STATE["misses"] += 1
    _STATE["last_error"] = why[:120]


def transcribe(wav_bytes):
    """WAV in, text out, or "" if the Mac is not there.

    Never raises. A remote recogniser that can throw into the middle of
    a turn is a remote recogniser that can make the cabinet worse than
    having none.
    """
    conf = _conf()
    if conf is None or time.time() < _STATE["down_until"]:
        return ""
    if not wav_bytes or len(wav_bytes) > MAX_UPLOAD:
        return ""
    host, port, token = conf
    url = "http://%s:%d/stt" % (host, port)
    req = urllib.request.Request(
        url, data=wav_bytes, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "audio/wav",
                 "Content-Length": str(len(wav_bytes))})
    try:
        # No proxies, ever. A proxy is a third machine, and there is no
        # third machine in this arrangement.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=TIMEOUT) as r:
            body = r.read(64 * 1024)
        data = json.loads(body.decode("utf-8", "replace"))
        text = (data.get("text") or "").strip()
        if text:
            _STATE["hits"] += 1
            _STATE["last_error"] = ""
        return text
    except urllib.error.HTTPError as e:
        _mark_down("server said %s" % e.code)
    except Exception as e:
        _mark_down(str(e))
    return ""


def stats():
    return {"hits": _STATE["hits"], "misses": _STATE["misses"],
            "paired": _conf() is not None,
            "down_for": max(0, round(_STATE["down_until"] - time.time())),
            "last_error": _STATE["last_error"]}
