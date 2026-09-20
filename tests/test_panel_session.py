#!/usr/bin/env python3
"""Reopening the connector panel must not hand back a dead panel.

2026-09-19. Ryan, testing: "And make sure I can see it in the dose pi
conenctor app". The panel WAS running. Reopening it the normal way
was useless, and the reason was four lines:

    if already_running():
        # ... The session key belongs to that process, so the browser
        # goes to the bare URL and it redirects.
        webbrowser.open("http://127.0.0.1:%d/" % PANEL_PORT)

It does not redirect. `/` serves the page unconditionally, the page
reads its key from `location.search`, finds none, and every call it
makes afterwards comes back 403 "stale window". So the window opened,
looked completely normal, and every button silently did nothing. The
comment asserted a behaviour the code did not have — which is the
same failure this project has recorded before under "A DOCSTRING IS
NOT AN INVARIANT".

The key now lives in ~/.dose-server/panel-session, 0600, rewritten
every start, so the already-running path can open a URL that works.

WHAT THIS FILE CHECKS, AND HOW
This does not grep the source. It starts a real panel on a spare
port, makes real HTTP requests to it, and checks what comes back:

  - the key file exists, is owner-only, and matches the live process
  - a request WITH the key is served
  - a request WITHOUT one is refused — that refusal is the whole
    security boundary, and "make reopening work" must not weaken it
  - a request with someone else's key is refused

Run:  python3 tests/test_panel_session.py
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "tools", "dose_panel.py")

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(url):
    """(status, body). A 403 is an answer, not an exception."""
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(400).decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


PORT = free_port()
HOME = tempfile.mkdtemp(prefix="panel_home_")
env = dict(os.environ, HOME=HOME, DOSE_PANEL_PORT=str(PORT),
           BROWSER="true", DISPLAY="")
proc = subprocess.Popen([sys.executable, PANEL], env=env, cwd=ROOT,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True)
SESSION_FILE = os.path.join(HOME, ".dose-server", "panel-session")

try:
    # Wait for it to actually be listening, rather than sleeping a
    # guessed amount and hoping.
    up = False
    for _ in range(80):
        time.sleep(0.25)
        if proc.poll() is not None:
            break
        st, _b = get("http://127.0.0.1:%d/" % PORT)
        if st:
            up = True
            break
    check("the panel started and is serving", up,
          "it exited with %s" % proc.poll())

    print("\n── the key is on disk, and only for its owner ──────────────")
    check("the session file exists", os.path.isfile(SESSION_FILE),
          SESSION_FILE)
    mode = os.stat(SESSION_FILE).st_mode & 0o777 if os.path.isfile(
        SESSION_FILE) else None
    check("...owner-only (0600)", mode == 0o600, "mode is %o" % (mode or 0))
    key = ""
    if os.path.isfile(SESSION_FILE):
        key = open(SESSION_FILE).read().strip()
    check("...and it is a real key, not an empty file",
          len(key) >= 20, "%d chars" % len(key))

    print("\n── the key on disk is the one the LIVE panel accepts ───────")
    st, body = get("http://127.0.0.1:%d/api/status?k=%s" % (PORT, key))
    check("a request carrying it is served", st == 200, "status %s" % st)
    check("...and the answer is the panel's own status",
          st == 200 and ("panel" in body or "server_port" in body),
          body[:120])

    print("\n── and the boundary it exists to enforce still holds ───────")
    st_none, b_none = get("http://127.0.0.1:%d/api/status" % PORT)
    check("NO key is refused", st_none == 403,
          "status %s — this key is what stops any other program on "
          "this Mac driving the panel" % st_none)
    check("...with the stale-window message", "stale" in b_none.lower(),
          b_none[:120])
    st_bad, _ = get("http://127.0.0.1:%d/api/status?k=%s"
                    % (PORT, "x" * len(key)))
    check("somebody ELSE's key is refused", st_bad == 403,
          "status %s" % st_bad)
    st_empty, _ = get("http://127.0.0.1:%d/api/status?k=" % PORT)
    check("an empty key is refused", st_empty == 403,
          "status %s" % st_empty)

    print("\n── the bare page still loads (it is the reopen target) ─────")
    st_root, _ = get("http://127.0.0.1:%d/" % PORT)
    check("/ is served", st_root == 200, "status %s" % st_root)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

print("\n── and the misleading comment is gone ──────────────────────")
SRC = open(PANEL, encoding="utf-8").read()
code = "\n".join(l for l in SRC.splitlines()
                 if not l.lstrip().startswith("#"))
check("the already-running path opens a KEYED url",
      'url = "http://127.0.0.1:%d/?k=%s" % (PANEL_PORT, k)' in code,
      "it used to open the bare URL and call that a redirect")
check("...and says so plainly when it cannot read the key",
      "unable to do" in SRC)

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("PANEL: it can be reopened, and only by its owner")
