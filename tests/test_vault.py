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
import re
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
# AND THAT WAS NOT ENOUGH. Leaving out -w does not make the call
# free: on Ryan's Mac the item's access control covers the LOOKUP
# too. The panel refreshes every five seconds and asks about two
# secrets, so within a minute of protecting them he had a password
# box appearing over and over — "it keeps reasking a bunch of tiems
# is that normal" — and the obvious way to stop that is "Always
# Allow", the single click that gives the protection away.
check("...and by default does not call the keychain AT ALL",
      "if not ask_keychain:" in pres and "return name in _marked()" in pres,
      "a status display that nags somebody into disarming their own "
      "lock is worse than no status display")
check("the real check is available, but only when asked for",
      "ask_keychain=False" in pres)
# Look at the CODE, not the docstring — which says the word "value"
# in the course of promising not to hold one.
_mk = SRC.split("def _mark(")[1]
_mk = _mk[_mk.index('"""', _mk.index('"""') + 3):_mk.index("\ndef ")]
check("the marker holds names and times, never a value",
      "Never contains a value" in SRC and "value" not in _mk,
      _mk[:120])
check("it is written when a secret is protected",
      "_mark(name, True)" in SRC)
check("...and cleared when one is forgotten",
      "_mark(name, False)" in SRC)
PANEL = open(os.path.join(ROOT, "tools", "dose_panel.py"),
             errors="ignore").read()
check("the panel's five-second refresh does not ask the keychain",
      "REFRESHES EVERY FIVE SECONDS" in PANEL
      and "ask_keychain=True" not in PANEL.split("def status(")[1][:1800])
check("...and the button that DOES ask says so on its face",
      "Check for real (will ask)" in PANEL)
check("...and warns that a Deny reads as unprotected",
      "the check being denied" in PANEL,
      "otherwise a denied check looks like the secret vanished")
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

print("\n── the status page cannot be read as an inventory ──────────")
# It printed a title, then a list of names, and Ryan read it as what
# had been PUT in his keychain. It was the opposite — every line said
# NOT, and the names were instructions. "Wait you accessed my
# passwords vault and already added it in" is a fair reading of a
# badly shaped page, on the one subject where being misread is worst.
check("the verdict comes before any list",
      SRC.index("NOTHING IS PROTECTED YET.")
      < SRC.index("where each one is now:"))
check("...and says it in words, not a state name",
      "still ordinary files that anything" in SRC
      and "Nothing has been added to" in SRC)
check("a partly-done state is named rather than implied",
      "PARTLY PROTECTED" in SRC)
check("the names are labelled as a thing that has NOT happened",
      "Nothing below has been created." in SRC)
check("an unprotected item says so on its own line",
      "plain file — not protected" in SRC,
      "'NOT in the keychain' in a column reads as a category, not a "
      "warning")

print("\n── it will not delete a secret of his ──────────────────────")
check("--import moves the plaintext aside rather than deleting it",
      "was-plaintext" in SRC)
check("...and says so, and leaves the deleting to him",
      "I will not delete a secret of yours" in SRC)
check("--keep-file exists for someone who wants both",
      "--keep-file" in SRC)

print("\n── the server prefers it, and still works without it ───────")
# THE RESOLVE, NOT THE ACCESSOR. token() used to hold this body and
# was called from the request handler, so Ryan got a password dialog
# every few seconds — the Pi polls /health while idle. The body now
# lives in _resolve_token(), called once at startup; token() is a
# cache in front of it and token_now() is what a request may use.
tok = SRV.split("def _resolve_token(")[1]
tok = tok[:tok.index("\ndef ")]
# THE WHOLE FUNCTION, not a fixed slice of it. This was [:900] and
# the peer pin pushed token_now() past character 900, so the check
# started failing on code that was correct. CLAUDE.md already records
# a test that sliced a function body as a fixed 3000 characters and
# broke when a docstring grew. Same mistake, same file.
_ALLOWED_CODE = SRV.split("def _allowed(")[1]
_ALLOWED_CODE = _ALLOWED_CODE[:_ALLOWED_CODE.index("\n    def ")]
_ALLOWED_CODE = "\n".join(
    ln for ln in _ALLOWED_CODE.splitlines()
    if not ln.lstrip().startswith("#"))
check("asking the keychain happens in ONE place, called once",
      "def _resolve_token(" in SRV and "def token_now(" in SRV,
      "a keychain read on the request path is a password prompt per "
      "request")
check("...and the request path uses the accessor that cannot ask",
      # Two traps in one line, both already in CLAUDE.md. "token()"
      # is a SUBSTRING of "token_now()", so the word has to be
      # matched; and the comment above that line contains the literal
      # words "NEVER token()", so the comments have to come out
      # first. Grepping source text that includes its own commentary
      # is how a test asserts the opposite of what it means.
      "token_now()" in _ALLOWED_CODE
      and not re.search(r"(?<!_now)\btoken\(\)", _ALLOWED_CODE))
check("the server asks the keychain before the file",
      tok.index('v.present("mac-token")') < tok.index("open(TOKEN_FILE)"),
      "a protected token that is ignored is not protected")
check("...and falls back to the file when there is nothing there",
      "open(TOKEN_FILE)" in tok,
      "a station that has not been through the protection step has "
      "to keep working")
# Case-insensitive: the refusal message is now shouted, because two
# hundred requests went by while it whispered.
tok_l = tok.lower()
check("...and says out loud when a person released it",
      "released from the keychain by someone at this Mac" in tok)
check("...and when the keychain refused",
      "did not release the token" in tok_l)
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
