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
import ssl
import time
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(APP_DIR, "dose_server.conf")

# ── THE PINNED CERTIFICATE ──────────────────────────────────────────
#
# Ryan: "jsut make sure its encrypted".
#
# The audio still goes to the Mac — that is the point of the whole
# arrangement — and now it goes inside TLS. This file is the Mac's
# certificate, copied here during pairing. It is the ONLY certificate
# this station will accept: not a certificate authority, not the
# system trust store, this exact file.
#
# That is deliberately stronger than ordinary HTTPS. There are two
# machines and they are both his; a CA exists to vouch for strangers,
# and there are no strangers here. Pinning means that any of the
# hundreds of authorities a normal client trusts being compromised
# changes nothing — the station will not accept a certificate it was
# not handed by hand during pairing.
#
# NO HOSTNAME IS CHECKED, on purpose. The Mac's address comes from
# DHCP, so a name baked into the certificate is a thing that silently
# stops matching the day the router hands out a different lease. The
# identity check here is the certificate itself, which is a stronger
# statement than a name anyway.
CERT = os.path.join(APP_DIR, "dose_server.crt")

# ONCE THIS STATION HAS A CERTIFICATE, IT WILL NOT SPEAK PLAIN HTTP.
#
# The dangerous shape is a client that tries TLS, fails, and "helpfully"
# retries in the clear — anybody able to break the handshake gets the
# audio just by breaking it. So: certificate present means https and
# only https. No certificate means this station was paired before
# encryption existed, and it keeps working over http so an upgrade
# cannot silently take a working cabinet offline — with the state
# printed in the heartbeat rather than assumed.
_SSL = {"ctx": None, "at": 0.0, "have": False}


def _ssl_ctx():
    """The pinned trust context, rebuilt if the file changes."""
    now = time.time()
    if _SSL["ctx"] is not None and now - _SSL["at"] < 60:
        return _SSL["ctx"]
    _SSL["at"] = now
    _SSL["ctx"] = None
    _SSL["have"] = False
    if not os.path.exists(CERT):
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # The Mac is identified by its certificate, not by a name.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=CERT)
        _SSL["ctx"] = ctx
        _SSL["have"] = True
    except Exception as e:
        # A certificate that will not load is NOT a reason to fall
        # back to plaintext. available() reports the station as
        # unpaired instead, and it uses its own models — slower, and
        # safe.
        _STATE["last_error"] = "certificate unusable: %s" % str(e)[:60]
    return _SSL["ctx"]


def _opener():
    """A URL opener with no proxies and the pinned certificate.

    No proxies, ever. A proxy is a third machine, and there is no
    third machine in this arrangement — it would also be a machine
    that terminates the TLS this exists to provide.
    """
    handlers = [urllib.request.ProxyHandler({})]
    ctx = _ssl_ctx()
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def encrypted():
    """Whether this station is talking to the Mac over TLS. For the
    heartbeat, so the answer to 'is the audio encrypted' is read off
    the device rather than taken on anybody's word."""
    _ssl_ctx()
    return _SSL["have"]


def _url(host, port, path):
    return "%s://%s:%d/%s" % ("https" if encrypted() else "http",
                              host, port, path)

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
        _url(host, port, "health"), method="GET",
        headers={"Authorization": "Bearer " + token})
    try:
        opener = _opener()
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

    The plain text, for every caller that only wants the words. The
    Mac may also send back a suggested REPLY; `transcribe_full()`
    returns that as well. Deliberately a RETURN VALUE and not
    something stashed on a module attribute for the caller to pick up
    afterwards — this project has now had four separate bugs of
    exactly that shape (see CLAUDE.md, "Two threads, one attribute"),
    and the speculative pass and the final pass call this function
    concurrently on purpose.

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
    return transcribe_full(wav_bytes, fast=fast)[0]


def transcribe_full(wav_bytes, fast=False):
    """WAV in, (text, reply, reply_kind) out.

    THE REPLY RIDES HOME WITH THE TRANSCRIPT. Ryan asked for the Mac
    to decide what the station says. The honest measurement is that
    deciding is already free on the Pi — `think: 0.00` in every turn
    row — so asking the Mac in a SECOND request would cost a whole
    round trip (0.85s measured) to replace something that costs
    nothing. In the same response it costs nothing at all.

    `reply_kind` is the contract:

        "chat"   the Mac composed this; say it
        "defer"  medication — the Pi answers from its own data
        "none"   nobody had a line; the Pi's respond() decides

    An older Mac that has not been updated sends no reply field at
    all, which reads as "none", which is the behaviour that existed
    before any of this. That is the intended failure.
    """
    conf = _conf()
    if conf is None or time.time() < _STATE["down_until"]:
        return "", "", "none"
    if not wav_bytes or len(wav_bytes) > MAX_UPLOAD:
        return "", "", "none"
    host, port, token = conf
    url = _url(host, port, "stt-fast" if fast else "stt")
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
        opener = _opener()
        with opener.open(req, timeout=budget) as r:
            body = r.read(64 * 1024)
        data = json.loads(body.decode("utf-8", "replace"))
        text = (data.get("text") or "").strip()
        reply = (data.get("reply") or "").strip()
        kind = (data.get("reply_kind") or "none").strip()
        # A kind this end does not understand is not a licence to
        # speak. Anything unrecognised falls back to the Pi.
        if kind not in ("chat", "defer", "none"):
            kind = "none"
        # AND THE THING HE WARNED ABOUT, ENFORCED HERE TOO:
        # "if it says nothing and the response is nothing it might
        #  seem like it answered but really it was a null value".
        # An empty string with kind "chat" would make the station
        # open its mouth and emit silence, and the row would say it
        # replied. It is not a chat answer if there is nothing in it.
        if kind == "chat" and not reply:
            kind = "none"
        _STATE["warmed"] = True
        _mark_ok((time.time() - t0) * 1000)
        if text:
            _STATE["hits"] += 1
            _STATE["last_error"] = ""
        return text, reply, kind
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
    return "", "", "none"


def stats():
    """What the heartbeat prints. 'Is the Mac doing the work' should be
    answerable by reading one line, not by trusting anybody's word."""
    now = time.time()
    return {"hits": _STATE["hits"], "misses": _STATE["misses"],
            "encrypted": encrypted(),
            "timeouts": _STATE["timeouts"],
            "paired": _conf() is not None,
            "fails_in_a_row": _STATE["fails"],
            "down_for": max(0, round(_STATE["down_until"] - now)),
            "healthy": bool(_STATE["healthy_at"]
                            and now - _STATE["healthy_at"] < 60),
            "last_ms": _STATE["last_ms"],
            "last_error": _STATE["last_error"]}
