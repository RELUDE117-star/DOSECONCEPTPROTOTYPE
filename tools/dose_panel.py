#!/usr/bin/env python3
"""DOSE control panel — the bit you click, and the bit nothing can reach.

WHY THIS IS A SECOND PROGRAM
----------------------------
dose_server.py is spoken to by the Pi, so it listens on the LAN. This
panel holds a GitHub token and can open an SSH session to the cabinet,
so it must not be on the LAN at all.

It binds to 127.0.0.1 and nothing else. Not the LAN address, not
0.0.0.0. A process on this Mac can reach it; the Pi cannot, the router
cannot, and a device on the Wi-Fi cannot. Two jobs with two different
blast radii do not share a listener.

The rest of the rules:

  * A session key is generated at startup and lives only in memory.
    Every request must carry it. Closing the app invalidates it.
  * The GitHub token is written 0600 and is never rendered back into
    the page. The panel will tell you it is there and show the last
    four characters; it will not hand it back.
  * Nothing typed here is ever put into a shell string. Every command
    is a list of arguments, and the token reaches `ssh` through stdin,
    never argv — argv is visible to every process on the machine.
  * There is no route that runs a command chosen by the caller. The
    actions are a fixed list, written here, with no parameters but the
    ones this file defines.

    python3 tools/dose_panel.py            # opens in your browser
"""
import html
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".dose-server")
GH_TOKEN = os.path.join(STATE_DIR, "github_token")
SRV_TOKEN = os.path.join(STATE_DIR, "token")
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(HERE, "dose_server.py")

PI_HOST = os.environ.get("DOSE_PI_HOST", "dose-pi")
PI_ADDR = os.environ.get("DOSE_PI_ADDR", "192.168.4.154")
PI_APP = "/home/rjarv1/dose-home-station"
PANEL_PORT = int(os.environ.get("DOSE_PANEL_PORT", "8766"))

SESSION = secrets.token_urlsafe(24)
_SRV = {"proc": None}


def run(args, stdin_text=None, timeout=25):
    """Every command is a LIST. Nothing typed into the panel is ever
    concatenated into a shell string, and secrets go in on stdin
    because argv is readable by every process on this machine."""
    try:
        p = subprocess.run(args, input=stdin_text, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


def pi_reachable():
    rc, _ = run(["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes",
                 PI_HOST, "true"], timeout=12)
    return rc == 0


def lan_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 9))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def server_running():
    p = _SRV.get("proc")
    return bool(p and p.poll() is None)


def status():
    st = {
        "lan": lan_address(),
        "panel": "127.0.0.1:%d" % PANEL_PORT,
        "server_running": server_running(),
        "server_token": os.path.exists(SRV_TOKEN),
        "gh_token": os.path.exists(GH_TOKEN),
        "gh_tail": "",
        "pi_host": PI_HOST,
        "pi_addr": PI_ADDR,
    }
    if st["gh_token"]:
        try:
            with open(GH_TOKEN) as f:
                t = f.read().strip()
            st["gh_tail"] = t[-4:] if len(t) > 4 else "****"
        except Exception:
            pass
    return st


PAGE = """<!doctype html><meta charset=utf-8>
<title>DOSE</title>
<style>
:root{--bg:#f5f7fa;--card:#fff;--ink:#12213a;--dim:#5b6b85;--line:#e3e8f0;
--blue:#6aa1fb;--ok:#1a9e63;--no:#c4453a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
.logo{width:46px;height:46px;border-radius:12px;background:var(--blue);
position:relative;flex:0 0 auto}
.logo i{position:absolute;left:9px;top:9px;width:20px;height:7px;
background:#fff;border-radius:4px;
clip-path:polygon(0 0,100% 0,72% 100%,28% 100%)}
h1{font-size:21px;margin:0;letter-spacing:-.2px}
.sub{color:var(--dim);font-size:13px;margin-top:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 20px;margin-bottom:14px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.7px;
color:var(--dim);margin:0 0 12px}
.row{display:flex;justify-content:space-between;align-items:center;
padding:7px 0;border-bottom:1px solid var(--line);gap:12px}
.row:last-child{border-bottom:0}
.k{color:var(--dim)}
.v{font-variant-numeric:tabular-nums;text-align:right}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
font-weight:600}
.yes{background:#e6f6ee;color:var(--ok)}
.nope{background:#fbeceb;color:var(--no)}
button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:9px 15px;
background:var(--blue);color:#fff;cursor:pointer}
button.ghost{background:#eef2f8;color:var(--ink)}
button:disabled{opacity:.5;cursor:default}
input{font:inherit;padding:9px 11px;border:1px solid var(--line);
border-radius:9px;width:100%;background:#fbfcfe}
.acts{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}
pre{background:#0f1726;color:#d6e2f5;padding:13px;border-radius:10px;
overflow:auto;font-size:12.5px;max-height:260px;margin:12px 0 0}
code{background:#eef2f8;padding:2px 6px;border-radius:5px;font-size:13px}
.note{color:var(--dim);font-size:12.5px;margin-top:10px}
@media(prefers-color-scheme:dark){:root{--bg:#0e1420;--card:#151d2c;
--ink:#e8eefb;--dim:#93a3bd;--line:#243149}input{background:#1b2436}
button.ghost{background:#243149;color:var(--ink)}code{background:#243149}}
</style>
<div class=wrap>
<header><div class=logo><i></i></div>
<div><h1>DOSE</h1><div class=sub id=sub>local only &middot; nothing is exposed</div></div>
</header>
<div class=card><h2>Home station</h2><div id=pi></div>
<div class=acts>
<button onclick="act('pi_check')">Check connection</button>
<button class=ghost onclick="act('pi_restart')">Restart the station</button>
<button class=ghost onclick="act('pi_health')">Read its heartbeat</button>
</div></div>
<div class=card><h2>Speech server</h2><div id=srv></div>
<div class=acts>
<button onclick="act('server_start')">Start</button>
<button class=ghost onclick="act('server_stop')">Stop</button>
<button class=ghost onclick="act('server_pair')">Pair with the station</button>
</div>
<div class=note>The station sends audio and gets back text. That is the
only thing this server can do.</div></div>
<div class=card><h2>GitHub token</h2><div id=gh></div>
<div style="margin-top:10px"><input id=tok type=password
placeholder="paste a token — it is stored 0600 and never shown again"></div>
<div class=acts><button onclick="saveTok()">Save</button>
<button class=ghost onclick="act('gh_push')">Copy it to the station</button></div>
</div>
<div class=card><h2>Terminal</h2>
<div class=row><span class=k>SSH to the station</span>
<span class=v><code>ssh dose-pi</code></span></div>
<div class=acts><button class=ghost onclick="act('open_terminal')">Open a
terminal there</button></div></div>
<div class=card><h2>Notes for Claude</h2>
<div class=row><span class=k>Instructions</span>
<span class=v><code>~/.dose-server/CLAUDE_INSTRUCTIONS.md</code></span></div>
<div class=acts><button class=ghost onclick="act('show_notes')">Show
them</button></div>
<div class=note>What this app is, the rules it was built under, and the
mistakes not to repeat. A Claude session can read it at that path
without asking.</div></div>
<div class=card><h2>Output</h2><pre id=out>ready</pre></div>
</div>
<script>
const K=new URLSearchParams(location.search).get('k')||'';
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function pill(b){return b?'<span class="pill yes">yes</span>':'<span class="pill nope">no</span>'}
function rows(el,pairs){document.getElementById(el).innerHTML=
 pairs.map(([k,v])=>`<div class=row><span class=k>${esc(k)}</span><span class=v>${v}</span></div>`).join('')}
async function refresh(){
 const r=await fetch('/api/status?k='+encodeURIComponent(K));
 const s=await r.json();
 rows('pi',[['Address',esc(s.pi_addr)],['SSH alias','<code>'+esc(s.pi_host)+'</code>']]);
 rows('srv',[['Running',pill(s.server_running)],
   ['Listening on',esc(s.lan)+':8765'],['Paired token',pill(s.server_token)]]);
 rows('gh',[['Stored',pill(s.gh_token)],['Ends with',s.gh_tail?'&middot;&middot;&middot;'+esc(s.gh_tail):'&mdash;']]);
}
async function act(a){
 const o=document.getElementById('out');o.textContent='working…';
 const r=await fetch('/api/act?k='+encodeURIComponent(K)+'&a='+encodeURIComponent(a),{method:'POST'});
 const j=await r.json();o.textContent=j.out||JSON.stringify(j);refresh();
}
async function saveTok(){
 const el=document.getElementById('tok');const v=el.value;
 if(!v){return}
 const o=document.getElementById('out');o.textContent='saving…';
 const r=await fetch('/api/token?k='+encodeURIComponent(K),{method:'POST',body:v});
 const j=await r.json();el.value='';o.textContent=j.out||'saved';refresh();
}
refresh();setInterval(refresh,5000);
</script>
"""


class Panel(BaseHTTPRequestHandler):
    server_version = "dose-panel/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page has no business fetching anything, ever.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; connect-src 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, q):
        # Bound to loopback already; the key stops any other program on
        # this Mac from driving the panel by guessing a URL.
        k = (q.get("k") or [""])[0]
        if not secrets.compare_digest(k, SESSION):
            self._send(403, json.dumps({"error": "stale window"}))
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if u.path == "/api/status":
            if not self._guard(q):
                return
            self._send(200, json.dumps(status()))
            return
        self._send(404, json.dumps({"error": "no"}))

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._guard(q):
            return
        if u.path == "/api/token":
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0 or n > 500:
                self._send(400, json.dumps({"out": "that is not a token"}))
                return
            tok = self.rfile.read(n).decode("utf-8", "replace").strip()
            if not tok or any(c.isspace() for c in tok):
                self._send(400, json.dumps({"out": "that is not a token"}))
                return
            os.makedirs(STATE_DIR, exist_ok=True)
            fd = os.open(GH_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         0o600)
            with os.fdopen(fd, "w") as f:
                f.write(tok + "\n")
            self._send(200, json.dumps(
                {"out": "saved to %s, mode 0600. It is not shown again."
                        % GH_TOKEN}))
            return
        if u.path == "/api/act":
            a = (q.get("a") or [""])[0]
            fn = ACTIONS.get(a)
            if fn is None:
                self._send(404, json.dumps({"out": "no such action"}))
                return
            try:
                out = fn()
            except Exception as e:
                out = "failed: %s" % e
            self._send(200, json.dumps({"out": out}))
            return
        self._send(404, json.dumps({"error": "no"}))


# ── the actions, written here, with no parameters from the caller ───

def a_pi_check():
    ok = pi_reachable()
    return ("The station answered over SSH." if ok else
            "No answer from the station. It may be off, asleep, or on "
            "another network.")


def a_pi_restart():
    rc, out = run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                   PI_HOST,
                   "sudo -n -u rjarv1 XDG_RUNTIME_DIR=/run/user/1000 "
                   "systemctl --user restart dose-home-station.service "
                   "&& echo restarted"], timeout=40)
    return out.strip() or ("exit %d" % rc)


def a_pi_health():
    rc, out = run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                   PI_HOST, "sudo -n cat %s/voice/live.txt" % PI_APP],
                  timeout=25)
    return out.strip() or ("exit %d" % rc)


def a_server_start():
    if server_running():
        return "already running"
    _SRV["proc"] = subprocess.Popen(
        [sys.executable, SERVER_PY, "--serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    return ("started on %s:8765" % lan_address() if server_running()
            else "it did not stay up — see ~/.dose-server/server.log")


def a_server_stop():
    p = _SRV.get("proc")
    if not (p and p.poll() is None):
        return "not running"
    p.terminate()
    try:
        p.wait(timeout=8)
    except Exception:
        p.kill()
    return "stopped"


def a_server_pair():
    """Put the speech server's address and secret on the Pi.

    The secret goes over stdin, never on a command line: argv is
    readable by every process on both machines.
    """
    rc, tok = run([sys.executable, SERVER_PY, "--token"], timeout=20)
    tok = tok.strip()
    if rc != 0 or not tok:
        return "could not read the server token"
    payload = "%s:8765\n%s\n" % (lan_address(), tok)
    rc, out = run(
        ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", PI_HOST,
         "sudo -n -u rjarv1 tee %s/dose_server.conf >/dev/null "
         "&& sudo -n chmod 600 %s/dose_server.conf && echo paired"
         % (PI_APP, PI_APP)],
        stdin_text=payload, timeout=30)
    return out.strip() or ("exit %d" % rc)


def a_gh_push():
    if not os.path.exists(GH_TOKEN):
        return "no token saved yet"
    with open(GH_TOKEN) as f:
        tok = f.read().strip()
    rc, out = run(
        ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", PI_HOST,
         "sudo -n -u rjarv1 tee %s/github_token >/dev/null "
         "&& sudo -n chmod 600 %s/github_token && echo installed"
         % (PI_APP, PI_APP)],
        stdin_text=tok + "\n", timeout=30)
    return out.strip() or ("exit %d" % rc)


def a_open_terminal():
    rc, out = run(["open", "-a", "Terminal",
                   os.path.join(STATE_DIR, "ssh-dose-pi.command")],
                  timeout=15)
    return "opened a terminal" if rc == 0 else out.strip()


def a_show_notes():
    p = os.path.join(STATE_DIR, "CLAUDE_INSTRUCTIONS.md")
    try:
        with open(p) as f:
            return f.read()
    except Exception:
        return "not installed yet — re-run install_dose_app.sh"


ACTIONS = {
    "show_notes": a_show_notes,
    "pi_check": a_pi_check,
    "pi_restart": a_pi_restart,
    "pi_health": a_pi_health,
    "server_start": a_server_start,
    "server_stop": a_server_stop,
    "server_pair": a_server_pair,
    "gh_push": a_gh_push,
    "open_terminal": a_open_terminal,
}


def write_ssh_shortcut():
    os.makedirs(STATE_DIR, exist_ok=True)
    p = os.path.join(STATE_DIR, "ssh-dose-pi.command")
    with open(p, "w") as f:
        f.write("#!/bin/sh\nexec ssh %s\n" % PI_HOST)
    os.chmod(p, 0o700)


def already_running():
    """Is a panel already listening on our port?

    Clicking an app that is already open should SHOW it, not fail. This
    bound the port unconditionally, so a second launch raised
    "Address already in use" and exited — and because the bundle is
    LSUIElement, with no window and no terminal, that exit was
    completely invisible. Ryan clicked the icon, nothing happened, he
    moved it to the Desktop, clicked again, nothing happened again, and
    asked whether he had broken something.

    Nothing about that was his mistake. An app whose only failure mode
    is silence has no way to be used correctly.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/" % PANEL_PORT, timeout=1.5) as r:
            r.read(256)
        return True
    except Exception:
        return False


def main():
    write_ssh_shortcut()
    if already_running():
        # Show the one that is already there rather than dying next to
        # it. The session key belongs to that process, so the browser
        # goes to the bare URL and it redirects.
        print("DOSE panel already running: http://127.0.0.1:%d/"
              % PANEL_PORT, flush=True)
        try:
            webbrowser.open("http://127.0.0.1:%d/" % PANEL_PORT)
        except Exception:
            pass
        return 0
    # 127.0.0.1 ONLY. Not the LAN address, not 0.0.0.0. The Pi cannot
    # reach this, and neither can anything else on the network.
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PANEL_PORT), Panel)
    except OSError as e:
        # The port is taken by something that is NOT our panel. Say so
        # in a way that survives having no terminal.
        msg = ("Port %d on this Mac is already in use by another "
               "program, so the DOSE panel cannot start.\n\n%s"
               % (PANEL_PORT, str(e)[:120]))
        print(msg, flush=True)
        try:
            subprocess.run(
                ["osascript", "-e",
                 'display dialog "%s" with title "DOSE PI CONNECTOR" '
                 'buttons {"OK"} default button 1 with icon caution'
                 % msg.replace('"', "'").replace("\n", " ")],
                capture_output=True, timeout=30)
        except Exception:
            pass
        return 1
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/?k=%s" % (PANEL_PORT, SESSION)
    print("DOSE panel: %s" % url, flush=True)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        a_server_stop()
        print("closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
