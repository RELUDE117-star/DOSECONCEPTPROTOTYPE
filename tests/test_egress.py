"""Nothing private leaves this device, and it only talks to hosts we chose.

A medication dispenser sitting on someone's kitchen counter knows an
unusual amount about them: what medicines they take, when they take
them, and — through a microphone that is always listening — whatever is
said near it. The repository it updates from is PUBLIC, and the audit
feature posts to a public issue tracker by design.

So this suite is not about code style. It is the standing answer to
"could this thing leak?", run on every change:

  1. EGRESS ALLOWLIST. Every outbound host in the codebase is one we
     deliberately chose. A new URL fails the build until someone adds
     it here on purpose.
  2. NO CREDENTIALS IN THE TREE, in any file, ever.
  3. REDACTION ACTUALLY REDACTS. Real report shapes in, nothing
     sensitive out — transcripts, IPs, MACs, usernames, home paths,
     tokens.
  4. THE CLOUD REQUEST CARRIES AUDIO AND NOTHING ELSE — no hostname,
     no device id, no filename that could grow one.
  5. CREDENTIALS ARE READ FROM OUTSIDE THE REPO and are git-ignored.

Run:  python3 tests/test_egress.py
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

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


def sh(*args):
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=120)
        return r.returncode == 0, r.stdout
    except Exception:
        return False, ""


# ── 1. egress allowlist ───────────────────────────────────────────────
# Every host this code may contact, and why. Adding one is a deliberate
# act with a reason attached, not something that slips in with a patch.
ALLOWED_HOSTS = {
    "api.github.com":            "updates + the audit issue post",
    "raw.githubusercontent.com": "update fallback when the API is stale",
    "github.com":                "repository links in docs/comments",
    "api.groq.com":              "free-tier Whisper STT",
    "huggingface.co":            "free ZeroGPU Whisper Space + model files",
    "alphacephei.com":           "Vosk model download",
    "claude.ai":                 "session links in commit trailers",
    "docs.claude.com":           "documentation links",
    "1.1.1.1":                   "TCP connectivity probe (no DNS, no HTTP)",
    "8.8.8.8":                   "TCP connectivity probe (no DNS, no HTTP)",
}

print("== 1. every outbound host is on the allowlist ==")
url_re = re.compile(r"https?://([A-Za-z0-9.\-]+)")
found = {}
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames
                   if d not in (".git", "__pycache__", "node_modules")]
    for fn in filenames:
        if not fn.endswith((".py", ".sh")):
            continue
        p = os.path.join(dirpath, fn)
        try:
            body = open(p, errors="ignore").read()
        except Exception:
            continue
        for host in url_re.findall(body):
            found.setdefault(host, set()).add(
                os.path.relpath(p, ROOT))

unknown = {h: sorted(f) for h, f in found.items()
           if h not in ALLOWED_HOSTS}
ok(not unknown,
   "no unapproved outbound host %s" % (unknown if unknown else ""))
print("   hosts in use: %s" % ", ".join(sorted(found)))

# The connectivity probe must stay a bare TCP connect. If it ever
# becomes an HTTP request it starts carrying headers, and headers carry
# identity.
try:
    import dose_cloud_stt as C
    src = open(os.path.join(ROOT, "dose_cloud_stt.py"),
               errors="ignore").read()
    probe = src[src.find("def is_online"):src.find("def transcribe_groq")]
    ok("create_connection" in probe and "requests" not in probe,
       "the connectivity probe is a bare TCP connect, not an HTTP call")
except Exception as e:
    ok(False, "could not inspect is_online: %s" % e)

# ── 2. no credentials anywhere in the tree ────────────────────────────
print("== 2. no credential-shaped string in any tracked file ==")
SECRET_SHAPES = (
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"github_pat_[A-Za-z0-9_]{40,}",
    r"gsk_[A-Za-z0-9]{30,}",
    r"hf_[A-Za-z0-9]{30,}",
    r"sk-[A-Za-z0-9]{30,}",
    r"AKIA[0-9A-Z]{16}",
)
_, tracked = sh("git", "ls-files")
offenders = []
for f in [x for x in tracked.splitlines() if x.strip()]:
    try:
        body = open(f, errors="ignore").read()
    except Exception:
        continue
    for shape in SECRET_SHAPES:
        # The shapes themselves live in this file and in the redactor.
        if re.search(shape, body) and "SECRET_SHAPES" not in body \
                and "_SECRET_SHAPES" not in body:
            offenders.append((f, shape))
ok(not offenders, "no tracked file carries a credential %s"
   % (offenders[:3] if offenders else ""))

print("== 3. credential files are git-ignored and live outside the repo ==")
for name in ("groq_key", "hf_token", "github_token",
             "cloud_budget.json"):
    good, _ = sh("git", "check-ignore", name)
    ok(good, "%s is git-ignored" % name)
try:
    src = open(os.path.join(ROOT, "dose_cloud_stt.py"),
               errors="ignore").read()
    ok("~/dose-home-station/groq_key" in src
       and "~/.dose_groq_key" in src,
       "credentials are read from the device, never from the repo")
except Exception as e:
    ok(False, "could not check credential paths: %s" % e)

# ── 4. redaction actually redacts ─────────────────────────────────────
print("== 4. the published audit carries nothing sensitive ==")
# The fake tokens are ASSEMBLED AT RUNTIME, never written as literals.
# A test fixture that looks exactly like a credential is itself a
# credential-shaped string sitting in the repository, and it made the
# security audit report a false positive on its own test file. Anything
# that scans for secrets — this suite, the audit script, a CI scanner,
# somebody's grep — should stay quiet on a clean tree.
_FAKE_GH = "gh" + "p_" + "A" * 36
_FAKE_GROQ = "gs" + "k_" + "b" * 44

RAW = """DOSE Home Station — audit
  ssh                    SSH READY — claudeagent@raspberrypi (192.168.4.154)
## RECENT CRASHES
### crash.log (tail)
  File "/home/rjarv1/dose-home-station/dose_app.py", line 10
  token was %s
  Authorization: Bearer %s""" % (_FAKE_GH, _FAKE_GROQ) + """
## EVERY TURN (newest last)
  [OK ] 12:12:38  total 25.19s (end 0.49 fast 17.26 slow 0.0)
       audio : 79.8s  room 1  voice 455  peak 22742  clip 0%  snr 1568.6
       live  : 'what time is it'
       fast  : 'i take lisinopril every morning'
       USED  : 'dispense my blood pressure pills'  (time)
  mac ; b8:27:eb:12:34:56  v6 fe80:0000:0000:0000:0cd2:1055:ed0c:af23
"""
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "da", os.path.join(ROOT, "dose_app.py"))
    # dose_app imports heavy UI deps; pull the classmethod off the source
    # instead of importing the module.
    body = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
    ns = {}
    start = body.find("    _SECRET_SHAPES = (")
    end = body.find("    def audit_report_text")
    snippet = body[start:end]
    snippet = "class R:\n" + snippet
    exec(compile(snippet, "redactor", "exec"), ns)
    redact = ns["R"].redact_for_publication
except Exception as e:
    redact = None
    ok(False, "could not load the redactor: %s" % e)

if redact:
    safe = redact(RAW)
    # The things that must be gone.
    ok("lisinopril" not in safe,
       "a medication name spoken aloud is NOT published")
    ok("blood pressure" not in safe,
       "what the person asked for is NOT published")
    ok("what time is it" not in safe, "transcripts are withheld")
    ok("192.168.4.154" not in safe, "the LAN address is removed")
    ok("raspberrypi" not in safe, "the hostname is removed")
    ok("claudeagent" not in safe, "the SSH account is removed")
    ok("rjarv1" not in safe, "the OS username is removed")
    ok("/home/" not in safe, "absolute home paths are removed")
    ok("b8:27:eb:12:34:56" not in safe, "the MAC address is removed")
    ok("fe80:0000" not in safe, "the IPv6 address is removed")
    ok(_FAKE_GH not in safe, "a GitHub token is removed")
    ok(_FAKE_GROQ not in safe, "a Groq key is removed")
    # The things that must SURVIVE, or the report stops being useful.
    ok("total 25.19s" in safe, "turn timings survive")
    ok("snr 1568.6" in safe, "audio measurements survive")
    ok("RECENT CRASHES" in safe, "structure survives")
    ok("<transcript withheld>" in safe,
       "withheld transcripts are marked, not silently dropped")

    # Redaction must be idempotent — running it twice changes nothing.
    ok(redact(safe) == safe, "redaction is idempotent")

    # And it must not crash on junk.
    for junk in ("", None, "no secrets here"):
        try:
            redact(junk)
            ok(True, "redactor survives %r" % (junk,))
        except Exception as e:
            ok(False, "redactor raised on %r: %s" % (junk, e))

# ── 5. the cloud request carries audio and nothing else ───────────────
print("== 5. the cloud upload carries audio and nothing else ==")
try:
    src = open(os.path.join(ROOT, "dose_cloud_stt.py"),
               errors="ignore").read()
    groq = src[src.find("def transcribe_groq"):
               src.find("# ── Hugging Face")]
    ok('("audio.wav"' in groq,
       "a fixed filename is sent, never the local path or temp name")
    for leak in ("gethostname", "platform.node", "uuid.getnode",
                 "getpass", "os.getlogin", "machine-id"):
        ok(leak not in groq,
           "the request does not include %s" % leak)
    # Only these fields may be sent alongside the audio.
    fields = set(re.findall(r'data\["?([a-z_]+)"?\]', groq)) | \
        set(re.findall(r'"([a-z_]+)":', groq))
    allowed_fields = {"model", "response_format", "temperature",
                      "language", "file"}
    extra = fields - allowed_fields
    ok(not extra, "no unexpected fields in the request %s"
       % (sorted(extra) if extra else ""))
except Exception as e:
    ok(False, "could not inspect the Groq request: %s" % e)

print()
print("egress/privacy suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== NOTHING PRIVATE LEAVES THE DEVICE ===")
