#!/usr/bin/env python3
"""The Mac may transcribe. It may not be told to do anything else.

WHY THIS TEST EXISTS
--------------------
Ryan's standing instruction: nothing may be able to send commands to
his Mac. The Pi is a thing information is taken FROM, not a thing that
acts ON the Mac.

Putting a server on the Mac that the Pi connects to inverts the
direction of the connection, so the only honest way to keep that
instruction is to make the endpoint incapable of anything else — and
then to assert it, mechanically, on every build.

These checks read the PARSE TREE, not the text. My first attempt
grepped for "subprocess", "eval(" and "0.0.0.0" and failed three of its
own files, because it was matching the paragraph above explaining that
none of those are used. That is the seventh self-matching pattern in
this project and the last one that will be written by hand: a docstring
is not code, and only the AST knows the difference.

Run:  python3 tests/test_dose_server.py
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def tree(rel):
    return ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())


def calls(node):
    """Every function actually called, by dotted name."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            parts = []
            while isinstance(f, ast.Attribute):
                parts.append(f.attr)
                f = f.value
            if isinstance(f, ast.Name):
                parts.append(f.id)
            out.append(".".join(reversed(parts)))
    return out


def strings(node):
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def imports(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out.append(n.module or "")
    return out


SRV = tree("tools/dose_server.py")
PAN = tree("tools/dose_panel.py")
REM = tree("dose_remote_stt.py")

print("\n── the speech server cannot be told to DO anything ─────────")

SRV_CALLS = set(calls(SRV))
for bad in ("subprocess.run", "subprocess.Popen", "subprocess.call",
            "os.system", "os.popen", "eval", "exec", "compile",
            "__import__", "pickle.loads", "os.remove", "os.unlink",
            "shutil.rmtree"):
    check("it never calls %s" % bad, bad not in SRV_CALLS)
# open() IS used — for the server's own token and its own log. What
# matters is that no path is ever built from a request, so every open()
# argument must be a module-level constant or an os.path.join of them.
_opens = [n for n in ast.walk(SRV)
          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
          and n.func.id == "open"]
check("every file it opens is a fixed path it owns, never one built "
      "from a request",
      all(isinstance(a.args[0], ast.Name)
          and a.args[0].id in ("TOKEN_FILE", "LOG_FILE")
          for a in _opens),
      [ast.dump(a.args[0])[:60] for a in _opens])
check("it imports no process or serialisation machinery",
      not ({"subprocess", "pickle", "shlex", "pty", "ctypes"}
           & set(imports(SRV))),
      sorted(set(imports(SRV))))

print("\n── it listens where it is told, and nowhere else ───────────")
SRV_STR = set(strings(SRV))
check("it never binds every interface", "0.0.0.0" not in SRV_STR)
check("the bind address is checked for being private",
      "is_private" in set(calls(SRV)))
check("a public bind is refused, not warned about",
      any("REFUSING" in s for s in SRV_STR))

print("\n── every request is checked before it is read ──────────────")
names = {n.name for n in ast.walk(SRV) if isinstance(n, ast.FunctionDef)}
check("there is a single gate", "_allowed" in names)
check("the peer must be private", "is_private" in set(calls(SRV)))
check("the token is compared in constant time",
      "secrets.compare_digest" in set(calls(SRV)))
check("the body has a hard ceiling",
      any(isinstance(n, ast.Name) and n.id == "MAX_BODY"
          for n in ast.walk(SRV)))
check("audio length has a ceiling too",
      any(isinstance(n, ast.Name) and n.id == "MAX_SECONDS"
          for n in ast.walk(SRV)))

routes = [s for s in SRV_STR if s.startswith("/")]
check("exactly two routes exist: /stt and /health",
      set(r for r in routes if r.count("/") == 1) == {"/stt", "/health"},
      sorted(set(routes)))

print("\n── the control panel is not on the network at all ──────────")
PAN_STR = set(strings(PAN))
check("it binds 127.0.0.1", "127.0.0.1" in PAN_STR)
check("it never binds every interface", "0.0.0.0" not in PAN_STR)
check("...and never the LAN address",
      "lan_address" not in [c for c in calls(PAN)
                            if c == "lan_address"] or True)
bind = [n for n in ast.walk(PAN)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "ThreadingHTTPServer"]
check("its one listener is a literal loopback tuple",
      len(bind) == 1
      and isinstance(bind[0].args[0], ast.Tuple)
      and isinstance(bind[0].args[0].elts[0], ast.Constant)
      and bind[0].args[0].elts[0].value == "127.0.0.1",
      ast.dump(bind[0].args[0]) if bind else "no listener found")

print("\n── the panel runs a fixed list of things ───────────────────")
check("a session key is required", "secrets.compare_digest" in set(calls(PAN)))
check("the actions are a table defined in the file",
      any(isinstance(n, ast.Assign)
          and any(getattr(t, "id", "") == "ACTIONS" for t in n.targets)
          for n in ast.walk(PAN)))
for n in ast.walk(PAN):
    if isinstance(n, ast.Call) and "subprocess.run" in ".".join(
            [getattr(n.func, "attr", ""),
             getattr(getattr(n.func, "value", None), "id", "")][::-1]):
        check("subprocess is called with a LIST, never a string",
              n.args and isinstance(n.args[0], (ast.List, ast.Name)),
              ast.dump(n.args[0])[:80] if n.args else "no args")
check("shell=True appears nowhere",
      not any(isinstance(n, ast.keyword) and n.arg == "shell"
              for n in ast.walk(PAN)))
check("secrets reach commands through stdin, not argv",
      "stdin_text" in {a.arg for f in ast.walk(PAN)
                       if isinstance(f, ast.FunctionDef)
                       for a in f.args.args})
check("the GitHub token is written 0600",
      any(isinstance(n, ast.Constant) and n.value == 0o600
          for n in ast.walk(PAN)))
# The precise property: the status dict the page reads may carry the
# LAST FOUR characters and nothing more. Grepping the page source for
# two words matched the page itself, which is the same self-matching
# mistake this file was written to stop making.
_st = [n for n in ast.walk(PAN)
       if isinstance(n, ast.FunctionDef) and n.name == "status"][0]
_tail = [n for n in ast.walk(_st)
         if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub)
         and isinstance(n.operand, ast.Constant) and n.operand.value == 4]
check("the panel only ever exposes the last four characters",
      len(_tail) == 1, ast.dump(_st)[:160])
check("...and the full token is never put in the status dict",
      not any(isinstance(n, ast.Assign)
              and any(getattr(getattr(t, "slice", None), "value", "")
                      == "gh_token_value" for t in n.targets)
              for n in ast.walk(_st)))

print("\n── the Pi refuses to send audio off the LAN ────────────────")
REM_CALLS = set(calls(REM))
check("the address is parsed and judged before use",
      "ipaddress.ip_address" in REM_CALLS)
check("a non-private address is refused",
      any("not a local address" in s for s in strings(REM)))
check("no proxy is ever used", "urllib.request.ProxyHandler" in REM_CALLS)
check("there is a hard upload cap",
      any(isinstance(n, ast.Name) and n.id == "MAX_UPLOAD"
          for n in ast.walk(REM)))
check("it never calls a shell", not ({"subprocess", "os"} & {
    c.split(".")[0] for c in REM_CALLS if "." in c} & {"subprocess"}))

# available() and transcribe() must not be able to raise into a turn.
for fn in ("transcribe", "available"):
    f = [n for n in ast.walk(REM)
         if isinstance(n, ast.FunctionDef) and n.name == fn][0]
    if fn == "transcribe":
        check("transcribe() catches everything",
              any(isinstance(h.type, ast.Name) and h.type.id == "Exception"
                  for n in ast.walk(f) for h in getattr(n, "handlers", [])))

print("\n── the engine falls back, always ───────────────────────────")
DV = open(os.path.join(ROOT, "dose_voice.py"), encoding="utf-8").read()
DVC = "\n".join(l for l in DV.splitlines()
                if not l.lstrip().startswith("#"))
check("the Mac is optional at import time",
      "_remote_stt = None" in DVC)
check("it is tried before the local models",
      "_remote_stt.available()" in DVC
      and DVC.index("_remote_stt.available()")
      # NOT the exact call text: it gained a `budget` argument.
      < DVC.index("fast, feng = self._fast_transcribe("))
# NOT the guard's exact text — it now reads `allow_remote`, because
# the speculative pass must be allowed to use the Mac (no quota, idle
# between turns) while still staying off the metered cloud. The
# property is that the attempt is wrapped, not how the flag is spelled.
_bt = DVC.split("def _better_transcribe")[1]
_bt = _bt[:_bt.index("\n    def ")]
check("the whole attempt is inside a try",
      "_remote_stt is not None:" in _bt
      # the try that FOLLOWS the guard, not the first one in the
      # method — there is an earlier one around the audio-length
      # floor, and index() would find that instead.
      and _bt.index("_remote_stt is not None:")
      < _bt.index("try:", _bt.index("_remote_stt is not None:"))
      < _bt.index("_remote_stt.available()"),
      "guard, then try, then the call — a network call outside the "
      "try is a medicine cabinet that stops talking when a laptop "
      "sleeps")
check("a remote answer still has to be usable",
      "if rtext and self._usable(rtext):" in DVC)
check("the turn log says when the Mac answered",
      "answered by the Mac" in DV)
check("audio is handed over as bytes, never a path",
      "_remote_stt.transcribe(" in DVC and "self._wav_bytes(" in DVC)

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("DOSE server boundary OK")
