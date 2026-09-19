#!/usr/bin/env python3
"""The audio crosses the network encrypted, and cannot quietly stop.

Ryan, in two messages a minute apart:

    "yeah for now jsut make sure its encrypted but other than that im
     okay"
    "The MAC has to hear the audio in order to do the computing so
     please make sure it stays that way"

Both at once, and they are not in tension: the Mac keeps receiving
every byte — that is why a turn takes 0.85s instead of nine — and
what changes is that nobody else on his Wi-Fi can read it.

THE FAILURE THIS FILE EXISTS TO PREVENT is not "TLS was never
added". It is TLS being added and then quietly not happening: a
client that retries in the clear when the handshake fails, a server
that serves plaintext when its certificate is missing, a downgrade
nobody notices because everything still works. Encryption that falls
back silently is encryption you cannot rely on, and it looks exactly
like encryption that works.

Run:  python3 tests/test_tls.py
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

SRV = open(os.path.join(ROOT, "tools", "dose_server.py"),
           encoding="utf-8").read()
REM = open(os.path.join(ROOT, "dose_remote_stt.py"),
           encoding="utf-8").read()
CRT = open(os.path.join(ROOT, "tools", "dose_cert.py"),
           encoding="utf-8").read()

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


print("\n── the server wraps the socket, or refuses ─────────────────")
check("it wraps the listening socket in TLS",
      "ctx.wrap_socket(httpd.socket, server_side=True)" in SRV)
check("TLS 1.2 is the floor on the server",
      "ssl.TLSVersion.TLSv1_2" in SRV)
check("a broken certificate REFUSES rather than serving plaintext",
      "REFUSING to serve in the clear" in SRV,
      "a server that falls back on error is a server an attacker "
      "downgrades by breaking the handshake")
srv_serve = SRV.split("def serve(")[1]
srv_serve = srv_serve[:srv_serve.index("\ndef ")]
check("...and that refusal RETURNS, it does not carry on",
      "return 2" in srv_serve.split("REFUSING to serve in the clear")[1]
      [:400])
check("having no certificate at all is said out loud",
      "NO CERTIFICATE — serving plain HTTP" in SRV,
      "an unencrypted station must never be indistinguishable from "
      "an encrypted one")
check("--status reports it in words, not a boolean alone",
      '"encryption"' in SRV and "NOT ENCRYPTED" in SRV)

print("\n── the station pins ONE certificate ────────────────────────")
check("it verifies, rather than accepting anything",
      "ssl.CERT_REQUIRED" in REM)
check("the trust anchor is the pinned file, not the system store",
      "load_verify_locations(cafile=CERT)" in REM,
      "loading a CA bundle here would trust hundreds of authorities "
      "to vouch for a laptop in his kitchen")
check("TLS 1.2 is the floor on the station too",
      "ssl.TLSVersion.TLSv1_2" in REM)
check("the hostname check is off DELIBERATELY and explained",
      "check_hostname = False" in REM and "DHCP" in REM,
      "off with no reason written down is indistinguishable from off "
      "by accident")
check("an unusable certificate does NOT fall back to plaintext",
      "certificate unusable" in REM
      and "is NOT a reason to fall" in REM)

print("\n── and it cannot silently downgrade ────────────────────────")
# The whole point. Every URL must be built by the one function that
# decides the scheme; a hardcoded http:// anywhere is a hole.
hard = [ln.strip() for ln in REM.splitlines()
        if '"http://' in ln and not ln.strip().startswith("#")]
check("no request URL is hardcoded to http://", not hard, hard)
check("one function decides the scheme", "def _url(" in REM)
check("...and it picks https whenever a certificate is loaded",
      '"https" if encrypted() else "http"' in REM)
check("the opener carries the pinned context",
      "HTTPSHandler(context=ctx)" in REM)
check("...and still refuses proxies",
      "ProxyHandler({})" in REM,
      "a proxy terminates the TLS this exists to provide")
# Both request paths, not just the one I was looking at.
for fn in ("transcribe_full", "probe"):
    seg = REM.split("def %s(" % fn)[1]
    seg = seg[:seg.index("\ndef ")]
    check("%s() builds its URL through _url()" % fn, "_url(" in seg)
    check("...and opens through _opener()" % (), "_opener()" in seg,
          fn)

print("\n── the device reports its own state ────────────────────────")
check("stats() says whether the link is encrypted",
      '"encrypted": encrypted()' in REM,
      "'is the audio encrypted' must be answerable by reading the "
      "device, not by trusting me")

print("\n── the key is made outside the network-facing program ──────")
# THE AST, NOT THE TEXT. My first version of this check grepped for
# "subprocess" in the source — and failed, on a file that runs no
# program, because the COMMENT explaining that the certificate is
# generated elsewhere contains the word. That is the ninth
# self-matching pattern in this project, written into a file whose own
# sibling says "a docstring is not code, and only the AST knows the
# difference". Ninth.
_srv_ast = ast.parse(SRV)
_srv_imports = set()
for _n in ast.walk(_srv_ast):
    if isinstance(_n, ast.Import):
        _srv_imports |= {a.name.split(".")[0] for a in _n.names}
    elif isinstance(_n, ast.ImportFrom):
        _srv_imports.add((_n.module or "").split(".")[0])
_srv_calls = set()
for _n in ast.walk(_srv_ast):
    if isinstance(_n, ast.Call):
        _f = _n.func
        _parts = []
        while isinstance(_f, ast.Attribute):
            _parts.append(_f.attr)
            _f = _f.value
        if isinstance(_f, ast.Name):
            _parts.append(_f.id)
        _srv_calls.add(".".join(reversed(_parts)))
check("dose_server.py imports nothing that runs a program",
      not ({"subprocess", "pty", "ctypes"} & _srv_imports),
      sorted(_srv_imports))
check("...and calls nothing that runs a program",
      not ({"os.system", "os.popen", "subprocess.run", "eval", "exec"}
           & _srv_calls),
      "the one rule this file has never bent")
check("the certificate tool is what shells out",
      "subprocess.run" in CRT)
check("...and it only ever exports the PUBLIC half",
      "CERT_FILE" in CRT.split("def export(")[1][:300]
      and "KEY_FILE" not in CRT.split("def export(")[1][:300])
check("the private key is written 0600",
      "os.chmod(KEY_FILE, 0o600)" in CRT)
check("replacing a certificate is refused by default",
      "Not replacing it" in CRT and "--force" in CRT,
      "a replaced certificate breaks every station that pinned the "
      "old one, instantly and silently")
check("the fingerprint is computed from the file, both ends alike",
      "hashlib.sha256(der)" in CRT)
check("the certificate tool reaches no network",
      not any(w in CRT for w in ("urllib", "socket", "http")))

print("\n── AND THE LOG REDACTION, AGAINST OLD FORMATS ─────────────")
# It reported "redacted 102 transcript lines" and 261 were still
# there. The pattern matched the two shapes in the CURRENT source —
# `stt[model] ... ->` and the prompt echo — and an older build had
# written thousands of lines with no brackets at all:
#
#     stt 2.50s of audio in 0.68s -> 'What time is it?'
#
# A redaction that only knows today's format is not a redaction, and
# the material it exists for is precisely the log that accumulated
# across builds. So this checks the PATTERN against formats this
# project has actually written, including ones it no longer writes.
import importlib.util                                      # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "_srv_for_redact", os.path.join(ROOT, "tools", "dose_server.py"))
_srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_srv)
CASES = [
    # (must be redacted?, line)
    (True,  "13:18:50  stt 2.50s of audio in 0.68s -> 'What time is it?'"),
    (True,  "10:00:00  stt[base.en] 1.0s in 0.2s -> \"Did I take my aspirin?\""),
    (True,  "10:00:00  dropped a prompt echo: 'Medication reminder device.'"),
    (False, "21:54:50  stt[distil-small.en] 0.9s in 0.7s -> (4 words, 16 chars)"),
    (False, "10:00:00  stt[small.en] 1.0s in 0.2s -> (nothing)  reply=none/x"),
    (False, "10:00:00  DOSE server on https://192.168.4.21:8765  (/stt small.en)"),
    (False, "10:00:00  token released from the keychain by someone at this Mac"),
]
for want, line in CASES:
    got = bool(_srv.TRANSCRIPT_LINE.match(line))
    check("%s: %s" % ("redacts" if want else "keeps  ", line[26:62]),
          got == want,
          "got %s" % ("redact" if got else "keep"))
check("the original is moved aside, never deleted",
      "with-transcripts" in SRV and "I have not deleted it" in SRV,
      "a redaction he cannot check is not a redaction")

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("TLS: the Mac hears it, nobody else does")
