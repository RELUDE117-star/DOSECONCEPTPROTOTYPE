"""The Pi developer-access bootstrap.

Guards the two things that must never go wrong:
  * a PLACEHOLDER or malformed public key is never installed (that would
    write a broken authorized_keys line and silently break key auth);
  * the bootstrap is idempotent and its status object carries NO secrets.

No system is modified: privileged operations are stubbed, and the "real"
keys are synthesised in OpenSSH wire format (public material only — no
private key is ever created, read, or written).
"""
import base64
import importlib.util
import json
import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASSED = FAILED = 0
FAILS = []


def ok(cond, label):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        FAILS.append(label)
        print("  FAIL", label)


def _load():
    spec = importlib.util.spec_from_file_location(
        "bca", os.path.join(ROOT, "tools", "bootstrap_claude_access.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mk_pubkey(ktype="ssh-ed25519", keylen=32):
    """A structurally valid OpenSSH PUBLIC key blob. Public material only."""
    raw = (struct.pack(">I", len(ktype)) + ktype.encode()
           + struct.pack(">I", keylen) + os.urandom(keylen))
    return ktype + " " + base64.b64encode(raw).decode() + " dose-pi dev"


m = _load()

print("== 1. a placeholder key is NEVER accepted ==")
REAL = _mk_pubkey()
for label, val, want in (
        ("the documented placeholder",
         "ssh-ed25519 AAAA...my-mac-pubkey... dose-pi claude dev", False),
        ("truncated body", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 x@y", False),
        ("a comment line", "# ssh-ed25519 AAAAC3... note", False),
        ("blank", "   ", False),
        ("type that disagrees with the body",
         "ssh-rsa " + REAL.split()[1] + " x@y", False),
        ("a real ed25519 key", REAL, True)):
    ok(m._looks_like_pubkey(val) is want,
       "%s -> %s" % (label, want))

print("== 2. the shipped key file is inert until a real key is added ==")
ok(m.collect_pubkeys() == [] or all(
    m._looks_like_pubkey(k) for k in m.collect_pubkeys()),
   "collect_pubkeys yields only genuine keys (placeholder file is inert)")

print("== 3. idempotent, and the status leaks nothing ==")
m2 = _load()
m2.run = lambda a, **k: (True, "ok\n", "")
m2.run_priv = lambda a, mode, **k: (True, "active\n", "")
m2.is_root = lambda: False
m2.have_passwordless_sudo = lambda: True          # STATE 2
m2.user_exists = lambda n: True
m2.ensure_openssh = lambda mode: (True, True, "ok")
m2.configure_sshd_dropin = lambda mode: (True, "key-only")
m2.configure_dev_sudo = lambda mode: (True, "dev sudo")
m2.install_authorized_keys = lambda u, k, mode: (True, "installed")
m2.ensure_groups = lambda mode: ["audio", "video"]
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["DOSE_CLAUDE_PUBKEY"] = REAL
m2.APP_DIR = os.path.join(home, "dose-home-station")

ok(m2.main() == 0, "first run exits 0 (never takes the app down)")
d = m2.state_dir()
st = m2.load_state(d)
ok(st.get("completed") is True, "it reports completion")
ok(m2.main() == 0, "a second run is a safe no-op (idempotent)")

status = json.load(open(os.path.join(d, "status.json")))
blob = json.dumps(status).lower()
for bad in ("ghp_", "github_pat", "token", "password", "secret",
            "private", "bearer", "authorization", "ssh-ed25519"):
    ok(bad not in blob, "status carries no %r" % bad)
for field in ("hostname", "addresses", "ssh_user", "bootstrap_version",
              "ready", "timestamp"):
    ok(field in status, "status reports %s" % field)

print("== 4. no privilege -> precise single manual step, still exits 0 ==")
m3 = _load()
m3.run = lambda a, **k: (True, "ok\n", "")
m3.is_root = lambda: False
m3.have_passwordless_sudo = lambda: False         # STATE 3
m3.user_exists = lambda n: n == m3._current_user()
m3.ensure_openssh = lambda mode: (True, True, "ok")
m3.install_authorized_keys = lambda u, k, mode: (True, "installed")
os.environ["HOME"] = tempfile.mkdtemp()
m3.APP_DIR = os.path.join(os.environ["HOME"], "dose-home-station")
ok(m3.main() == 0, "unprivileged run still exits 0")
st3 = m3.load_state(m3.state_dir())
ok(st3.get("completed") is False, "it does not claim completion")
ok("sudo" in (st3.get("pending_manual_step") or ""),
   "and names the ONE sudo command a human must run")

print("== 5. it never escalates privilege by itself ==")
src = open(os.path.join(ROOT, "tools",
                        "bootstrap_claude_access.py")).read()
for bad in ("chmod u+s", "setuid", "/etc/shadow", "pkexec",
            "NOPASSWD: ALL\\n%", "su -c"):
    ok(bad not in src, "no privilege-escalation trick (%r)" % bad)
ok("visudo" in src and "sshd" in src and "-t" in src,
   "sudoers and sshd config are VALIDATED before being relied on")
ok("PermitRootLogin yes" not in src, "root SSH is never enabled")

print()
print("bootstrap suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== PI BOOTSTRAP: ALL PASSED (no placeholder keys, no secrets) ===")
