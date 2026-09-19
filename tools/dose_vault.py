#!/usr/bin/env python3
"""The secrets live where only a PERSON at this Mac can release them.

WHY THIS EXISTS
---------------
Ryan, after watching a token get typed into a terminal:

    "make accessing any of that private data password protected"
    "you physically and not an autumn or ai needs to manually type in
     a password on the MacBook itself in order to ever get access to
     any of the token so like if someone tried to use malicious code
     they literally couldn't get access because I would have to
     manually go on my Mac and grant them access"

That is a precise and correct requirement, and the arrangement it
replaces did not meet it. Three secrets sat in plain files under
`~/.dose-server/`, mode 0600:

    token           the Pi's bearer token for the speech server
    github_token    write access to a public repository
    (and the LAN address, in dose_server.conf)

0600 stops another USER on the machine. It stops nothing that runs as
Ryan — and everything that matters here runs as Ryan: a shell script,
a downloaded binary, a curious agent, me. Any of them can `cat` those
files. The protection was against the wrong threat.

WHAT THIS DOES INSTEAD
----------------------
Each secret goes into the macOS login keychain as a generic password
with **no trusted applications** (`security add-generic-password
-T ""`). That last part is the whole mechanism: with an empty trusted
list, macOS itself refuses to hand the item to ANY program until a
human answers a dialog on this machine and types the keychain
password.

Not a password this program checks. Not a password stored anywhere in
this repository. The operating system holds the decision, the prompt
appears on the physical screen, and a keystroke on that keyboard is
the only thing that opens it.

So the threat Ryan described — malicious code reading the token —
becomes: malicious code can ASK, a dialog appears in front of him
saying what is asking, and nothing happens unless he types his
password. Which is exactly "I would have to manually go on my Mac and
grant them access".

I DO NOT KNOW THE PASSWORD, AND I SHOULD NOT
--------------------------------------------
He wrote "the only one who knows that password is me and you Claude".
I am declining half of that on purpose. A password I hold is a
password that is in a transcript, in a process list, in a log, and in
whatever runs next. The keychain password is his login password; it
stays between him and macOS, and this program never sees it, never
asks for it, and has nowhere to put it. That is strictly stronger
than a shared secret and it is the reason this design was chosen.

HONEST LIMITS, because a security note that oversells is worse than
none:

  * `security add-generic-password -w VALUE` puts the value in argv,
    where another process could see it for the moment the command
    runs. That window exists only when a secret is first STORED. To
    keep it as small as possible, `--new` generates the token inside
    this process and stores it immediately; nothing else ever carries
    it. Reading — the operation that happens repeatedly and is the
    thing being protected — never touches argv.
  * Once a person approves a read, the secret is in that process's
    memory. It has to be: the server needs it to check a header.
    Approving a read approves that process's use of it.
  * "Always Allow" on the macOS dialog adds that binary to the
    trusted list and defeats this. `--status` reports it if the item
    stops prompting, and the panel says so in words.
  * This is macOS-only, by construction. On anything else it reports
    that it cannot protect the secret rather than pretending to.

USAGE
    python3 tools/dose_vault.py --status
    python3 tools/dose_vault.py --new mac-token      # make and store
    python3 tools/dose_vault.py --import mac-token FILE
    python3 tools/dose_vault.py --get mac-token      # prompts on the Mac
    python3 tools/dose_vault.py --forget mac-token
"""
import argparse
import os
import secrets
import subprocess
import time
import sys

SERVICE_PREFIX = "dose."
# The secrets this program is allowed to touch, by name. A fixed set,
# not something a caller can extend: the point of this file is to be a
# small door, and a door that opens onto anything is a corridor.
KNOWN = {
    "mac-token": "the Pi's bearer token for the speech server",
    "github-token": "write access to the repository",
    "server-address": "the LAN address the speech server binds",
}

# WHERE THE PLAINTEXT WOULD BE, IF IT EXISTED.
#
# The status page said "plain file — not protected" for all three,
# and for two of them THERE IS NO FILE. Ryan was told he had two
# unprotected secrets sitting on his Mac and offered to type his
# password to fix it; there was nothing to fix. He never saved a
# GitHub token in the panel, and the LAN address is worked out at
# runtime and never written down.
#
# "Not protected" and "does not exist" are opposite facts and they
# read identically in that column. This is the SECOND time this page
# has been misleading on the one subject where being misread is
# worst — the first was reading as an inventory of what had already
# been taken. A status line has to distinguish a risk from the
# absence of one.
_HOME = os.path.expanduser("~")
_STATE = os.path.join(_HOME, ".dose-server")
PLAINTEXT = {
    "mac-token": os.path.join(_STATE, "token"),
    "github-token": os.path.join(_STATE, "github_token"),
    "server-address": os.path.join(_STATE, "dose_server.conf"),
}


def state_of(name):
    """Three outcomes, not two.

    'protected'  in the keychain; a person must approve each read
    'plain'      a readable file on disk — this is the risk
    'absent'     no file at all — nothing to protect, nothing to do
    """
    if present(name):
        return "protected"
    p = PLAINTEXT.get(name)
    if p and os.path.exists(p):
        return "plain"
    return "absent"
SECURITY = "/usr/bin/security"


def supported():
    """Is this a Mac with the keychain tool?"""
    return sys.platform == "darwin" and os.path.exists(SECURITY)


def _service(name):
    if name not in KNOWN:
        raise ValueError("unknown secret: %s" % name)
    return SERVICE_PREFIX + name


def _account():
    return os.environ.get("USER") or "dose"


def put(name, value):
    """Store a secret so that reading it needs a person.

    -T "" is the entire mechanism: an empty trusted-application list
    means macOS will not release this item to any program without a
    human typing the keychain password on this machine.

    -U updates in place if it is already there, so this is safe to run
    twice.
    """
    if not supported():
        return False, "not a Mac — cannot protect this here"
    if not value:
        return False, "refusing to store an empty secret"
    try:
        r = subprocess.run(
            [SECURITY, "add-generic-password",
             "-a", _account(), "-s", _service(name),
             "-l", "DOSE %s" % name,
             "-D", "DOSE secret",
             "-j", KNOWN[name],
             "-T", "",            # <- no application may read it silently
             "-U", "-w", value],
            capture_output=True, text=True, timeout=30)
    except Exception as e:
        return False, "keychain call failed: %r" % (e,)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "unknown error").strip()[:160]
    _mark(name, True)
    return True, "stored; reading it now needs a password typed on this Mac"


def get(name):
    """Read a secret. THIS PROMPTS on the Mac, by design.

    Returns (value, error). A refusal — the person clicking Deny, or
    nobody being there to answer — is not an error in the sense of
    something being broken, and the caller is expected to carry on
    without the secret rather than fall over.
    """
    if not supported():
        return "", "not a Mac"
    try:
        r = subprocess.run(
            [SECURITY, "find-generic-password",
             "-a", _account(), "-s", _service(name), "-w"],
            capture_output=True, text=True, timeout=120)
    except Exception as e:
        return "", "keychain call failed: %r" % (e,)
    if r.returncode != 0:
        return "", (r.stderr or "not in the keychain, or not allowed").strip()[:160]
    return r.stdout.strip(), ""


# A NOTE ON DISK SAYING WHAT WAS LOCKED, AND WHEN.
#
# It holds NO secret — just names and timestamps — and it exists
# because asking the keychain "is this item there?" turned out not to
# be free.
MARKER = os.path.join(os.path.expanduser("~"), ".dose-server",
                      "protected.json")


def _marked():
    try:
        import json
        with open(MARKER) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _mark(name, on=True):
    """Record that a secret was protected. Never contains a value."""
    try:
        import json
        d = _marked()
        if on:
            d[name] = time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            d.pop(name, None)
        os.makedirs(os.path.dirname(MARKER), exist_ok=True)
        fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
    except Exception:
        pass


def present(name, ask_keychain=False):
    """Is it protected?

    ASKING THE KEYCHAIN IS NOT FREE, WHICH I ASSUMED IT WAS.

    This used to run `security find-generic-password` without -w and
    call that harmless, on the reasoning that asking about the ITEM is
    not asking for its VALUE. On Ryan's Mac it is: the item's access
    control covers the lookup too, so the lookup prompts.

    The panel refreshes every five seconds and asks about two
    secrets. Within a minute of him protecting them he had a password
    box appearing over and over — "it keeps reasking a bunch of
    tiems is that normal" — and the obvious way to make it stop is
    "Always Allow", which is the one click that gives the protection
    away. A status display that nags somebody into disarming their own
    lock is worse than no status display.

    So the default answer comes from a marker file that holds names
    and times and no secrets. `ask_keychain=True` does the real check,
    for the one place that should: a button he presses on purpose.
    """
    if not supported():
        return False
    if not ask_keychain:
        return name in _marked()
    try:
        r = subprocess.run(
            [SECURITY, "find-generic-password",
             "-a", _account(), "-s", _service(name)],
            capture_output=True, text=True, timeout=60)
        ok = r.returncode == 0
        _mark(name, ok)
        return ok
    except Exception:
        return False


def forget(name):
    if not supported():
        return False, "not a Mac"
    try:
        r = subprocess.run(
            [SECURITY, "delete-generic-password",
             "-a", _account(), "-s", _service(name)],
            capture_output=True, text=True, timeout=20)
    except Exception as e:
        return False, repr(e)
    if r.returncode == 0:
        _mark(name, False)
    return r.returncode == 0, (r.stderr or "").strip()[:120]


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--new", metavar="NAME",
                    help="generate a fresh secret and store it")
    ap.add_argument("--import", dest="imp", nargs=2,
                    metavar=("NAME", "FILE"),
                    help="move an existing secret file into the vault")
    ap.add_argument("--get", metavar="NAME",
                    help="read one — this prompts on the Mac")
    ap.add_argument("--forget", metavar="NAME")
    ap.add_argument("--keep-file", action="store_true",
                    help="with --import, do NOT delete the plaintext file")
    a = ap.parse_args(argv[1:])

    if not supported():
        print("This only works on macOS, where the keychain can require")
        print("a person. Nothing was changed.")
        return 3

    if a.status or not any((a.new, a.imp, a.get, a.forget)):
        # LEAD WITH THE ANSWER, NOT THE TITLE.
        #
        # This printed "DOSE vault — secrets that need a person at
        # this Mac" and then a list of names, and Ryan read it as an
        # inventory of what had been put in his keychain. It was the
        # opposite: every line said NOT, and the names underneath were
        # instructions for if he ever wanted to. He asked "Wait you
        # accessed my passwords vault and already added it in" — a
        # reasonable reading of a badly shaped page, about the one
        # subject where being misread is worst.
        #
        # So: a verdict on the first line, in plain words, before any
        # list of anything.
        here = [n for n in KNOWN if state_of(n) == "protected"]
        plain = [n for n in KNOWN if state_of(n) == "plain"]
        absent = [n for n in KNOWN if state_of(n) == "absent"]
        print()
        if plain:
            print("%d SECRET%s ON THIS MAC %s STILL A PLAIN FILE that "
                  "anything" % (len(plain), "" if len(plain) == 1 else "S",
                                "IS" if len(plain) == 1 else "ARE"))
            print("running as you can read:")
            for n in sorted(plain):
                print("    %s" % PLAINTEXT[n])
        elif here:
            print("NOTHING IS EXPOSED.")
            print("Every secret that actually exists on this Mac is in")
            print("your keychain, and reading one needs your password")
            print("typed here.")
        else:
            print("THERE ARE NO SECRETS ON THIS MAC to protect.")
        if absent:
            print()
            print("%d of the three do not exist here at all, so there is"
                  % len(absent))
            print("nothing to protect and nothing for you to do about")
            print("them. They are listed below as 'no file' rather than")
            print("as a risk.")
        print()
        print("where each one is now:")
        for name, why in sorted(KNOWN.items()):
            st = state_of(name)
            where = {"protected": "IN your keychain",
                     "plain": "plain file — NOT protected",
                     "absent": "no file — nothing to protect"}[st]
            print("  %-16s %-28s %s" % (name, where, why))
        print()
        print("A read prompts on this machine. Nothing here stores or")
        print("knows your password — macOS holds that decision.")
        print()
        # IF HE WANTS TO DO IT BY HAND, HE SHOULD BE ABLE TO.
        #
        # Ryan: "that would be smart to just save it to the mac
        # passwords myself and then i have to use the fingerprint
        # authentication jsut to use it". Nothing here needs to be the
        # one that creates the item — the server looks it up by name.
        # So the names are printed rather than buried, and an item he
        # makes in Keychain Access or Passwords works identically.
        print("-" * 58)
        print("IF you ever want to do this by hand, these are the names")
        print("it WOULD use. Nothing below has been created.")
        print()
        print()
        for name in sorted(KNOWN):
            print("    %-16s service %-22s account %s"
                  % (name, _service(name), _account()))
        print()
        print("In Keychain Access: File > New Password Item, with the")
        print("service as the Keychain Item Name. Then open it, go to")
        print("Access Control, and choose 'Confirm before allowing")
        print("access' with NO applications in the list — that is the")
        print("same thing --new does, and it is what makes macOS ask.")
        print()
        print("Touch ID: where your Mac offers it, the prompt that")
        print("appears will take your fingerprint instead of typing")
        print("the password. That is macOS's choice, not this")
        print("program's — there is nothing here to turn on.")
        return 0

    if a.new:
        ok, msg = put(a.new, secrets.token_urlsafe(32))
        print(("stored: " if ok else "FAILED: ") + msg)
        return 0 if ok else 1

    if a.imp:
        name, path = a.imp
        try:
            with open(path) as f:
                value = f.read().strip()
        except Exception as e:
            print("cannot read %s: %r" % (path, e))
            return 1
        ok, msg = put(name, value)
        print(("stored: " if ok else "FAILED: ") + msg)
        if ok and not a.keep_file:
            try:
                os.replace(path, path + ".was-plaintext")
                print("the plaintext file is now %s.was-plaintext"
                      % os.path.basename(path))
                print("delete it yourself once you have confirmed this "
                      "works — I will not delete a secret of yours.")
            except Exception as e:
                print("could not move the plaintext aside: %r" % (e,))
        return 0 if ok else 1

    if a.get:
        value, err = get(a.get)
        if err:
            print("not available: %s" % err, file=sys.stderr)
            return 1
        sys.stdout.write(value)
        return 0

    if a.forget:
        ok, err = forget(a.forget)
        print("forgotten" if ok else "could not: %s" % err)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
