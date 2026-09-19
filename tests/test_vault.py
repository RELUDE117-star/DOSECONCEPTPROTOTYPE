#!/usr/bin/env python3
"""The secrets need a PERSON at the Mac, and this proves the mechanism.

Ryan, exactly:

    "make accessing any of that private data password protected"
    "you physically and not an autumn or ai needs to manually type in
     a password on the MacBook itself in order to ever get access to
     any of the token so like if someone tried to use malicious code
     they literally couldn't get access"

What that replaces: three secrets in plain files under ~/.dose-server/
at mode 0600. That stops another USER on the machine and stops nothing
that runs as Ryan — which is everything that matters here: a script, a
downloaded binary, an agent, me. `cat` was the whole attack.

THE MECHANISM IS ONE FLAG. `security add-generic-password -T ""`
stores a keychain item with an EMPTY TRUSTED-APPLICATION LIST, and
macOS then refuses to release it to any program until a human answers
a dialog on that machine. Not a password this program checks, not a
password in this repository — the operating system holds the decision
and a keystroke on that keyboard is the only thing that opens it.

Lose the `-T ""` and everything else still appears to work, silently,
forever. That is what most of this file is about.

Run:  python3 tests/test_vault.py
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import dose_vault as V                                      # noqa: E402

SRC = open(os.path.join(ROOT, "tools", "dose_vault.py"),
           encoding="utf-8").read()
SRV = open(os.path.join(ROOT, "tools", "dose_server.py"),
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


print("\n── the one flag that does the work ─────────────────────────")
put = SRC.split("def put(")[1]
put = put[:put.index("\ndef ")]
check('put() passes -T "" — an empty trusted-application list',
      '"-T", ""' in put,
      "without this the item is released silently to whatever asks, "
      "and every other line here is decoration")
check("...and it is in the ADD call, not a comment about one",
      '"add-generic-password"' in put and put.index("add-generic-password")
      < put.index('"-T", ""'))
check("it updates in place, so protecting twice is not an error",
      '"-U"' in put)
check("an empty secret is refused rather than stored",
      "refusing to store an empty secret" in put)

print("\n── nothing here writes a secret to disk ────────────────────")
tree = ast.parse(SRC)
writes = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id == "open":
        mode = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        for kw in node.keywords or []:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value)
        if any(c in mode for c in "wax+"):
            writes.append(node.lineno)
check("no file is ever opened for writing", not writes, writes)
check("...and nothing is appended to a log here either",
      "logging" not in SRC and ".write(" not in SRC.replace(
          "sys.stdout.write(value)", ""),
      "the only write is --get printing to stdout, which is the "
      "caller asking for it on purpose")

print("\n── a fixed set of names, not a corridor ────────────────────")
check("the secrets it will touch are named here", "KNOWN = {" in SRC)
check("...and an unknown name raises rather than being created",
      'raise ValueError("unknown secret' in SRC)
for name in ("mac-token", "github-token", "server-address"):
    check("it covers %s" % name, name in V.KNOWN)
try:
    V._service("../../etc/passwd")
    bad = False
except ValueError:
    bad = True
check("a path cannot be smuggled in as a name", bad)
try:
    V._service("mac-token; rm -rf /")
    bad2 = False
except ValueError:
    bad2 = True
check("nor a shell fragment", bad2)
check("and nothing is ever passed to a shell",
      "shell=True" not in SRC,
      "subprocess with a list, always")

print("\n── asking WHETHER it exists must not prompt ────────────────")
pres = SRC.split("def present(")[1]
pres = pres[:pres.index("\ndef ")]
check("present() does not ask for the value",
      '"-w"' not in pres,
      "-w is what makes the keychain release the secret; a status "
      "page that prompts is a status page nobody can leave open")
get = SRC.split("def get(")[1]
get = get[:get.index("\ndef ")]
check("...and get() does", '"-w"' in get)
check("get() waits long enough for a person to walk over",
      "timeout=120" in get,
      "a 15-second timeout is a design that assumes nobody is coming")

print("\n── it refuses rather than pretending ───────────────────────")
check("supported() checks the platform AND the tool",
      'sys.platform == "darwin"' in SRC and "os.path.exists(SECURITY)" in SRC)
for fn in ("put", "get", "forget"):
    seg = SRC.split("def %s(" % fn)[1]
    seg = seg[:seg.index("\ndef ")]
    check("%s() gives up when it cannot protect anything" % fn,
          "if not supported():" in seg,
          "silently doing nothing here would leave a secret "
          "unprotected while the status said otherwise")
check("on a non-Mac it says so and changes nothing",
      "Nothing was changed." in SRC)

print("\n── the argv window is acknowledged, not hidden ─────────────")
# `security add-generic-password -w VALUE` puts the value in argv for
# the moment that command runs. That is real, it is unavoidable
# through this tool, and it exists only when a secret is STORED.
check("the docstring states the argv exposure plainly",
      "argv" in SRC and "HONEST LIMITS" in SRC)
check("--new generates the secret in THIS process, so nothing else "
      "ever carries it",
      "secrets.token_urlsafe(32)" in SRC
      and "put(a.new, secrets.token_urlsafe(32))" in SRC)
check("...and reading, which is the repeated operation, never "
      "touches argv",
      '"-w"' in get and "value" not in get.split("subprocess.run")[1][:200])
check("'Always Allow' defeating this is written down",
      "Always Allow" in SRC)

print("\n── it will not delete a secret of his ──────────────────────")
check("--import moves the plaintext aside rather than deleting it",
      "was-plaintext" in SRC)
check("...and says so, and leaves the deleting to him",
      "I will not delete a secret of yours" in SRC)
check("--keep-file exists for someone who wants both",
      "--keep-file" in SRC)

print("\n── the server prefers it, and still works without it ───────")
tok = SRV.split("def token(")[1]
tok = tok[:tok.index("\ndef ")]
check("the server asks the keychain before the file",
      tok.index('v.present("mac-token")') < tok.index("open(TOKEN_FILE)"),
      "a protected token that is ignored is not protected")
check("...and falls back to the file when there is nothing there",
      "open(TOKEN_FILE)" in tok,
      "a station that has not been through the protection step has "
      "to keep working")
check("...and says out loud when a person released it",
      "released from the keychain by someone at this Mac" in tok)
check("...and when the keychain refused",
      "did not release the token" in tok)
check("the vault is optional at import time",
      "def _vault(" in SRV and "return None" in SRV.split("def _vault(")[1][:400],
      "the server must not fail to start because a helper is missing")
check("--status reports WHETHER a person is required, in words",
      "token_protected" in SRV and "PLAINTEXT FILE" in SRV,
      "'token_installed: true' was equally true of a file anything "
      "could read, and it read as reassuring")

print("\n── and the secret never lands in the repository ────────────")
check("no secret value appears in this file", "token_urlsafe" in SRC
      and not any(len(w) > 40 and w.isalnum() for w in SRC.split()))
check("the vault module imports nothing that reaches the network",
      "urllib" not in SRC and "socket" not in SRC and "http" not in SRC)

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("VAULT: the tokens need a person at that Mac")
