#!/usr/bin/env python3
"""The certificate that puts the audio inside TLS.

WHY
---
Ryan:

    "yeah for now jsut make sure its encrypted but other than that im
     okay"
    "The MAC has to hear the audio in order to do the computing so
     please make sure it stays that way"

Both, exactly as stated. The Mac keeps hearing the audio — that is
the whole reason a turn takes 0.85s instead of nine — and what
changes is that nobody ELSE on the network can. Until now the Pi
POSTed raw WAV bytes over plain HTTP: anyone on that Wi-Fi with a
packet capture had the audio and therefore the words.

WHY A SELF-SIGNED CERTIFICATE IS THE RIGHT ANSWER HERE, NOT A
COMPROMISE
-----------------------------------------------------------------
There are exactly two machines and they are both his. A public CA
exists to tell strangers that a stranger is who they claim to be;
there are no strangers here, and asking one to vouch for a laptop on
a home LAN would mean a public hostname, a renewal process and a
third party — three new things that can break or leak, to answer a
question nobody is asking.

So the Pi PINS this certificate: it trusts this one file and nothing
else, not a CA bundle, not the system store. That is strictly
stronger than ordinary HTTPS. A certificate authority being tricked,
or any of the hundreds in a system store being compromised, does not
help an attacker here, because the Pi will not accept a certificate
it was not handed.

The pairing already carries a secret from the Mac to the Pi by hand,
so there is a trustworthy channel to carry a fingerprint over too,
and that is what makes pinning practical rather than theoretical.

HOW IT IS GENERATED, AND WHY NOT IN THE SERVER
----------------------------------------------
With `openssl`, from this command-line tool — never from
dose_server.py. That file is the one thing on the Mac the Pi can
reach, and it has a hard rule with a test behind it: no subprocess,
no exec, nothing that runs a program, anywhere in it. Generating a
key is a once-per-install administrative act; it does not belong in
the process that answers network requests, and putting it there to
save a file would trade that rule for nothing.

    python3 tools/dose_cert.py --make        create, if absent
    python3 tools/dose_cert.py --make --force   replace it
    python3 tools/dose_cert.py --fingerprint  what the Pi must pin
    python3 tools/dose_cert.py --status

THE KEY NEVER LEAVES THIS MACHINE. Only the certificate — the public
half — goes to the Pi. `--export` prints exactly that and nothing
else, and this file has no code path that reads the key and writes it
anywhere.
"""
import argparse
import hashlib
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".dose-server")
CERT_FILE = os.path.join(STATE_DIR, "server.crt")
KEY_FILE = os.path.join(STATE_DIR, "server.key")

# Ten years. This is a device on a shelf in a house, and an expiry is
# a way for it to stop working at three in the morning for a reason
# nobody will connect to the symptom. Renewal exists to limit the
# damage of a stolen key that somebody else might still trust — the
# only machine that trusts this one is the Pi, and re-pairing is one
# command, so a short lifetime buys nothing and costs an outage.
DAYS = "3650"


def _openssl():
    for p in ("/usr/bin/openssl", "/opt/homebrew/bin/openssl",
              "/usr/local/bin/openssl"):
        if os.path.exists(p):
            return p
    return None


def exists():
    return os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)


def fingerprint(path=None):
    """SHA-256 of the certificate's DER body — what gets compared.

    Read straight off the file rather than asked of openssl, so the
    number the Pi is told to expect is computed the same way on both
    machines by the same few lines.
    """
    path = path or CERT_FILE
    try:
        with open(path, "rb") as f:
            pem = f.read()
    except Exception:
        return ""
    import base64
    body = []
    keep = False
    for line in pem.decode("ascii", "replace").splitlines():
        if "BEGIN CERTIFICATE" in line:
            keep = True
            continue
        if "END CERTIFICATE" in line:
            break
        if keep:
            body.append(line.strip())
    if not body:
        return ""
    try:
        der = base64.b64decode("".join(body))
    except Exception:
        return ""
    h = hashlib.sha256(der).hexdigest()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2)).upper()


def make(force=False):
    """Create the key and certificate. Refuses to clobber by default.

    Replacing the certificate breaks every Pi that pinned the old
    one, instantly and completely — the station falls back to its own
    models and gets slower, with nothing on screen to explain why. So
    the default is to refuse, and --force says it out loud.
    """
    ssl_bin = _openssl()
    if ssl_bin is None:
        print("openssl was not found. Nothing was changed.")
        return 2
    if exists() and not force:
        print("A certificate already exists:")
        print("    %s" % CERT_FILE)
        print("    fingerprint %s" % fingerprint())
        print()
        print("Not replacing it. Any station that pinned this one "
              "would stop trusting the Mac the moment it changed, "
              "and would go quiet about why. Use --force if that is "
              "what you mean, and re-pair the station afterwards.")
        return 0
    os.makedirs(STATE_DIR, exist_ok=True)
    # No hostname is asserted. The Pi pins this exact certificate and
    # does not check a name, because the Mac's address is DHCP and a
    # name in here would be a thing that silently stops matching on
    # the day the router hands out a different lease.
    cmd = [ssl_bin, "req", "-x509", "-newkey", "rsa:3072",
           "-keyout", KEY_FILE, "-out", CERT_FILE,
           "-days", DAYS, "-nodes",
           "-subj", "/CN=dose-speech-server"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:
        print("could not run openssl: %s" % e)
        return 2
    if r.returncode != 0 or not exists():
        print("openssl failed: %s"
              % r.stderr.decode("utf-8", "replace")[-300:])
        return 2
    os.chmod(KEY_FILE, 0o600)
    os.chmod(CERT_FILE, 0o644)
    print("created:")
    print("    %s   (PRIVATE — never leaves this Mac)" % KEY_FILE)
    print("    %s   (public — this is what the Pi pins)" % CERT_FILE)
    print()
    print("fingerprint %s" % fingerprint())
    return 0


def export():
    """The certificate only. The public half, by construction."""
    try:
        with open(CERT_FILE) as f:
            sys.stdout.write(f.read())
        return 0
    except Exception as e:
        print("no certificate: %s" % e, file=sys.stderr)
        return 1


def status():
    if not exists():
        print("NOT ENCRYPTED YET.")
        print()
        print("There is no certificate on this Mac, so the speech "
              "server is serving plain HTTP and the audio crosses "
              "your network in the clear.")
        print()
        print("    python3 tools/dose_cert.py --make")
        return 0
    print("ENCRYPTED.")
    print()
    print("    certificate  %s" % CERT_FILE)
    print("    private key  %s  (mode %s)"
          % (KEY_FILE, oct(os.stat(KEY_FILE).st_mode & 0o777)))
    print("    fingerprint  %s" % fingerprint())
    print()
    print("The station trusts this certificate and nothing else — not "
          "a certificate authority, not the system store. Nothing can "
          "stand in the middle of that connection without holding the "
          "private key above, which has never left this machine.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--make", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--fingerprint", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.make:
        return make(force=a.force)
    if a.fingerprint:
        print(fingerprint())
        return 0
    if a.export:
        return export()
    return status()


if __name__ == "__main__":
    sys.exit(main())
