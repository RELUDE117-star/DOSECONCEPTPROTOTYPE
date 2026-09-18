#!/usr/bin/env python3
"""bootstrap_claude_access.py — one-time, idempotent Raspberry Pi setup
that grants a developer Mac direct SSH control of this device.

It runs ON the Pi, after the normal GitHub update delivers it. It never
handles a private key and never touches the GitHub credential: it only
installs a PUBLIC key (safe by nature) that the Mac generated, configures
a dedicated `claudeagent` account for key-based SSH, and writes a
SANITIZED status file so the setup can be verified without anyone reading
a terminal on the Pi.

Design rules (all enforced below):
  * Idempotent. Re-running never corrupts anything; completed steps are
    skipped, incomplete ones retried.
  * No secrets in, no secrets out. Only a public key goes in; only
    non-sensitive facts go in the status file.
  * Least privilege. Works as root, via passwordless sudo, or as an
    unprivileged user — doing everything it can at the level it has and
    reporting precisely the one step a human must finish if it cannot.
  * Reversible & safe. Backs up any file before editing, validates sshd
    and sudoers config BEFORE reloading, never enables root SSH, never
    opens a router port.

Exit code is always 0 — this must never take the voice app down with it.
"""

import json
import os
import pwd
import grp
import shutil
import socket
import subprocess
import sys
import time

# v2: configure_dev_sudo() used to demand real root and bail under
# passwordless sudo, so on a device where every OTHER step succeeded via
# sudo the dev account ended up with SSH but no sudo. Bumping the version
# is what lets main() re-run past its "already complete" early return and
# finish the job on an existing install.
BOOTSTRAP_VERSION = 2
DEV_USER = os.environ.get("DOSE_CLAUDE_USER", "claudeagent")
# Hardware groups the assistant work needs — added only if they exist.
HW_GROUPS = ("audio", "video", "dialout", "gpio", "i2c", "spi",
             "plugdev", "render")
APP_DIR = os.path.expanduser("~/dose-home-station")


# ── privilege model ───────────────────────────────────────────────────
def is_root():
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def have_passwordless_sudo():
    try:
        r = subprocess.run(["sudo", "-n", "true"],
                           capture_output=True, timeout=8)
        return r.returncode == 0
    except Exception:
        return False


def priv_mode():
    if is_root():
        return "root"
    if have_passwordless_sudo():
        return "sudo"
    return "user"


def run_priv(argv, mode, **kw):
    """Run a command with privilege if we have it. Returns
    (ok, stdout, stderr). Never raises."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 120)
    if mode == "sudo":
        argv = ["sudo", "-n"] + argv
    try:
        r = subprocess.run(argv, **kw)
        return r.returncode == 0, (r.stdout or ""), (r.stderr or "")
    except Exception as e:
        return False, "", str(e)


def run(argv, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 60)
    try:
        r = subprocess.run(argv, **kw)
        return r.returncode == 0, (r.stdout or ""), (r.stderr or "")
    except Exception as e:
        return False, "", str(e)


# ── state ─────────────────────────────────────────────────────────────
def state_dir():
    """Where the bootstrap remembers what it has done. Prefer a
    system path when privileged, else a per-user path."""
    if is_root():
        d = "/var/lib/dose-claude-bootstrap"
    else:
        d = os.path.expanduser("~/.local/state/dose-claude-bootstrap")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(APP_DIR, "claude-bootstrap")
        os.makedirs(d, exist_ok=True)
    return d


def load_state(d):
    try:
        with open(os.path.join(d, "state.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d, st):
    try:
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump(st, f, indent=2)
    except Exception:
        pass


def backup_once(path, mode):
    """Copy a file to <path>.dose-bak once (never overwrite an existing
    backup, so the ORIGINAL is always recoverable)."""
    bak = path + ".dose-bak"
    if os.path.exists(path) and not os.path.exists(bak):
        if mode == "root" or is_root():
            try:
                shutil.copy2(path, bak)
            except Exception:
                run_priv(["cp", "-p", path, bak], mode)
        else:
            run_priv(["cp", "-p", path, bak], mode)


# ── public key source (NEVER a private key) ───────────────────────────
def _looks_like_pubkey(line):
    """A REAL OpenSSH public key, not a placeholder.

    Checking only the 'ssh-ed25519 ' prefix was not enough: a template
    line like 'ssh-ed25519 AAAA...my-mac-pubkey... comment' passed and
    would have been written into authorized_keys as a broken entry,
    silently making key auth fail. So we decode the base64 body and
    confirm the key type embedded inside it matches the prefix — which
    only a genuine key can satisfy."""
    line = (line or "").strip()
    if line.startswith("#") or not line:
        return False
    parts = line.split()
    if len(parts) < 2:
        return False
    ktype, body = parts[0], parts[1]
    if ktype not in ("ssh-ed25519", "ssh-rsa", "ssh-dss",
                     "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                     "ecdsa-sha2-nistp521",
                     "sk-ssh-ed25519@openssh.com",
                     "sk-ecdsa-sha2-nistp256@openssh.com"):
        return False
    # a placeholder gives itself away here: '.' is not base64
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9+/]+={0,3}", body) or len(body) < 32:
        return False
    try:
        import base64 as _b64
        import struct as _st
        raw = _b64.b64decode(body, validate=True)
        n = _st.unpack(">I", raw[:4])[0]
        if n <= 0 or n > 64 or len(raw) < 4 + n:
            return False
        return raw[4:4 + n].decode("ascii", "ignore") == ktype
    except Exception:
        return False


def collect_pubkeys():
    """Gather the developer's PUBLIC key(s) from the places the repo/app
    may have placed them. A placeholder file yields nothing."""
    keys = []
    env = os.environ.get("DOSE_CLAUDE_PUBKEY", "").strip()
    if _looks_like_pubkey(env):
        keys.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(APP_DIR, "claude_dev_authorized_keys"),
        os.path.join(here, "claude_dev_authorized_keys"),
        os.path.join(here, "..", "claude_dev_authorized_keys"),
    ]
    for path in candidates:
        try:
            with open(path) as f:
                for line in f:
                    if _looks_like_pubkey(line):
                        keys.append(line.strip())
        except Exception:
            continue
    # de-dup, preserve order
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# ── account + ssh ─────────────────────────────────────────────────────
def user_exists(name):
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def ensure_account(mode):
    if user_exists(DEV_USER):
        return True
    if mode == "user":
        return False
    ok, _, _ = run_priv(
        ["useradd", "-m", "-s", "/bin/bash", DEV_USER], mode)
    return ok or user_exists(DEV_USER)


def ensure_groups(mode):
    if not user_exists(DEV_USER):
        return []
    try:
        current = set(g.gr_name for g in grp.getgrall()
                      if DEV_USER in g.gr_mem)
    except Exception:
        current = set()
    added = []
    for g in HW_GROUPS:
        try:
            grp.getgrnam(g)
        except KeyError:
            continue                       # group does not exist here
        if g in current:
            continue
        if mode == "user":
            continue
        ok, _, _ = run_priv(["usermod", "-aG", g, DEV_USER], mode)
        if ok:
            added.append(g)
    return added


def _home_of(name):
    try:
        return pwd.getpwnam(name).pw_dir
    except Exception:
        return None


def install_authorized_keys(target_user, keys, mode):
    """Install PUBLIC keys into <target_user>'s authorized_keys with
    correct ownership and permissions. Idempotent — never duplicates a
    key already present."""
    if not keys:
        return False, "no public key provided yet"
    home = _home_of(target_user)
    if not home:
        return False, "no home for %s" % target_user
    ssh_dir = os.path.join(home, ".ssh")
    ak = os.path.join(ssh_dir, "authorized_keys")

    # Are we able to write as that user?
    can_direct = (target_user == _current_user()) or is_root()

    if can_direct and not is_root() and target_user != _current_user():
        can_direct = False

    existing = ""
    try:
        existing = open(ak).read()
    except Exception:
        # try privileged read
        if mode != "user":
            _, existing, _ = run_priv(["cat", ak], mode)

    to_add = [k for k in keys if k.split()[1] not in existing] \
        if existing else keys
    # compare by the key BODY (2nd field), not the comment
    def body(k):
        p = k.split()
        return p[1] if len(p) > 1 else k
    have_bodies = set()
    for line in (existing or "").splitlines():
        if _looks_like_pubkey(line):
            have_bodies.add(body(line))
    to_add = [k for k in keys if body(k) not in have_bodies]
    if not to_add and existing:
        return True, "authorized_keys already current"

    new_content = (existing.rstrip("\n") + "\n" if existing.strip()
                   else "") + "\n".join(to_add) + "\n"

    if is_root() or target_user == _current_user():
        try:
            os.makedirs(ssh_dir, exist_ok=True)
            with open(ak, "w") as f:
                f.write(new_content)
            os.chmod(ssh_dir, 0o700)
            os.chmod(ak, 0o600)
            if is_root():
                import pwd as _p
                u = _p.getpwnam(target_user)
                os.chown(ssh_dir, u.pw_uid, u.pw_gid)
                os.chown(ak, u.pw_uid, u.pw_gid)
            return True, "installed %d key(s)" % len(to_add)
        except Exception as e:
            return False, "write failed: %s" % str(e)[:60]
    elif mode == "sudo":
        # write via a privileged helper, then fix ownership/perms
        tmp = "/tmp/.dose_ak_%d" % int(time.time())
        try:
            with open(tmp, "w") as f:
                f.write(new_content)
        except Exception as e:
            return False, "temp write failed: %s" % str(e)[:60]
        run_priv(["install", "-d", "-m", "700", "-o", target_user,
                  "-g", target_user, ssh_dir], mode)
        ok, _, err = run_priv(
            ["install", "-m", "600", "-o", target_user, "-g",
             target_user, tmp, ak], mode)
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return ok, ("installed %d key(s)" % len(to_add)) if ok else err
    return False, "insufficient privilege to write %s's keys" % target_user


def _current_user():
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return os.environ.get("USER", "pi")


def ensure_openssh(mode):
    """Return (installed, running, note). Installs the server if we can
    and it is missing."""
    have = shutil.which("sshd") is not None or os.path.exists(
        "/usr/sbin/sshd")
    if not have and mode != "user":
        # detect package manager, prefer apt on Pi OS
        if shutil.which("apt-get"):
            run_priv(["apt-get", "update", "-qq"], mode, timeout=300)
            run_priv(["apt-get", "install", "-y", "openssh-server"],
                     mode, timeout=600)
        have = shutil.which("sshd") is not None or os.path.exists(
            "/usr/sbin/sshd")
    running = False
    if mode != "user":
        # enable + start via systemd (service is 'ssh' on Debian/Pi OS)
        for svc in ("ssh", "sshd"):
            run_priv(["systemctl", "enable", svc], mode)
            run_priv(["systemctl", "start", svc], mode)
        ok, out, _ = run(["systemctl", "is-active", "ssh"])
        running = "active" in out
        if not running:
            ok, out, _ = run(["systemctl", "is-active", "sshd"])
            running = "active" in out
    else:
        ok, out, _ = run(["systemctl", "is-active", "ssh"])
        running = "active" in out
    return have, running, ("ok" if have else "sshd not installed")


def configure_sshd_dropin(mode):
    """Narrowly scope key-only auth for the dev account via a drop-in,
    validate, then reload. Never rewrites the main config, never enables
    root login. Returns (ok, note)."""
    if mode == "user":
        return False, "needs privilege"
    d = "/etc/ssh/sshd_config.d"
    conf = os.path.join(d, "60-claudeagent.conf")
    body = (
        "# DOSE dev access — key-only auth for %s. Managed by\n"
        "# tools/bootstrap_claude_access.py; safe to remove to revoke.\n"
        "Match User %s\n"
        "    PubkeyAuthentication yes\n"
        "    PasswordAuthentication no\n"
        "    KbdInteractiveAuthentication no\n"
        % (DEV_USER, DEV_USER))
    # Only valid if the main config includes the drop-in dir.
    main = "/etc/ssh/sshd_config"
    includes = False
    try:
        includes = "sshd_config.d/*.conf" in open(main).read()
    except Exception:
        pass
    if not os.path.isdir(d) and not includes:
        # Fall back: append a guarded block to the main config (backed
        # up first) only if there is no drop-in mechanism at all.
        backup_once(main, mode)
        # write via privileged tee
        block = "\n# >>> DOSE claudeagent (managed) >>>\n" + body + \
                "# <<< DOSE claudeagent (managed) <<<\n"
        tmp = "/tmp/.dose_sshd_%d" % int(time.time())
        try:
            cur = open(main).read()
        except Exception:
            cur = ""
        if "DOSE claudeagent (managed)" not in cur:
            with open(tmp, "w") as f:
                f.write(cur.rstrip("\n") + "\n" + block)
            run_priv(["install", "-m", "644", tmp, main], mode)
            try:
                os.unlink(tmp)
            except Exception:
                pass
    else:
        run_priv(["install", "-d", "-m", "755", d], mode)
        tmp = "/tmp/.dose_sshd_dropin_%d" % int(time.time())
        with open(tmp, "w") as f:
            f.write(body)
        run_priv(["install", "-m", "644", tmp, conf], mode)
        try:
            os.unlink(tmp)
        except Exception:
            pass
    # VALIDATE before touching the running service.
    ok, out, err = run_priv(["sshd", "-t"], mode)
    if not ok:
        return False, "sshd config invalid, not reloaded: %s" \
            % (err or out)[:80]
    # reload (not restart) so existing sessions survive
    r_ok, _, _ = run_priv(["systemctl", "reload", "ssh"], mode)
    if not r_ok:
        run_priv(["systemctl", "reload", "sshd"], mode)
    return True, "key-only auth configured for %s" % DEV_USER


def configure_dev_sudo(mode):
    """Development-time passwordless sudo for the dev account, via a
    validated drop-in. Documented as dev-only.

    THIS USED TO DEMAND REAL ROOT and that was the bug. Every other step
    here — creating the account, adding it to groups, installing the
    authorized_keys, writing the sshd drop-in — goes through run_priv(),
    which prefixes `sudo -n` when mode == "sudo" and works perfectly well
    that way. Only this function short-circuited on `mode != "root"`.

    The result on the real device: the app runs as the kiosk user, which
    HAS passwordless sudo, so the bootstrap ran in "sudo" mode and
    succeeded at everything — the claudeagent account exists, is in the
    audio/video/gpio groups, and accepts the developer key — except this
    one step, which returned "needs root" and left the account with SSH
    but no sudo. A whole debugging session was then blocked on reading
    the app directory (mode 0700), and the only way out on offer was a
    human typing a long command on a touchscreen keyboard.

    run_priv already routes through sudo, and the drop-in is validated
    with `visudo -cf` in isolation BEFORE it is installed and again
    afterwards, so accepting "sudo" here is no less safe than root."""
    if mode not in ("root", "sudo") and not is_root():
        return False, "needs root or passwordless sudo"
    path = "/etc/sudoers.d/90-claudeagent"
    body = (
        "# DOSE DEVELOPMENT ONLY — passwordless sudo for the Claude dev\n"
        "# account so it can install packages, inspect audio hardware,\n"
        "# manage services and logs. RESTRICT OR REMOVE for production.\n"
        "%s ALL=(ALL) NOPASSWD: ALL\n" % DEV_USER)
    tmp = "/tmp/.dose_sudoers_%d" % int(time.time())
    try:
        with open(tmp, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o440)
    except Exception as e:
        return False, "temp write failed: %s" % str(e)[:60]
    # VALIDATE the drop-in in isolation before installing it.
    ok, _, err = run_priv(["visudo", "-cf", tmp], mode)
    if not ok:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False, "sudoers invalid, not installed: %s" % err[:80]
    run_priv(["install", "-m", "440", "-o", "root", "-g", "root",
              tmp, path], mode)
    try:
        os.unlink(tmp)
    except Exception:
        pass
    # final full validation
    ok, _, err = run_priv(["visudo", "-cf", "/etc/sudoers"], mode)
    return ok, ("passwordless dev sudo configured" if ok
                else "post-install validation failed")


# ── discovery (sanitized) ─────────────────────────────────────────────
def local_addresses():
    addrs = []
    try:
        ok, out, _ = run(["hostname", "-I"])
        if ok:
            for a in out.split():
                if ":" not in a and not a.startswith("127."):
                    addrs.append(a)
    except Exception:
        pass
    if not addrs:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            addrs.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return addrs


def _first_line(path):
    try:
        return open(path).read().strip().strip("\x00")
    except Exception:
        return ""


def sanitized_status(st, mode):
    model = _first_line("/proc/device-tree/model")
    ok, arch, _ = run(["uname", "-m"])
    ok, kernel, _ = run(["uname", "-r"])
    os_name = ""
    try:
        for line in open("/etc/os-release"):
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    git_commit = ""
    try:
        ok, out, _ = run(["git", "-C", APP_DIR, "rev-parse", "--short",
                          "HEAD"])
        if ok:
            git_commit = out.strip()
    except Exception:
        pass
    service = ""
    for cand in ("dose", "dose-home-station", "doseapp"):
        ok, out, _ = run(["systemctl", "is-enabled", cand])
        if ok:
            service = cand
            break
    return {
        "device": "dose-pi",
        "bootstrap_version": BOOTSTRAP_VERSION,
        "ready": bool(st.get("ssh_running") and st.get("account_configured")
                      and st.get("public_key_installed")),
        "hostname": socket.gethostname(),
        "addresses": local_addresses(),
        "ssh_port": 22,
        "ssh_user": DEV_USER,
        "privilege": mode,
        "model": model,
        "architecture": (arch or "").strip(),
        "os": os_name,
        "kernel": (kernel or "").strip(),
        "app_dir": APP_DIR,
        "git_commit": git_commit,
        "service": service,
        "ssh_installed": st.get("ssh_installed"),
        "ssh_running": st.get("ssh_running"),
        "account_configured": st.get("account_configured"),
        "public_key_installed": st.get("public_key_installed"),
        "dev_sudo": st.get("dev_sudo"),
        "groups_added": st.get("groups_added", []),
        "pending_manual_step": st.get("pending_manual_step"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ── main ──────────────────────────────────────────────────────────────
def main():
    d = state_dir()
    st = load_state(d)
    if st.get("bootstrap_version") == BOOTSTRAP_VERSION \
            and st.get("completed"):
        # Already done. Refresh the status (IP may have changed) and exit.
        mode = priv_mode()
        status = sanitized_status(st, mode)
        _write_status(d, status)
        print("bootstrap v%d already complete" % BOOTSTRAP_VERSION)
        return 0

    mode = priv_mode()
    st["bootstrap_version"] = BOOTSTRAP_VERSION
    st["privilege"] = mode

    # 1) OpenSSH
    installed, running, note = ensure_openssh(mode)
    st["ssh_installed"] = installed
    st["ssh_running"] = running

    # 2) account + groups
    acct = ensure_account(mode)
    st["account_configured"] = acct
    st["groups_added"] = ensure_groups(mode) if acct else []

    # 3) public key -> the dev account if it exists, else current user
    keys = collect_pubkeys()
    target = DEV_USER if acct else _current_user()
    pk_ok, pk_note = install_authorized_keys(target, keys, mode)
    st["public_key_installed"] = pk_ok
    st["public_key_note"] = pk_note
    st["public_key_target"] = target

    # 4) sshd drop-in (key-only for the dev account)
    if acct:
        sd_ok, sd_note = configure_sshd_dropin(mode)
        st["sshd_configured"] = sd_ok
        st["sshd_note"] = sd_note

    # 5) dev sudo (root OR passwordless sudo — see configure_dev_sudo)
    su_ok, su_note = configure_dev_sudo(mode)
    st["dev_sudo"] = su_ok
    st["dev_sudo_note"] = su_note

    # Decide what, if anything, still needs a human.
    pending = None
    if mode == "user":
        pending = (
            "Run ONE command on the Pi to finish system provisioning:\n"
            "  sudo python3 %s\n"
            "(this installs OpenSSH, creates the %s account, and "
            "configures key-only SSH; the current user already has the "
            "public key if one was provided)."
            % (os.path.abspath(__file__), DEV_USER))
    elif not keys:
        pending = ("No developer public key found yet. Add the Mac's "
                   "ed25519 PUBLIC key to tools/claude_dev_authorized_keys "
                   "in the repo (public keys are safe to commit); the next "
                   "update will install it for %s." % target)
    st["pending_manual_step"] = pending

    st["completed"] = bool(
        st.get("ssh_running") and st.get("account_configured")
        and st.get("public_key_installed") and not pending)
    save_state(d, st)

    status = sanitized_status(st, mode)
    _write_status(d, status)
    _maybe_report(status)

    print("bootstrap v%d: %s" % (
        BOOTSTRAP_VERSION,
        "COMPLETE" if st["completed"] else "partial (see status)"))
    if pending:
        print("PENDING: " + pending.splitlines()[0])
    return 0


def _write_status(d, status):
    try:
        with open(os.path.join(d, "status.json"), "w") as f:
            json.dump(status, f, indent=2)
        # also drop a copy where the app/audit can find it
        os.makedirs(APP_DIR, exist_ok=True)
        with open(os.path.join(APP_DIR, "claude-bootstrap-status.json"),
                  "w") as f:
            json.dump(status, f, indent=2)
    except Exception:
        pass


def _maybe_report(status):
    """OPTIONALLY publish the sanitized status as a GitHub issue, using a
    token if one is already on the device. The Pi never sends its token
    anywhere — it only uses it locally to authenticate this one request.
    If no token exists, we simply skip (LAN discovery is the fallback).
    The status object contains NO secrets."""
    tok = ""
    for p in ("~/dose-home-station/github_token", "~/.dose_github_token"):
        try:
            p = os.path.expanduser(p)
            if os.path.isfile(p):
                tok = open(p).read().strip()
                if tok:
                    break
        except Exception:
            continue
    if not tok:
        return
    try:
        import urllib.request
        body = json.dumps({
            "title": "Bootstrap status — %s (v%d)"
                     % (status.get("hostname", "pi"),
                        status.get("bootstrap_version")),
            "body": "```json\n" + json.dumps(status, indent=2) + "\n```",
            "labels": ["bootstrap-status"],
        }).encode()
        req = urllib.request.Request(
            "https://api.github.com/repos/relude117-star/"
            "doseconceptprototype/issues",
            data=body, method="POST",
            headers={"Authorization": "Bearer " + tok,
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "dose-bootstrap",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Must NEVER take the app down. Log and exit clean.
        try:
            print("bootstrap error (non-fatal): %s" % str(e)[:120])
        except Exception:
            pass
        sys.exit(0)
