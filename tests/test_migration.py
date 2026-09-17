"""THE AMY MIGRATION — the bug that made the last update do nothing.

The previous release removed Amy from the code, and on a fresh install
that worked. On a station that ALREADY had Amy it did nothing at all,
because the download and the cleanup both lived inside the "voice
model missing" branch — and a station with Amy on disk isn't missing a
voice. It probed as perfectly healthy and kept the slow voice forever.

That is the failure this suite exists to prevent. Every check below
runs against a directory seeded exactly like the affected device.

The rule now: ONE voice, checked BY NAME, on EVERY launch. Any other
.onnx is not a fallback — it is something to replace.
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


sys.modules.setdefault("tkinter", types.ModuleType("tkinter"))
sys.modules["tkinter"].font = types.ModuleType("tkinter.font")
sys.modules.setdefault("tkinter.font", sys.modules["tkinter"].font)
import importlib.util                                      # noqa: E402

spec = importlib.util.spec_from_file_location(
    "dose_app_mig", os.path.join(ROOT, "dose_app.py"))
da = importlib.util.module_from_spec(spec)
spec.loader.exec_module(da)
import dose_voice as dv                                    # noqa: E402

VOICE = da.DoseApp.VOICE_NAME
ONNX_MAGIC = b"\x08" + b"\x00" * 20


def seed(*names, cache=0):
    """A voice directory exactly like a real device's."""
    d = tempfile.mkdtemp(prefix="dose_mig_")
    for n in names:
        with open(os.path.join(d, n + ".onnx"), "wb") as f:
            f.write(ONNX_MAGIC + b"x" * 1000)
        with open(os.path.join(d, n + ".onnx.json"), "w") as f:
            f.write("{}")
    os.makedirs(os.path.join(d, "cache"), exist_ok=True)
    for i in range(cache):
        open(os.path.join(d, "cache", "say_%d.wav" % i), "wb").close()
    return d


def names(d):
    return sorted(f for f in os.listdir(d) if f.endswith(".onnx"))


def app():
    a = object.__new__(da.DoseApp)
    a._swap_voice_engine = lambda: None
    return a


print("== 1. the exact device that got stuck ==")
# Amy installed, working, nothing "missing" — the old code's blind spot
d = seed("en_US-amy-medium", VOICE, cache=4)
gone = app()._retire_other_voices(d)
ok(names(d) == [VOICE + ".onnx"],
   "Amy is removed once her voice is present (left: %s)" % names(d))
ok(any("amy" in g for g in gone), "and it is reported as removed")
ok(not os.listdir(os.path.join(d, "cache")),
   "clips pre-rendered in the old voice are dropped too")
ok(os.path.exists(os.path.join(d, VOICE + ".onnx.json")),
   "her config file is kept")

print("== 2. her voice is fetched, and the fetch is retried ==")
# Silence is the correct state while waiting — NOT the old voice.
d = seed("en_US-amy-medium", cache=2)
app()._retire_other_voices(d)
ok(names(d) == [],
   "with only an old voice on disk, it is removed and nothing is left "
   "to speak with (silent, never the wrong voice)")
src = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
mig = src.split("def migrate_voice")[1].split("def _voice_download_models")[0]
ok("_voice_download_models(force=True)" in mig,
   "a station without her voice downloads it")
ok("_voice_dl_tries" in src, "and a failed download is retried")
dl = src.split("HER VOICE — exactly one file")[1][:2000]
ok(dl.index("_retire_other_voices") > dl.index("if os.path.exists(onx)"),
   "the downloader tidies up only after a successful download")

print("== 3. the migration is not gated on anything being missing ==")
boot = src.split("Voice assistant (\"Hey Dose\")")[1][:900]
ok("self.migrate_voice" in boot,
   "it runs at every launch, not only when a model is missing")
ok("voice_enabled" in boot, "under the normal voice-enabled setting")
sh = open(os.path.join(ROOT, "DOSE.sh"), errors="ignore").read()
ok("One voice, always hers" in sh,
   "the setup script has the same unconditional step")
# and it must sit OUTSIDE the "models missing" block
gate = sh.index("# Voice models: keyed on the actual files")
ok(sh.index("One voice, always hers") > gate,
   "placed after the models-missing block, not inside it")
ok(sh.index("One voice, always hers") > sh.index("Voice installed:"),
   "and after the download, so it never deletes the only voice")

print("== 4. there is exactly ONE voice ==")
ok(dv.VOICE_NAME == VOICE, "engine and app agree on the name (%s)" % VOICE)
vsrc = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
ok("VOICE_PREFERENCE" not in vsrc, "no list of alternative voices")
ok("_synth_moonshine" not in vsrc, "no second synthesis engine")
ok("kokoro" not in vsrc.lower().replace("kokoro_af_heart", "")
   or "Kokoro and the" in vsrc,
   "Kokoro is not used as a voice")
ok("VOICE_CANDIDATES" not in src, "the app fetches one voice by name")
ok(src.count('VOICE_SUB = "en/en_US/hfc_female/medium"') == 1,
   "from one path")

print("== 5. a stranger voice is replaced, not adopted ==")
# some other Piper voice on disk must NOT be used as if it were hers
probe = vsrc.split("HER voice, or none")[1][:700]
ok('self.reason = "voice model missing"' in probe,
   "an unrecognised .onnx counts as MISSING, so hers gets fetched")
ok("VOICE_NAME in os.path.basename" in probe,
   "the voice is matched by name, never by 'whatever is first'")

d = seed("en_US-lessac-medium", VOICE)
app()._retire_other_voices(d)
ok(names(d) == [VOICE + ".onnx"],
   "any other voice is retired, not just Amy (left: %s)" % names(d))

print("== 6. it is safe to run over and over ==")
d = seed(VOICE, cache=3)
for _ in range(5):
    gone = app()._retire_other_voices(d)
ok(names(d) == [VOICE + ".onnx"], "her voice survives repeated runs")
ok(gone == [], "nothing is reported removed when there is nothing to do")
ok(len(os.listdir(os.path.join(d, "cache"))) == 3,
   "and the pre-rendered clips are NOT wiped every launch")

print("== 7. AMY IS NEVER AUDIBLE — not even one sentence ==")
# Three separate ways she could still be heard. All three are closed.

# (a) the engine refuses to load a voice it doesn't recognise, so a
#     station holding only Amy is SILENT rather than speaking as her
probe = vsrc.split("HER voice, or none")[1][:700]
ok('self.reason = "voice model missing"' in probe,
   "with only Amy on disk the engine reports a MISSING voice...")
after = vsrc.split("HER voice, or none")[1]
ok(after.index("self.available = True") > after.index(
    'self.reason = "voice model missing"'),
   "...and never reaches available=True, so nothing is ever spoken")

# (b) the pre-rendered clips. These are played straight from disk, so
#     a clip rendered in her voice would be heard no matter what the
#     engine decided. The cache key must include the voice FILE.
ck = vsrc.split("def _cache_path")[1][:600]
ok("self._piper_path" in ck,
   "every cached clip is keyed by the voice file that made it")
acks = vsrc.split("def _prerender")[1][:1600] if "def _prerender" in vsrc \
    else vsrc.split("Load Piper up front")[1][:1600]
ok("hashlib.md5" not in acks,
   "the 'Hey Dose' acknowledgements no longer use a text-only key")
ok("self.render_to_cache(line)" in acks,
   "they go through the same voice-keyed cache as everything else")
ok("_purge_foreign_cache" in acks,
   "and a cache from another voice is purged before anything plays")

# (c) belt and braces: a stamp file, so even a half-run migration
#     cannot leave a clip of hers behind
pf = vsrc.split("def _purge_foreign_cache")[1][:1200]
ok('stamp' in pf and '"*.wav"' in pf,
   "the clip cache is stamped with the voice that rendered it")
ok("if was != now:" in pf,
   "and every clip is deleted the moment that stamp doesn't match")

# (d) she is deleted on sight, before any download
mg = src.split("def migrate_voice")[1].split(
    "def _voice_download_models")[0]
ok(mg.index("_retire_other_voices") < mg.index("os.path.exists(mine)"),
   "other voices are removed BEFORE checking whether hers is present")
ok("ON SIGHT" in mg, "deliberately, not as a side effect")
sh_seg = sh.split("One voice, always hers")[1][:900]
ok("RETIRED=" in sh_seg and 'if [ -f "$VOICE_DIR/$V_NAME.onnx" ]; then'
   not in sh_seg,
   "the setup script deletes her unconditionally too")

# and the end-to-end shape: Amy present, hers absent -> Amy gone
d = seed("en_US-amy-medium", cache=5)
app()._retire_other_voices(d)
ok(names(d) == [],
   "Amy is removed even when hers has not downloaded yet "
   "(the station stays silent instead of speaking as her)")
ok(not os.listdir(os.path.join(d, "cache")),
   "and every clip she rendered goes with her")

print("== 8. one recogniser ==")
ok("faster_whisper" not in vsrc.replace("faster-whisper", ""),
   "faster-whisper is gone from the engine")
ok("faster-whisper" not in sh, "and from the setup script")
ok("faster_whisper" not in src.replace("faster-whisper", ""),
   "and from the app's dependency list")
ok("moonshine" in vsrc.lower(), "Moonshine does the hearing")

print("== 9. exactly ONE voice model is ever downloaded ==")
# Pin the source. The voice is the HuggingFace Piper hfc_female
# medium checkpoint and nothing else — chosen because on a Pi 4 it
# synthesizes about 3x faster than real time (RTF ~0.15) while Kokoro
# is ~0.48 and Amy is slower still. Speed here IS reliability: a voice
# that renders faster than it plays can never fall behind mid-sentence.
import re as _re                                           # noqa: E402

URL_RX = r'''https?://[^\s"')]+'''

for blob, who in ((src, "dose_app.py"), (sh, "DOSE.sh")):
    # the URL is assembled from pieces, so check the pieces
    ok(blob.count("piper-voices/resolve") == 1,
       "%s builds exactly one Piper voice URL (found %d)"
       % (who, blob.count("piper-voices/resolve")))
    ok("huggingface.co/rhasspy/" in blob,
       "%s: the voice comes from HuggingFace rhasspy/piper-voices"
       % who)
    ok(blob.count("hfc_female") >= 1
       and not _re.search(r"en_US-(?!hfc_female)[a-z_]+-(low|medium|high)",
                          blob),
       "%s names no voice checkpoint other than hers" % who)
    ok("/amy/" not in blob and "amy-medium" not in blob
       and "amy-low" not in blob,
       "%s downloads no Amy checkpoint" % who)
    ok("kokoro" not in blob.lower(),
       "%s downloads no Kokoro checkpoint" % who)

ok("hfc_female/medium" in src and "hfc_female/medium" in sh,
   "and it is specifically the hfc_female MEDIUM checkpoint")
ok(da.DoseApp.VOICE_SUB == "en/en_US/hfc_female/medium",
   "the app's path points at that checkpoint")
ok(da.DoseApp.VOICE_NAME == dv.VOICE_NAME == "en_US-hfc_female-medium",
   "app and engine agree on the file name")

# the only OTHER model the station fetches is the live listener
hosts = set()
for blob in (src, sh):
    for u in _re.findall(URL_RX, blob):
        h = u.split("//", 1)[-1].split("/")[0].lower()
        if any(k in u for k in ("model", "voice", ".zip", ".onnx")):
            hosts.add(h)
ok(hosts <= {"huggingface.co", "alphacephei.com"},
   "models come from exactly two free hosts: her voice and the live "
   "listener (found %s)" % ", ".join(sorted(hosts)))

print("== 10. 'Speech: downloading...' must not be a dead end ==")
# Reported from the device: Speech sat on "downloading..." forever.
# The loader asked for ONE exact arch name and swallowed every error,
# so a package whose build lacks that name — or a download that never
# finished — looked identical to one still in progress.
loader = vsrc.split("def _moonshine_v2")[1].split("\n    def ")[0]
ok("hasattr(mv.ModelArch" in loader,
   "it checks which model types the installed package actually has")
ok(loader.count("except Exception") >= 2 and "self._ms_reason" in loader,
   "and records WHY it failed instead of swallowing it")
ok("for arch_name in available" in loader,
   "it falls back through the available types rather than giving up")
ok("library not installed" in loader,
   "a missing library is reported as that, not as 'downloading'")

status = vsrc.split('rows.append(("Speech"')[0][-900:]
ok("_ms_reason" in status,
   "Settings shows the real reason on the Speech row")
ok("download failed" in vsrc,
   "and stops claiming 'downloading' once it plainly is not")

sh_dl = sh.split("Speech model: finish downloading")[1][:2000]
ok("get_model_for_language" in sh_dl,
   "the setup script downloads it BEFORE the app opens")
ok(sh.index("Speech model: finish downloading")
   < sh.index("Starting DOSE"),
   "before launch, not after")
ok(sh.index("Speech model: finish downloading")
   > sh.index(".bt_ready3"),
   "and outside the one-time setup block, so a failed download is "
   "retried on the next launch")
ok("hasattr(mv.ModelArch" in sh_dl,
   "with the same fallback through available model types")
ok("sys.exit(0)" in sh_dl,
   "a failure never blocks the app from starting")

print()
print("migration suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== MIGRATION: ALL PASSED (one voice, and it actually lands) ===")
