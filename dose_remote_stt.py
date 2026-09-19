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
import socket
import time
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(APP_DIR, "dose_server.conf")

# THE MAC IS THE RECOGNISER. THE PI IS THE PARACHUTE.
#
# This file used to give up on the Mac after ONE failure and then
# refuse to try again for two minutes. That is a parachute that opens
# when the plane hits turbulence. A laptop waking from sleep, a Wi-Fi
# roam, a single dropped packet — any of them cost the next twenty
# turns, silently, and the station looked like it had never been
# paired at all.
#
# Ryan, exactly: "it should of course fall back but it shouldn't fall
# back so easily."
#
# So the policy is now:
#   * a failure is not a verdict. Three CONSECUTIVE failures are.
#   * the back-off starts small and grows (10s, 30s, 90s, 180s), and
#     any success resets it to nothing.
#   * a TIMEOUT is not the same as a REFUSAL. A timeout means the Mac
#     answered the door and is busy; a refusal means nobody is home.
#     Only refusals count toward giving up.
#   * a cheap /health probe runs between turns, so the moment the Mac
#     comes back the station knows — rather than serving local results
#     for another two minutes because of one bad second.
TIMEOUT = float(os.environ.get("DOSE_REMOTE_TIMEOUT", "6.0"))
# The first request after an idle spell may land while the Mac is
# still loading its model. Giving up on THAT is giving up on the
# thing we actually want, so the first call gets longer.
WARM_TIMEOUT = float(os.environ.get("DOSE_REMOTE_WARM_TIMEOUT", "12.0"))
# How many consecutive refusals before the Mac is written off at all.
FAILS_BEFORE_DOWN = int(os.environ.get("DOSE_REMOTE_FAILS", "3"))
# Escalating back-off, in seconds. The last value repeats.
BACKOFF = [10.0, 30.0, 90.0, 180.0]
# How stale a health answer may be before it is re-asked.
HEALTH_EVERY = float(os.environ.get("DOSE_REMOTE_HEALTH_EVERY", "20"))
# Two seconds was too tight. The device recorded a 1269 ms round trip
# and three health refusals in one run — a probe budget barely above
# the measured round trip turns ordinary Wi-Fi jitter into "the Mac is
# gone". The probe is off the critical path, so being patient here
# costs nothing and buys the Mac the benefit of the doubt, which is
# the whole point of the policy.
HEALTH_TIMEOUT = float(os.environ.get("DOSE_REMOTE_HEALTH_TIMEOUT", "5.0"))
MAX_UPLOAD = 2 * 1024 * 1024

_STATE = {"down_until": 0.0, "conf": None, "conf_at": 0.0,
          "hits": 0, "misses": 0, "last_error": "",
          "fails": 0, "backoff_at": 0, "timeouts": 0,
          "healthy_at": 0.0, "health_at": 0.0, "warmed": False,
          "last_ms": 0}


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


def _mark_ok(ms=0):
    """Anything that worked clears the whole back-off. One good answer
    is better evidence than three old bad ones."""
    _STATE["fails"] = 0
    _STATE["backoff_at"] = 0
    _STATE["down_until"] = 0.0
    _STATE["healthy_at"] = time.time()
    if ms:
        _STATE["last_ms"] = int(ms)


def _mark_soft(why):
    """It answered, but not in time. The Mac is THERE. Do not write it
    off — this turn falls back, the next one asks again."""
    _STATE["timeouts"] += 1
    _STATE["last_error"] = ("slow: " + why)[:120]


def _mark_down(why):
    """A refusal: nobody answered the door. Only these accumulate, and
    only FAILS_BEFORE_DOWN of them in a row stop us asking."""
    _STATE["misses"] += 1
    _STATE["fails"] += 1
    _STATE["last_error"] = why[:120]
    if _STATE["fails"] < FAILS_BEFORE_DOWN:
        return
    i = min(_STATE["backoff_at"], len(BACKOFF) - 1)
    _STATE["down_until"] = time.time() + BACKOFF[i]
    _STATE["backoff_at"] = min(_STATE["backoff_at"] + 1, len(BACKOFF) - 1)


def probe(force=False):
    """Ask the Mac whether it is there, cheaply, between turns.

    Without this, the only thing that could clear a back-off was the
    clock — so a Mac that came back after ten seconds went unused for
    as long as the back-off happened to be. A GET that costs two
    seconds at worst, run while nobody is speaking, means the station
    notices its recogniser returning almost immediately.

    Also warms the model: the first real request then lands on a Mac
    that has already loaded it, instead of paying for the load inside
    somebody's question.
    """
    conf = _conf()
    if conf is None:
        return False
    now = time.time()
    if not force and now - _STATE["health_at"] < HEALTH_EVERY:
        return _STATE["healthy_at"] >= _STATE["health_at"]
    _STATE["health_at"] = now
    host, port, token = conf
    req = urllib.request.Request(
        "http://%s:%d/health" % (host, port), method="GET",
        headers={"Authorization": "Bearer " + token})
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        t0 = time.time()
        with opener.open(req, timeout=HEALTH_TIMEOUT) as r:
            r.read(4096)
        _mark_ok((time.time() - t0) * 1000)
        _STATE["warmed"] = True
        return True
    except Exception as e:
        # A failed probe is information, not a verdict: it uses the
        # same three-strikes rule as a real request, so one bad probe
        # cannot take the Mac out of service.
        _mark_down("health: " + str(e))
        return False


def transcribe(wav_bytes, fast=False):
    """WAV in, text out, or "" if the Mac is not there.

    Never raises. A remote recogniser that can throw into the middle of
    a turn is a remote recogniser that can make the cabinet worse than
    having none.

    `fast` picks the OTHER ROUTE, not a parameter in the body. The
    Mac names its two models itself and this end picks one of two
    URLs; nothing about which model runs is carried in the request.

    Measured on that Mac, identical audio: /stt-fast (base.en) 0.21s,
    /stt (small.en) 0.57s, and identical output on every command
    phrase the station is actually asked. They part company on drug
    names — "metformin" came back as "medformin" from the fast one —
    which is why the fast route answers the pass that runs DURING the
    pause, and anything that does not parse is asked again properly.
    """
    conf = _conf()
    if conf is None or time.time() < _STATE["down_until"]:
        return ""
    if not wav_bytes or len(wav_bytes) > MAX_UPLOAD:
        return ""
    host, port, token = conf
    url = "http://%s:%d/%s" % (host, port, "stt-fast" if fast else "stt")
    req = urllib.request.Request(
        url, data=wav_bytes, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "audio/wav",
                 "Content-Length": str(len(wav_bytes))})
    # The first call after a cold start may land while the Mac is still
    # loading its model. Giving up on that one is giving up on exactly
    # the thing this file exists for.
    budget = TIMEOUT if _STATE["warmed"] else WARM_TIMEOUT
    t0 = time.time()
    try:
        # No proxies, ever. A proxy is a third machine, and there is no
        # third machine in this arrangement.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=budget) as r:
            body = r.read(64 * 1024)
        data = json.loads(body.decode("utf-8", "replace"))
        text = (data.get("text") or "").strip()
        _STATE["warmed"] = True
        _mark_ok((time.time() - t0) * 1000)
        if text:
            _STATE["hits"] += 1
            _STATE["last_error"] = ""
        return text
    except urllib.error.HTTPError as e:
        # The server is RUNNING — it just said no. That is a
        # configuration problem (usually a stale token), and hammering
        # it will not fix it, so it counts.
        _mark_down("server said %s" % e.code)
    except socket.timeout:
        _mark_soft("no answer in %.0fs" % budget)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), socket.timeout):
            _mark_soft("no answer in %.0fs" % budget)
        else:
            _mark_down(str(e.reason or e))
    except Exception as e:
        _mark_down(str(e))
    return ""


def stats():
    """What the heartbeat prints. 'Is the Mac doing the work' should be
    answerable by reading one line, not by trusting anybody's word."""
    now = time.time()
    return {"hits": _STATE["hits"], "misses": _STATE["misses"],
            "timeouts": _STATE["timeouts"],
            "paired": _conf() is not None,
            "fails_in_a_row": _STATE["fails"],
            "down_for": max(0, round(_STATE["down_until"] - now)),
            "healthy": bool(_STATE["healthy_at"]
                            and now - _STATE["healthy_at"] < 60),
            "last_ms": _STATE["last_ms"],
            "last_error": _STATE["last_error"]}
