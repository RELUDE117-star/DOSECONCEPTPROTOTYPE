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
# NOT A FIXED SLICE. This was `[:1200]` and a docstring paragraph
# pushed the code it checks out of the window — the exact trap
# CLAUDE.md records under "four test assertions broke and all four
# were the test's fault". Take the function, and stop at the next one.
pf = vsrc.split("def _purge_foreign_cache")[1]
pf = pf[:pf.index("\n    def ")]
ok('stamp' in pf and '"*.wav"' in pf,
   "the clip cache is stamped with the voice that rendered it")
ok("if was != now:" in pf,
   "and every clip is deleted the moment that stamp doesn't match")

# AN UNRESOLVED VOICE IS NOT A DIFFERENT VOICE.
#
# `_piper_path` is filled in by the preflight, which has not
# necessarily run when this does, so `now` could be "" — and then
# every stamp differed from it and the whole cache went. The device
# counted it twice in one evening: 61 clips -> 35, and 116 -> 22
# sixty seconds after a restart. Every restart threw the prewarm away
# and re-rendered it, in minutes of synthesis at 68 C, for a
# directory whose contents were perfectly good.
ok(pf.index("if not self._piper_path:") < pf.index("if was != now:"),
   "the voice is resolved before anything is compared")
ok("_cache_purge_skipped" in pf
   and pf.index("_cache_purge_skipped") < pf.index("if was != now:"),
   "and an unknown voice leaves the cache alone instead of wiping it")
ok(pf.index("return") < pf.index("if was != now:"),
   "it returns rather than falling through to the delete loop")

# NOT EVERY .onnx IN THE VOICE FOLDER IS A VOICE.
#
# The retirement sweep globbed *.onnx and deleted anything that was
# not hers. silero_vad.onnx lives in the same directory. So on EVERY
# LAUNCH it deleted the voice-activity model, concluded a voice had
# been retired, and wiped the whole pre-rendered reply cache. The
# device's own purge log caught it twice in two minutes:
#
#   17:24:28  retired silero_vad.onnx  125 clips
#   17:26:10  retired silero_vad.onnx   62 clips
#
# The cache was the symptom. The real cost was quieter: the VAD model
# was re-downloaded every boot, so a station with no internet ran with
# no voice-activity detection, having deleted a model it already had.
rt = src.split("def _retire_other_voices")[1]
rt = rt[:rt.index("\n    def ")]
ok("NON_VOICE_MODELS" in rt,
   "the models that are not voices are named, not guessed at")
ok("silero_vad.onnx" in src,
   "...starting with the one that was being deleted every launch")
ok('stem + ".json"' in rt or "stem+\".json\"" in rt,
   "a Piper voice is a .onnx WITH a .onnx.json beside it; nothing "
   "else in that folder has one")
ok(rt.index("NON_VOICE_MODELS") < rt.index("os.unlink"),
   "and both tests run BEFORE anything is deleted")
ok("silero_vad.onnx) continue" in sh or "silero_vad.onnx)" in sh,
   "DOSE.sh's retirement loop had the same bug and the same fix")
ok('[ -e "$STEM.json" ] || continue' in sh,
   "...including the config-file test")

# The behaviour itself, not just its shape: retire a real voice, keep
# the VAD model, and only wipe clips when a voice actually went.
import ast                                                 # noqa: E402
import glob                                                # noqa: E402
import tempfile as _tf                                     # noqa: E402
import textwrap as _tw                                     # noqa: E402
_d = _tf.mkdtemp()
for _n in ("en_US-hfc_female-medium.onnx",
           "en_US-hfc_female-medium.onnx.json",
           "silero_vad.onnx",
           "en_US-amy-low.onnx", "en_US-amy-low.onnx.json"):
    open(os.path.join(_d, _n), "w").close()
os.makedirs(os.path.join(_d, "cache"))
for _i in range(5):
    open(os.path.join(_d, "cache", "say_%d.wav" % _i), "w").close()
_tree = ast.parse(src)
_fn = [n for n in ast.walk(_tree)
       if isinstance(n, ast.FunctionDef)
       and n.name == "_retire_other_voices"][0]
_body = _tw.dedent("\n".join(src.splitlines()[_fn.lineno - 1:_fn.end_lineno]))
_ns = {"os": os}
exec("class _A:\n"
     "    VOICE_NAME = 'en_US-hfc_female-medium'\n"
     "    NON_VOICE_MODELS = ('silero_vad.onnx',)\n"
     + _tw.indent(_body, "    "), _ns)
_removed = _ns["_A"]()._retire_other_voices(_d)
_left = sorted(f for f in os.listdir(_d) if f.endswith((".onnx", ".json")))
ok("silero_vad.onnx" in _left,
   "the VAD model SURVIVES a retirement sweep")
ok("en_US-hfc_female-medium.onnx" in _left, "and so does her voice")
ok("en_US-amy-low.onnx" in _removed,
   "while a real foreign voice is still retired on sight")
ok(len(glob.glob(os.path.join(_d, "cache", "*.wav"))) == 0,
   "clips rendered in that voice still go with it")
ok(os.path.exists(os.path.join(_d, "cache_purges.log")),
   "and the deleter signs the log, so the next person does not have "
   "to guess which of three it was")

_d2 = _tf.mkdtemp()
for _n in ("en_US-hfc_female-medium.onnx",
           "en_US-hfc_female-medium.onnx.json", "silero_vad.onnx"):
    open(os.path.join(_d2, _n), "w").close()
os.makedirs(os.path.join(_d2, "cache"))
for _i in range(7):
    open(os.path.join(_d2, "cache", "say_%d.wav" % _i), "w").close()
ok(_ns["_A"]()._retire_other_voices(_d2) == [],
   "nothing is retired when only her voice and the VAD model are "
   "present")
ok(len(glob.glob(os.path.join(_d2, "cache", "*.wav"))) == 7,
   "AND THE CACHE SURVIVES — this is the whole bug, in one check")

# (d) she is deleted on sight, before any download
mg = src.split("def migrate_voice")[1].split(
    "def _voice_download_models")[0]
ok(mg.index("_retire_other_voices") < mg.index("os.path.exists(mine)"),
   "other voices are removed BEFORE checking whether hers is present")
ok("ON SIGHT" in mg, "deliberately, not as a side effect")
# NOT A FIXED SLICE — fourth time this trap has fired in this project.
# It was [:900] and the comment explaining the silero_vad fix pushed
# the code it checks past the window. Take the block, ending where the
# next section starts.
sh_seg = sh.split("One voice, always hers")[1]
sh_seg = sh_seg[:sh_seg.index("# ── Update, every launch")]
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

print("== 8. two recognisers, with distinct jobs ==")
# Whisper was removed for speed and is back for accuracy. It is not a
# competing recogniser: it only runs when the fast one returns
# something that does not parse.
ok("faster_whisper" in vsrc,
   "faster-whisper is the fast path and the escalation")
ok("moonshine" in vsrc.lower(),
   "moonshine is still available if it wins the race on a board")
bt = vsrc.split("def _better_transcribe")[1].split("\n    def ")[0]
ok(bt.index("_fast_transcribe") < bt.index("_whisper_transcribe"),
   "the fast recogniser answers first")
ok("self._usable(fast)" in bt,
   "and the base.en model escalates only when that answer is unusable")
ok(vsrc.index("base.en") < vsrc.index("distil-small.en"),
   "with base.en as the default — the bigger models take SECONDS per "
   "utterance on a Pi 4")
ok("faster-whisper" in sh, "the setup script installs it")
ok("faster-whisper" in src, "so does the app's dependency list")

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
ok(hosts <= {"huggingface.co", "alphacephei.com",
             "raw.githubusercontent.com"},
   "models come from three free, open hosts — her voice, the live "
   "listener, and the voice detector (found %s)"
   % ", ".join(sorted(hosts)))
ok("raw.githubusercontent.com" in hosts or "silero" not in sh.lower(),
   "the voice detector is a plain file from GitHub, not a pip "
   "package that would drag in torch")

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

_fast_row_ctx = vsrc.split('rows.append(("Speech (fast)"')[0][-900:]
ok("_ms_reason" in _fast_row_ctx
   and "_whisper_fast_size" in _fast_row_ctx,
   "Settings shows the real reason on the Speech row — for whichever "
   "engine leads, never blank")
ok('"Speech (backup)"' in vsrc and '"Speech (fast)"' in vsrc,
   "and names the two recognisers separately — 'fast' does the "
   "hearing, 'backup' escalates — so which one is working is never "
   "a guess")
ok("loads when needed" in vsrc,
   "the backup says it is not loaded rather than claiming to be ready")
ok("download failed" in vsrc,
   "and stops claiming 'downloading' once it plainly is not")

sh_dl = sh.split("Speech model: finish downloading")[1][:4000]
ok("WhisperModel(" in sh_dl,
   "the setup script downloads the MAIN recogniser before the app "
   "opens")
ok("get_model_for_language" in sh_dl,
   "and the fast one too")
ok(sh.index("Speech model: finish downloading")
   < sh.index("Starting DOSE"),
   "before launch, not after")
ok(sh.index("Speech model: finish downloading")
   > sh.index(".bt_ready3"),
   "and outside the one-time setup block, so a failed download is "
   "retried on the next launch")
ok("hasattr(mv.ModelArch" in sh_dl,
   "with the same fallback through available model types")
ok("DOSE_WHISPER_MODELS" in sh_dl,
   "and the same Whisper chain the app uses, so they cannot "
   "disagree and make the first sentence pay for a download")
ok("sys.exit(0)" in sh_dl,
   "a failure never blocks the app from starting")

print()
print("migration suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== MIGRATION: ALL PASSED (one voice, and it actually lands) ===")
