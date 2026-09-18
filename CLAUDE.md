# DOSE Home Station — developer notes for Claude Code

Durable facts for future sessions. **No secrets in this file, ever.**

## Target device
- Raspberry Pi 4B, 4 GB RAM, 800×480 touchscreen (kiosk).
- USB mic: **AIRHUG** (enumerates as `card 5,0 A28 AIRHU` in recent audits — confirm with `arecord -l`).
- USB speaker (separate from the mic). Audio via **PipeWire/PulseAudio** over ALSA.

## Direct access (VERIFIED ON DEVICE 2026-09-17 — read this before believing older notes)
- `ssh dose-pi` — **verified working**: logs in as `claudeagent`, key auth, no password.
- **`claudeagent` HAS passwordless sudo** (`/etc/sudoers.d/90-claudeagent`),
  installed by the bootstrap on 2026-09-17. Verify with `sudo -n true`.
- **Why it was missing for so long, and the lesson:** the bootstrap HAD run —
  it created the account, the groups and the key — but `configure_dev_sudo()`
  opened with `if mode != "root" and not is_root(): return "needs root"`, while
  every other step used `run_priv()`, which routes through `sudo -n`. The app
  runs as the kiosk user, which has passwordless sudo, so the bootstrap ran in
  `"sudo"` mode and succeeded at everything *except* the sudoers drop-in. One
  over-strict guard cost a whole debugging session its access. Fixed, and
  `BOOTSTRAP_VERSION` bumped to 2 so `main()` re-runs past its
  "already complete" early return on existing installs.
- **`/home/rjarv1` is mode `0700`.** Without sudo, `claudeagent` cannot read the
  app directory, its logs, its config, or its user-site packages — it CAN read
  `/proc`, `/proc/asound`, run `amixer`/`arecord`, and see `dmesg`. This is why
  a missing sudoers file is not a cosmetic problem.
  Groups: `audio video plugdev gpio i2c spi render dialout`.
- **The kiosk user has passwordless sudo**, which is what makes the bootstrap
  self-sufficient: no typing on the Pi's touchscreen is ever required. Delivery
  is `Settings → Update Software`; `dose_app.py` re-fetches the bootstrap in
  `_run_claude_bootstrap()` 9 s after each launch.
- **Every `claudeagent` SSH login starts its own PipeWire + WirePlumber**
  (`user@1001`), which can contend with the kiosk session's for the USB audio
  devices. They are now masked for that account so diagnostics cannot perturb
  the app's audio. Undo with `systemctl --user unmask pipewire.service
  pipewire.socket pipewire-pulse.service pipewire-pulse.socket
  wireplumber.service` if `claudeagent` ever needs audio of its own.
- **A cloud Claude session cannot reach the Pi at all** (no LAN: ICMP drops,
  TCP/22 refused, `~/.ssh` unreachable, no mDNS). The working pattern is a
  queue-runner on Ryan's Mac: `~/Documents/dose-agent/runner.sh` watches
  `queue/` for `*.sh`, runs each in the real macOS shell (so `ssh dose-pi`
  works) and writes full output to `out/<name>.out`, which the cloud session
  reads through the connected-folder bridge. Start it with
  `bash ~/Documents/dose-agent/runner.sh` and leave that window alone.
  Pushes also go through that Mac checkout (`~/Documents/dose-agent/repo`),
  because the cloud session's git proxy refuses this repo.
- Device: hostname **`raspberrypi`** (`raspberrypi.local`), LAN IPv4
  **`192.168.4.154`** (DHCP — prefer the `.local` name).
- Pi host key fingerprint (first seen): `SHA256:ySNWexiarII8Tlbedsc2UYeZnOgDmLAu5ZCXxu6GwuU`.
- **IMPORTANT — two different homes on this machine.** The voice app runs as
  **`rjarv1`**, so the application lives at **`/home/rjarv1/dose-home-station`**,
  NOT under `/home/claudeagent`. Over SSH always use the absolute path; `~`
  as `claudeagent` is the wrong directory. Bootstrap status is at
  `/home/rjarv1/dose-home-station/claude-bootstrap-status.json` (or
  `/var/lib/dose-claude-bootstrap/status.json` if it ran as root).
- SSH alias: **`dose-pi`** → user `claudeagent`, key `~/.ssh/dose_pi_claude_ed25519` (Mac only).
- Authorised dev key: Ed25519, fingerprint
  `SHA256:sNBB8HJA4yPXgAbXSGyFNJunODwia11ILumD/dvB2Hk` (Ryan's MacBook Pro).
  Public half is in `tools/claude_dev_authorized_keys`; the private half
  exists only on that Mac and must never leave it.
- Mac `~/.ssh/config` entry to append (do not disturb other hosts):
  ```
  Host dose-pi
      HostName <pi-hostname>.local        # or the LAN IP from the bootstrap status
      User claudeagent
      IdentityFile ~/.ssh/dose_pi_claude_ed25519
      IdentitiesOnly yes
      ServerAliveInterval 30
      ServerAliveCountMax 4
  ```
- Finding the Pi from the Mac (LAN only; no router port is opened):
  `dns-sd -B _ssh._tcp` , `ping raspberrypi.local`, or read
  `~/dose-home-station/claude-bootstrap-status.json` on the device.
- The dedicated account, key-only SSH, and dev sudo are provisioned by
  `tools/bootstrap_claude_access.py`, which runs on the Pi after a normal
  GitHub update (idempotent; state in `/var/lib/dose-claude-bootstrap/` or
  `~/.local/state/dose-claude-bootstrap/`; sanitized status in
  `~/dose-home-station/claude-bootstrap-status.json`).
- The Pi's public key file for dev access: `tools/claude_dev_authorized_keys`
  (PUBLIC keys only — safe to commit; the bootstrap installs them).

## Application layout
- App dir on device: `~/dose-home-station` (`APP_DIR`).
- Entry point: `dose_app.py` (Tkinter UI). Voice engine: `dose_voice.py`.
  NLU: `dose_nlu.py`. Cloud STT: `dose_cloud_stt.py`. Setup/launch: `DOSE.sh`.
  Diagnostics: `voice_diagnostics.py`.
- Updater: `dose_app.py` `_do_update_check` pulls files via the **GitHub API**
  (`api.github.com/.../contents/<f>?ref=<BRANCH>`, `Accept: vnd.github.raw`)
  with raw.githubusercontent fallback. Applies + restarts via `os.execv`.
  **It now syncs the WHOLE BRANCH**, not a fixed list. `_repo_file_list()`
  resolves `refs/heads/<branch>` to a SHA and pulls one recursive tree — two
  API calls total — and each entry's blob SHA lets the device skip anything it
  already has byte for byte, so a repeat update costs those same two calls.
  Device-owned state (`github_token`, `med_data.json`, `calibration.json`,
  `learning.json`, `adherence_log.json`, the logs) is never overwritten;
  `voice/`, `.git/`, `__pycache__/` are skipped; >2 MB is skipped; absolute or
  `..` paths are rejected and the write refuses anything outside `APP_DIR`;
  Python extras must compile before landing. Falls back to the historical
  fixed set if the tree API is unreachable.
  *(Previously it fetched only six hardcoded files, which is why
  `voice_diagnostics.py`, `dose_cloud_stt.py` and everything under `tools/`
  never reached a device and had to be hand-carried over SSH.)*
- Monitored branch: **`claude/quirky-brown-vkHwi`** (see `BRANCH` in dose_app.py).
- Repo: `relude117-star/doseconceptprototype` (public). GitHub token, if any,
  lives ONLY on the device at `~/dose-home-station/github_token` (git-ignored).
- `DOSE_DISABLE_SELF_INSTALL=1` disables auto-update, dep-install, and the
  bootstrap — use it when running a working tree you don't want overwritten.

## Voice pipeline (current design)
- Capture → energy gate + **Silero VAD** (ONNX, fetched, fail-open) → recogniser.
- STT: **cloud primary when a free credential is present** (Groq key or HF token;
  `dose_cloud_stt.py`), else **local**: faster-whisper `tiny.en` (fast) with
  `base.en` escalation on low confidence. Moonshine is retired from the runtime.
- Live on-screen text: **Vosk**. TTS: **Piper** `en_US-hfc_female-medium`.
- Capture gain: auto-levelling watchdog, symmetric (steps down on clipping/hot,
  up when too quiet); default level `DEFAULT_CAPTURE_LEVEL` (70%).
- Endpointing is adaptive; speculative transcription overlaps the pause.
- **PIN THE MICROPHONE: `DOSE_MIC_CARD=5,0`.** This station exposes TWO USB
  capture devices — card 5 is the AIRHUG (the real mic), card 4 is a "USB
  Composite Device" that PipeWire reports as `mono-fallback 8000Hz` and which
  hears essentially nothing. Selection was inferred at runtime and got it
  wrong: an orphaned `arecord -D plughw:4,0` was found on the device, left by
  a crashed instance recording from the wrong card. The systemd unit now sets
  the pin. Verify with `arecord -l` if the hardware changes.
- **Cloud STT economy** (`dose_cloud_stt.py`): audio is downmixed to 16 kHz
  mono before upload — the mic only does 48 kHz stereo and Whisper resamples
  to 16 k mono anyway, so the old path sent **6× the bytes** for an identical
  result (measured 576,044 → 96,044 on a 3 s turn). Plus: min 0.4 s, max 20 s,
  12 requests/minute, 600/day, and a 10-minute account-wide cooldown after a
  429. The budget is PERSISTED (`voice/cloud_budget.json`) so restarts cannot
  reset the daily count.

## Hard-won device facts (2026-09-17 audit — do not re-derive these)
- **Python on the Pi is 3.13.5**, which removed stdlib `audioop` — but the
  app HAS the real thing: a running fault dump lists `audioop._audioop` among
  its loaded extension modules, so the backport is installed in the KIOSK
  user's site-packages. The pure-Python shim is dormant fallback, as designed.
  **Measure from the app's interpreter, not the dev account's.**
  `python3 -c "import audioop"` fails as `claudeagent` and that misled an
  entire line of investigation. (For reference, the shim WOULD cost ~3.0% of a
  core — ratecv 2.1%, rms 0.9% — so it was never the CPU hog either way.)
- **The CPU hog was Piper's ONNX session.** `py-spy` on the live process:
  `tts-prewarm` → `phoneme_ids_to_audio` → onnxruntime, with three native ORT
  workers at ~100% each — **392-398% of one core at 73.5 °C**, firing exactly
  when the capture stream comes up. `INFER_THREADS` was already 2 and
  `OMP_NUM_THREADS` exported to match; neither matters, because a stock
  onnxruntime wheel is NOT built with OpenMP and the only lever is
  `SessionOptions.intra_op_num_threads`, which piper leaves at 0.
  `_cap_onnx_threads()` fills it in. **392% → 111% of one core.**
- **QR scanning WAS the steady-state CPU cost:** `pyzbar.decode` in
  `_camera_loop` held ~84% of a core continuously, competing with audio.
  It is now duty-cycled (`QR_SCAN_INTERVAL`, 300 s, bursting
  `QR_BURST_FRAMES` at a time) and idles with no capture, no decode and
  no colour convert in between. The camera view, `request_qr_scan()`
  and the first pass after launch all still scan immediately. **Do not
  cite the 84% figure as current** — it describes the old loop.
- **The AIRHUG has exactly one native mode: 48 kHz, S16_LE, 2 channels.**
  (`/proc/asound/card5/stream0`.) Mono and 16 kHz only exist via `plughw`
  conversion, so `open_arecord()`'s rate/channel probe can only truly succeed
  at 48000/2 — the rest is the plug layer.
- **The kernel distrusts the AIRHUG's own volume scale**: `Warning! Unlikely
  big volume range (=8191), cval->res is probably wrong`. So capture-level
  percentages on this device are ordinal, not calibrated dB. "100%" presents
  as +31.99 dB, which is the device lying.
- **NEVER SIGKILL a recorder, AND ALWAYS REOPEN WHEN ONE DIES.** This is the
  root cause of "the assistant hears nothing", and it had two halves:
  1. `close_capture()` used to `kill()` with no `wait()`. A killed arecord
     never runs its cleanup and never hands the ALSA device back.
  2. **The half that actually kept recurring:** arecord exits on an XRUN (an
     overrun — whenever something takes the CPU from the capture for long
     enough), and the reader thread simply `break`ed and returned. Nothing
     reaped it, nothing closed its stdout, and nothing told the engine the
     capture had died.

  Either way the kernel is left with `state: SETUP`, an `owner_pid` that no
  longer exists and `subdevices_avail: 0` — unopenable by anyone, including a
  fresh `arecord` from another account (`Device or resource busy`), until the
  app restarts. Worst observed: `hw_ptr` frozen at 40054 for 23 minutes —
  **0.83 s of audio in 23 minutes**.

  Both are fixed: TERM → reap → close the pipe, and the reader's `finally`
  sets `_force_reopen` so a dead recorder takes the loop's existing recovery
  path (the one a USB replug triggers) instead of going quietly deaf.
  Verified over a 4-minute stress: **RUNNING=16, XRUN=0, SETUP=0.**
- **There are two USB capture devices.** Card 5 is the AIRHUG (the mic);
  card 4 is a "USB Composite Device" (Nordic Semi) that PipeWire happily
  captures from. Do not assume card 4 is a microphone.
- Healthy baselines: 55 °C, `vcgencmd get_throttled` = `0x0`, ~2.3 GiB RAM
  available, 44 GB disk free, ~11 ms RTT. The Pi is not the bottleneck.

## Running & restarting
- Launch: `bash DOSE.sh` (installs deps/models, tunes CPU governor, then runs
  `python3 dose_app.py`).
- **There was NO systemd unit, and TWO autostart entries.** Both
  `~/.config/autostart/dose.desktop` (→ `DOSE.sh`) and
  `dose-home-station.desktop` (→ `launch.sh`) launched the app at login — two
  instances competing for one USB microphone. The duplicate is now retired
  (renamed `*.disabled-by-claude.<ts>`, not deleted). **Exactly one autostart
  entry should ever be enabled.**
- **`DOSE.sh` must survive having no TTY.** It ran `clear`, which exits
  non-zero without `TERM`, and the `ERR` trap turned that into `exit 1` —
  so the systemd unit restart-looped instantly (`TERM environment variable not
  set`, five times). `clear` is now advisory and the trap only waits for a
  keypress when stdin is really a terminal. The unit also sets `TERM=dumb`.
- **`DOSE.sh` runs an apt preflight on EVERY launch: ~80 s before the app
  starts.** Budget for that when judging whether a launch has failed — a
  service can look dead for well over a minute and be perfectly fine.
- **systemd is now ENABLED and is the ONLY start path.** Both autostart
  `.desktop` entries are retired (renamed, not deleted). Never run one
  alongside the unit — two instances fighting for one USB microphone is its
  own class of bug, and that is exactly what this device used to do.
  - `systemctl --user status|restart dose-home-station` (as the kiosk user)
  - **Verified: `kill -9` the app and it is back in 24 s, by itself.**
- **The ~80 s preflight is cached.** `DOSE.sh` ran a full `apt update` plus an
  install pass on EVERY launch, because its "is anything missing" gate is made
  permanently true by one probe that can never be satisfied. It is now
  rate-limited by `.deps_checked` (12 h, `DOSE_DEPS_MAX_AGE_HOURS`,
  `DOSE_FORCE_DEPS=1` to force). Startup went **80 s → ~20 s**, which is what
  made `Restart=always` viable at all. It stamps even on a failed install —
  the question is "did we try recently", not "did it all work".
- **`DOSE.sh` cleans up audio before launching.** A process that aborts
  orphans its `arecord` onto init, still holding the capture device, so the
  next instance starts deaf — restart-on-crash faithfully restarting into a
  device that can never work. Launch now TERMs any leftover recorder/player
  and, if the mic is still busy (a kernel-stranded PCM whose owner is gone),
  resets the USB device. That reset was verified on the device as the only
  thing that reliably clears the state.

## KNOWN CRASH — ONNX Runtime aborts the process

    Fatal Python error: Aborted
      onnxruntime_inference_collection.py line 395 in run
      piper/voice.py phoneme_ids_to_audio / synthesize / synthesize_wav
      dose_voice.py _synth / render_to_cache   <- the TTS prewarm

SIGABRT from native code, so **nothing in Python can catch it**. Three
occurrences logged in one afternoon. It is NOT caused by the ONNX thread cap
(one abort predates it), and Piper synthesises fine in an isolated process —
which points at conditions inside the running app (two ONNX sessions, Silero
VAD and Piper, under concurrent load).

**CONTAINED 2026-09-18 — synthesis now runs in a child process.**
`tools/piper_worker.py` loads the voice ONCE and then waits for work, one
JSON line in, one out. An abort kills the child; the parent sees its pipe
close, respawns it, and the crash costs a retry instead of the application.

It is still not root-caused, and that is fine — the blast radius is what
mattered. Notes for anyone touching it:

- **A process per sentence would have been worse than the bug.** Loading the
  voice means building an ONNX session off an SD card: seconds, every
  sentence. The crash is occasional; that latency would be constant. Hence a
  persistent worker.
- **Every failure mode falls back to in-process synthesis**, which is exactly
  the old behaviour — worker missing, won't start, dies, hangs
  (`PIPER_WORKER_TIMEOUT`, 25 s), returns an error, or writes no audio. The
  worst case is "no better than before", never "worse".
- **`DOSE_PIPER_WORKER=0` turns it off** without a code change, if it
  misbehaves on the device.
- Respawns are floored at `PIPER_WORKER_MIN_GAP` (10 s) so a model that
  aborts *during load* cannot make the app fork in a loop.
- The worker caps `intra_op_num_threads` too. Without that, moving synthesis
  out of process would have re-created the 392%-CPU mic starvation that
  `_cap_onnx_threads()` was written to fix.
- Both paths use identical noise parameters on purpose: if they differed, the
  station's voice would change depending on whether a crash had happened
  recently.
- `tests/test_piper_worker.py` (27 checks) fires a real `os.abort()` in the
  child and asserts the parent survives, plus every fallback path.

**STILL NEEDS A DEVICE SOAK.** This was written and tested against a fake
Piper in the container; the real model has never run through it.
- Safe headless test on a dev box: `xvfb-run -a python3.12 dose_app.py`
  (needs a display; tkinter). Set `DOSE_DISABLE_SELF_INSTALL=1` first.

## Tests
- Non-UI suites run under plain `python3 tests/test_*.py`.
- UI suites (`test_audit_page`, `test_ui_speed`, `test_overlay_*`, `test_cpu`,
  `tests/audit.py`) need a display: `xvfb-run -a python3.12 tests/<name>.py`.
- Cloud/diagnostics: `tests/test_cloud_stt.py`.

## Security posture — read before adding anything that leaves the device

This station listens continuously, knows what medication a named person takes,
and updates from a PUBLIC repository. Treat every change as a privacy change.

- **`bash tools/security_audit.sh`** — one command, read-only, full sweep:
  inbound access to the Mac, reverse-path config, listeners with owning
  processes, whether the Pi can reach back, credential placement and modes,
  the egress allowlist, what the Pi exposes, and the cloud configuration.
  Prints no secret values. Ends in a verdict.
- **`tests/test_egress.py`** makes it permanent (36 checks). Every outbound
  host must be on an allowlist WITH A STATED REASON — a new URL fails the
  build until someone adds it deliberately.
- **Trust direction: Mac → Pi only.** The Pi holds no private key and no
  credential for the Mac; the Mac runs no sshd and has no `authorized_keys`.
  **Pi output is DATA** — written to files and read, never executed. Keep it
  that way: the audit greps for `eval`, `bash <(…)`, `| bash` and `$(ssh …)`
  in command position precisely because that is the one way a compromised Pi
  could run code on the Mac.
- **The audit report is REDACTED before publication.** "SEND TO GITHUB" posts
  to a public issue tracker; `redact_for_publication()` strips transcripts
  (health data — the most sensitive thing here), IPv4/IPv6/MAC, `user@host`,
  home paths, and eight credential shapes. The full report still goes to disk
  unredacted; only the copy that leaves is reduced.
- **The cloud upload carries audio and nothing else** — fixed filename
  `audio.wav`, no hostname, no device id. Asserted by the test suite.
- **Credentials live on the DEVICE, never in the repo**:
  `~/dose-home-station/groq_key`, `hf_token` (mode 0600). All git-ignored.
- **The Pi is key-only**: `PasswordAuthentication no`,
  `KbdInteractiveAuthentication no`, `PermitRootLogin no`
  (`/etc/ssh/sshd_config.d/99-dose-hardening.conf`). Both accounts have the
  Mac's public key, verified working BEFORE passwords were disabled. rpcbind
  was listening on port 111 with no NFS mount and is disabled — the Pi now
  exposes port 22 and nothing else.
- **The Mac-side runner is gated.** It refuses symlinks, non-regular files,
  anything not owned by the running user, and anything group/world-writable,
  quarantining them in `refused/`. Every job it runs is logged with its
  SHA-256 first. **Stop it when work is done** — it is a standing executor.

## Deployment workflow
- Durable source lives in GitHub, not only on the Pi. Commit → push to the
  monitored branch → the Pi self-updates and restarts. Never leave important
  changes only on the device. Never commit secrets, recordings, models, venvs.

## TWO COPIES OF THE APP WERE RUNNING. Read this before anything else.

**Root-caused 2026-09-17 22:00.** The station needed physical power
cycles twice in one evening. The cause was not the voice engine, not the
Piper worker, not Wi-Fi, and not the power supply.

```
pid 2543  ppid 1440  1126 MB  cgroup session-1.scope         <- desktop autostart
pid 2549  ppid 2001  1387 MB  cgroup dose-home-station.svc   <- systemd
```

**Two complete instances of `dose_app.py`.** 2.5 GB of a 3.8 GB board,
two copies of faster-whisper, Vosk, Silero and Piper, and two processes
contending for one USB microphone — which is where the pages of

```
paInvalidSampleRate ... AlsaOpen failed ... PaAlsaStream_Configure failed
```

in the log come from. Not a driver mystery. The other instance was
holding the device.

**`DOSE.sh` re-armed the duplicate on every launch.** It rewrote
`~/.config/autostart/dose.desktop` unconditionally. Once the systemd
unit became the start path that is a loop with a one-boot period:
systemd runs DOSE.sh → DOSE.sh writes the autostart entry → the next
graphical login starts a second DOSE.sh.

The entry had been disabled **by hand, twice** — the
`.disabled-by-claude.*` files sit right next to it — and the launcher
put it back both times. This file has said "systemd is the ONLY start
path" since that work, and the launcher contradicted it every boot.
**A fix the program undoes at startup is not a fix, and writing it down
did not make it true.**

Fixed: when systemd is managing us (`INVOCATION_ID` set, or the unit is
enabled) the autostart entry is **retired**, not merely skipped. With no
unit present it is still installed, because then it is the start path.

**Verified after the fix, 10-minute soak:**

| | before | after |
|---|---|---|
| app instances | 2 | **1** |
| available memory | 997 MB | **2088 MB** |
| app RSS | 1126 + 1387 MB | **1387 MB, flat** |
| temperature | 48–63 °C | **42.8–44.3 °C** |
| recorder | cycling | **one, 10+ min continuous** |
| service restarts | — | **0** |

**How to check this in one line**, because `pgrep` lies (see below):

```
for d in /proc/[0-9]*; do C=$(tr '\0' ' ' < $d/cmdline 2>/dev/null); \
  case "$C" in *dose_app.py*) echo "${d#/proc/} $C";; esac; done
```

There must be exactly one, and its cgroup must contain
`dose-home-station.service`.

### Five measurement errors that cost hours — read before trusting a number

Every one of these produced a confident wrong diagnosis on the device:

- **`pgrep -fc <pattern>` counts the shell running the pgrep.** It
  reported two Piper workers when there was one, and nearly justified a
  fix for a CPU regression that did not exist. Read
  `/proc/<pid>/cmdline` and skip the current pid instead.
- **`ps pcpu` is an average over process LIFETIME, not current load.**
  267% twenty seconds after a restart is startup work. Take instantaneous
  CPU from `/proc/<pid>/stat` jiffy deltas over a window.
- **`python3 -u` was added to `DOSE.sh` for unbuffered logging**, and
  every "is the app running" check still grepped for the command line
  without `-u`. Every "APP IS GONE" for an hour was false.
- **A cleanup loop matched its own command line and TERMed its own SSH
  session.** Skip by pid.
- **`ping raspberrypi.local` failed for hours while `ssh dose-pi`
  worked.** mDNS on the Mac is unreliable; SSH is the only reachability
  test that counts. `~/Documents/dose-agent/pi_host.sh` now resolves the
  Pi by cache → alias → known IP → subnet sweep.

The owner reading his own device ("it's something you did", "the Wi-Fi
is unstable", "maybe it's updating too often") was right more often than
these measurements were. **Check the instrument before the code.**

### Selection scoring zero was ALSO the duplicate — a correction

When selection reported `chose: NOTHING` with every route at peak 0, I
attributed it to `ingest()` dropping blocks while the engine is
SPEAKING (startup plays the greeting, and speaking-state audio goes to
barge-in detection and returns). I made measurement bypass mute and
barge-in, which is correct and is now tested three ways.

But the first clean run after the fix recorded:

```
engine state during the walk: idle   muted: False
arecord FORCED card 5,0: opened, peak 9542 (rms 2304, 88 blocks)
took: 2.0s (budget 45s)
```

**The engine was idle and unmuted.** So the speaking-state path was not
what had been zeroing it — the *duplicate instance holding the device*
was. One app, and the very first route delivers 88 blocks at peak 9542.

The bypass stays: it is a real hazard, selection genuinely does run
while the station is talking, and it costs nothing. But it did not
cause this, and the evidence field that proves it (`engine state during
the walk`) only exists because the fix added it. Record what the
program knew, then read it — do not reason backwards from a symptom.

### The auto-update had no brake

`_do_update_check` runs 2 s after launch, `_apply_update` ends in
`os.execv`, and the relaunched app checks again 2 s later. Nothing
counted, nothing waited. Six pushes in an evening meant six
update-and-restart cycles, each reloading four speech models off an SD
card; a non-converging update would spin forever.

- **`DOSE_FREEZE=1` — set this before a demo.** The station pulls
  nothing. Checked before everything else, read from the environment so
  no restart can clear it.
- A persisted cooldown (15 min) and a 3-try limit per remote hash, in
  `update_state.json` — on disk, because the whole failure mode is that
  the process restarts. After three tries on one hash the station stops
  and says so on screen. A stuck station that RUNS beats one that
  reboots.

## THE MICROPHONE FAULT — SOLVED 2026-09-18. Read this first.

Three sessions looked for this. It was never the model, the USB stack, the
recorder, or the CPU. `docs/PI_AUDIT.md` § SESSION 3 has the full account
with the measurements; the short version:

**Route selection tested for a live microphone with `audioop.rms()` while its
own docstring said peak.** In a quiet room those are not close numbers. On
this station, three seconds per route, nobody speaking:

| route | RMS | PEAK |
|---|---|---|
| card 5 `plughw:5,0` (the real mic) | 0 | **29** |
| card 5 via `sysdefault:5` | 0 | **107** |
| card 4 (dead "USB Composite Device") | 0 | **0** |

RMS separates none of them; peak separates them perfectly. So every working
route scored 0, failed the `floor > 1` liveness test, and was rejected as
digitally silent — and the walk continued to the PortAudio fallback at the
end of its list, where `Pa_StopStream` sat down and never got up. The station
was not deaf. It was still choosing, forever, holding the PCM in `SETUP`.

Then, with peak measurement in, it chose correctly 33 times in three minutes
and discarded the answer each time, because the walk closed the stream it had
just measured and reopened the same USB device a fraction of a second later.

**Rules that follow from this, for anyone touching capture:**
- Device liveness is **peak** (`ROUTE_LIVE_PEAK`), never RMS. A dead endpoint
  measures exactly 0; a real microphone measures 29+ in a silent room.
- Never call a bare `.stop()` on a PortAudio stream. Use `_shut_stream()` —
  `Pa_AbortStream` on a daemon thread, joined briefly. A leaked fd beats a
  deaf station.
- Nothing in device selection may be unbounded. See `PROBE_*`/
  `CAPTURE_OPEN_BUDGET`.
- If a route is live, **keep the stream you already have.** Reopening the same
  USB device races the kernel's release of the PCM and loses.
- `tests/test_route_liveness.py` (44 checks) fails if any of the above regress.

**When capture misbehaves, read `voice/selection.txt` first.** The app rewrites
it every selection: route chosen, peak/RMS of each route tried in order, time
against budget, reopen count, and why each reopen happened. It exists because
two diagnoses in one session were wrong from inferring causes out of external
symptoms while the program knew the answer. Don't attach py-spy until you've
read the file.

**And `logs/dose.log` now exists.** `DOSE.sh` used to run
`python3 dose_app.py 2>"$APP_DIR/error.log"` — truncating on every restart,
keeping only stderr, and in practice producing no file at all. Both streams
now append through `tee`, rotated at 8 MB. The unit's `StandardOutput` is
`journal` so the two views don't duplicate.

## HEARING: YES is about the DEVICE. It is not about the SIGNAL.

**2026-09-18.** The heartbeat on the device read `HEARING: YES`,
`blocks/sec: 46.4` — exactly 48000/1024, a flawless capture — and
`live level: peak 0 rms 0`. At that same moment `hw_ptr` advanced
144,385 frames in three seconds, arecord's `wchar` climbed at
96,000 B/s, the app's `rchar` climbed in step, and a standalone
`arecord -D plughw:5,0` read peak 8917.

**Every byte reaching the engine was zero, and every liveness check in
the program said YES.**

`CAPTURE_DEAD_AFTER` cannot see this. It asks whether the device
stopped delivering blocks, and it had not. The station stayed deaf
until a person noticed and said so — which, for a medication cabinet
somebody relies on, is not a recovery story.

So blocks arriving AND the level pinned at `ROUTE_LIVE_PEAK` for
`SILENT_CAPTURE_AFTER` (60 s) while idle is now a fault in its own
right, with a ladder that escalates cheapest-first:

| rung | action | hypothesis |
|---|---|---|
| 0 | force mixer unmute + capture level | ALSA persists a bad level across reboots |
| 1 | forget the arecord combination, re-select | the cached combination is wrong now |
| 2 | **switch to the other recordable card** | the pin names the wrong hardware |
| 3 | drop the pin, full auto-selection | nothing else worked |

**Rung 2 is the leading candidate for the fault itself.** This board has
reported card 5 as both `A28 [AIRHUG 28]` (mixer range 0–8191) and
`Device_1 [USB PnP Sound Device]` (range 0–16). If USB re-enumeration
swaps 4 and 5, `DOSE_MIC_CARD=5,0` points at the dead composite device
— and that device measures exactly peak 0.

The decision (`_silence_due`) is split from the action
(`_silence_recover`) because the half that can be silently wrong for
hours must be testable without a microphone.
`tests/test_silence_watchdog.py` (66 checks) covers the ladder and
every reason NOT to act: a false positive tears down a working mic.

The heartbeat now reports the two separately:

```
HEARING:            YES          <- the device is delivering
signal:             SILENT for 41s (acts at 60s)
silence recoveries: 2   next rung: 2
```

**STILL NOT DIAGNOSED.** This is the recovery, not the cause. But the
evidence now collects itself: the first time the watchdog ever fires it
arms the raw tap (`voice/dump_raw` → `voice/raw_from_engine.wav`) on
its own, once per process, because the tap previously needed somebody
present to touch a file and this fault has only ever appeared when
nobody was. Zeros throughout means the bytes really are silent and the
fault is upstream of the engine; zeros turning into signal means rung 0
fixed it and names the cause. Read that file before theorising.

## Nothing in a turn may start without asking what time it is

From the device's own `turns.jsonl`:

```
worst   "fast": 17.26   "speak": 1.41   "total": 25.19
best    "fast":  0.12   "speak": 1.90   "total":  2.52
```

Endpointing is 0.49–0.57 s of that. All the variance is transcription,
and the worst turn is the fast model taking seventeen seconds and then
the base.en escalation being started **on top of it**.

- **`STT_TURN_BUDGET` (6 s) is a ceiling, enforced.** Chosen against
  what the person does: past about five seconds someone assumes the
  machine did not hear them and repeats themselves, which starts a new
  turn and makes everything worse.
- **The escalation's cost is estimated from this device's own
  measurement** — `self._t_fast * ESCALATION_COST_RATIO`, the fast
  model's time on exactly this audio under exactly this load. A
  throttled Pi and a cool one get different answers with nobody tuning
  a constant. 0.12 s escalates; 17.26 s does not.
- A skip is **counted and explained in the turn log**. A station whose
  accuracy quietly fell is worse than one that is visibly slow.
- **`finish()` used to wait six seconds for the in-flight speculation
  and then transcribe the whole buffer AGAIN.** Six seconds of waiting
  followed by the full cost, for audio a worker was already most of the
  way through. It runs on the same audio — once started, nothing is
  faster than letting it finish. Hits *and misses* are now counted: a
  station whose speculation never lands is doing every turn twice.

`tests/test_stt_budget.py` (30 checks).

## Time-to-first-sound was a whole sentence

`_speak()` renders the first chunk, plays it, and renders the rest on a
worker while that audio is in the air. Piper runs ~3x real time here,
so later chunks are always ready in time. That design was right; what
counted as a chunk was not. `_sentences()` splits only on sentence
ENDS, so "You have two doses left today, Ryan, and the next one is at
six." is ONE chunk — four seconds of speech with nothing audible until
all of it has rendered.

Only the first chunk is on the critical path, so only it has a length
limit (`TTS_FIRST_CHUNK_MAX`, 42). `_split_first()` breaks at the
**strongest** boundary in the window, not the latest: a full stop beats
a comma, a comma beats a conjunction. Taking the latest split "I didn't
catch that, Ryan. Tap the logo and try again." across the word "and",
straight over a full stop that was sitting right there. Piper pauses at
a comma anyway, so the seam is inaudible. A sentence whose only break
is past the window overshoots to it rather than giving up; a phrase
with no boundary at all is left alone rather than chopped mid-clause.

`tests/test_tts_first_sound.py` (33 checks), including a corpus check
that no word is ever lost, reordered or invented.

## ROOT CAUSE, 2026-09-18: ALSA was averaging the microphone away

Measured on the device, app stopped, twelve seconds each, same card,
same rate, same quiet room:

| command | peak | non-zero |
|---|---|---|
| `arecord -D plughw:5,0 -r 48000 -c 1` | 103 | 740 / 570,000 |
| `arecord -D plughw:5,0 -r 48000 -c 2` | **294** | **3,308 / 1,152,000** |

And the engine's own raw tap, armed automatically by the silence
watchdog while the app ran that first command:

```
engine.wav  ch=1 48000 Hz  96256 frames  PEAK=0  non-zero=0/96256
```

**The AIRHUG's only native mode is 48 kHz S16_LE TWO channels.** Asking
for one channel does not hand over the microphone — it asks ALSA's plug
layer to AVERAGE the two. This capsule's quiet-room noise floor is one
or two LSB, and (1 + 0) / 2 rounds to zero. Averaging does not
attenuate a floor that small, it **annihilates** it, and halves
everything else including speech.

Every instrument was honest the whole time. The PCM was RUNNING,
`hw_ptr` advanced 145,677 frames in three seconds, arecord's `wchar`
climbed at exactly 96,000 B/s, and every byte it wrote was a correctly
computed zero. `open_arecord()`'s sweep simply tried `(1, 2)` — mono
first — and mono opened.

**Rules that follow:**
- Capture at the card's OWN channel count. `_native_channels()` reads
  `/proc/asound/card<N>/stream0`; unknown defaults to **2**, which is
  the safe answer, not the neutral one (two channels from a mono device
  duplicates the channel and costs nothing; one channel from a stereo
  device is this bug).
- The app downmixes by taking the **louder channel by PEAK**, with
  slowly decaying running maxima so the choice is made once. It used
  `audioop.rms()` — the third place in this file with the peak/RMS
  mistake — and in a quiet room both channels measure RMS 0, so the tie
  always resolved to left.
- `tests/test_capture_channels.py` fails if any of this regresses.

## DOSE.sh was undoing every deploy, and DOSE_FREEZE did not stop it

```
install dose_voice.py as e3e52dc, verify byte for byte  -> match
start the service, wait for the heartbeat               -> running
read the same path thirty seconds later                 -> c06b6681
```

`c06b6681` is the branch build. **DOSE.sh pulls dose_app.py,
dose_voice.py, dose_nlu.py and DOSE.sh from raw.githubusercontent on
EVERY launch** and had never heard of `DOSE_FREEZE`. The in-app updater
was innocent — it had already declined, exactly as the switch told it
to — and an hour went into reading it.

Anything not on the branch had a lifetime of **one restart**. That is
most of a week of "the update didn't go through".

Fixed: the whole update block in DOSE.sh is inside a `DOSE_FREEZE`
guard, accepting exactly the words dose_app.py accepts.
`tests/test_freeze.py` asserts both pullers obey it and that the guard
BRACKETS the downloads rather than merely mentioning the variable.

**Same lesson as the duplicate autostart entry, third time: A FIX THE
PROGRAM UNDOES AT STARTUP IS NOT A FIX.**

## The silence watchdog: quiet and dead look identical over a short window

Its first two thresholds were wrong, and the device said so both times.
In an empty room the heartbeat sits at `peak 0` for tens of seconds
with a capture the acceptance run then measures at peak 20,347 —
because the engine sees one 21 ms block at a time, on one channel, and
**only about 1.2% of blocks carry a non-zero sample** (`blocks with
signal: 38 of 3100`, measured).

No threshold separates quiet from dead. **Time does.** A room somebody
lives in produces something inside ten minutes; the fault is permanent
and total. `SILENT_CAPTURE_AFTER` is 600 s, the bar is peak strictly
above zero, and the heartbeat reports signal-carrying blocks against
the total so the next person can tell them apart by reading one file.

## The station tests itself now

`tools/acceptance_test.py` — the station says a phrase through its own
speaker, records itself through its own microphone, transcribes it with
the app's own settings and vocabulary bias, and runs `dose_nlu` on the
result. Stop the service first; it needs the capture device.

```
sudo -u rjarv1 python3 tools/acceptance_test.py --json /tmp/acc.json
```

It grades **understanding**, not word error: the cabinet's job is to
work out what was asked. Word error is still printed because it says
where a failure is — deletions mean capture or the VAD, insertions a
hot capture, substitutions the model.

Verified run, 2026-09-18, after the channel fix:

| | result | limit |
|---|---|---|
| understood | **4/4 (100%)** | 100% |
| word error (worst) | **0.0%** | informational |
| STT latency (worst) | **2.28 s** | 6 s |
| TTS render (worst) | **0.64 s** | 2 s |
| capture peak (min) | **15,000** | 300 |
| temperature | **54.5 °C** | 75 °C |
| throttling | **0x0** | 0x0 |

**A loopback flatters the recogniser** — a synthesised voice through a
speaker is cleaner than a person at two metres. A pass means "the path
works and is fast", never "it will understand everyone".

## Three more measurement traps, from this session

- **`pgrep -f "[a]record"` still matched the ssh command's own text.**
  The bracket stops pgrep matching its own pattern; it does not stop it
  matching a script that contains the literal word `arecord` further
  down. The remote shell killed itself mid-block, silently, twice.
  **Match `/proc/<pid>/comm`, not a command line** — a shell's comm is
  `bash`, never `arecord`.
- **A file transfer reported success and left the old file in place.**
  Overwriting an existing path across the desktop bridge kept the
  previous content (392,238 bytes, the build from the day before).
  Every transfer now uses a uniquely named destination and is
  hash-checked on both sides.
- **`cd /home/rjarv1/...` must be INSIDE `sudo -u rjarv1`.** The home
  is mode 0700, so a `cd` outside the sudo lands in `/home/claudeagent`
  and every relative path after it is wrong. This was already written
  down, and it happened anyway.
- **`sudo -n tr ... < /proc/PID/environ` fails**: the redirect is done
  by the *shell*, not by sudo. Use `sudo -n cat ... | tr`.

## "IT CRASHES EVERY TIME" WAS NEVER A CRASH — 2026-09-18

Ryan reported the voice feature crashing on every use. The application
never fell over: **one instance, zero service restarts, no traceback,
3.5 h uptime.** The MICROPHONE was being destroyed and rebuilt about
thirty times a minute, which from the outside is indistinguishable.

Five separate causes, found in this order, each of which hid the next.
Read them together: three of the five were things I had introduced or
mis-measured while fixing the one before.

### 1. The reopen loop — `close_capture()` signalling itself

**This was the big one, and py-spy named it in one dump:**

```
Thread "_run"
    open_pipe_cmd (dose_voice.py:5820)      <- the settle-sleep
    attempt       (dose_voice.py:6088)
    open_arecord  (dose_voice.py:6164)
    open_capture  (dose_voice.py:6640)
    _run          (dose_voice.py:6838)
```

Not stuck, not crashed — opening a microphone it was about to close.

`close_capture()` TERMs the recorder. The reader thread's `finally`
could not see *who* ended it, so it reported our own `terminate()` as
the microphone dying and set `_force_reopen`, which brought the
supervisor straight back to tear down the replacement. **One
legitimate reopen from anything at all — a selection, a hot-plug, the
silence ladder — and the station never stops.** Every `Aborted by
signal Terminated` in that log was the app signalling itself.

Fixed: `close_capture()` leaves the pid in `_closed_on_purpose` before
it signals; the reader skips the reopen for a recorder it finds there.
**Keyed by pid, not a flag** — teardowns overlap during a re-selection
and a flag set by one would silence the other's genuine death report.

The heartbeat now prints **two** numbers, because they mean two
different things:

```
capture reopens: 0   closed on purpose: 2
```

A run of "332 reopens" turned out to be 332 of the second kind.

### 2. Route selection condemned a working microphone on a coin toss

`route_floor()` listened **1.6 s** and rejected anything below
`ROUTE_LIVE_PEAK`. But only about **1.2% of blocks in this room carry
a non-zero sample** (38 of 3100 — a number already written down in
this file, about the silence watchdog). 1.6 s is ~75 blocks, so the
expected number carrying anything is **0.9**.

So the walk measured the real mic, called it digitally silent, killed
it and started over — every ~2 s, forever. 94 blocks per recorder,
which *is* 1.6 s at 47 blocks/sec.

**No threshold separates quiet from dead. Time does.** That sentence
was already in this file about a different watchdog; this was the
second place that needed it. A route now gets up to
`ROUTE_FLOOR_PATIENCE` (5 s) and listening **stops the instant** a
sample clears the bar, so a live route costs what it always did.

### 3. The winner cache remembered whatever OPENED

`open_arecord()` cached the (subdevice, base, rate, channels) that
worked, and wrote it the moment arecord **started**. On this hardware
everything starts — plughw converts anything to anything — so
`16000/1ch`, the averaging path that annihilates a quiet room, was
cached as a winner and tried first on every reopen. The only thing
that evicted it was a failure to *open*, which never came.

Fixed: the tuple is **proposed** on open and **promoted only when the
walk accepts the route as live**; a rejected route evicts it.

### 4. Undecided is not "left". It is "both".

With capture finally correct at 48 kHz stereo:

```
HEARING: YES     blocks/sec: 47.7 (nominal 46.9)     live level: peak 0
```

The downmix picks the louder channel with `>=`, so before either
channel has shown a sample **the tie resolves to LEFT** — and on a
capsule wired to the right that is permanent, because the re-check
looks at ONE block and a quiet block leaves both maxima at 0. With
~0.3% of samples non-zero, the tie is the normal state.

Fixed: while nothing is proven the channels are **summed**, not
picked. `peak 0 → peak 9` on the device. Summing is not the averaging
that started all this: `(1+0)/2` rounds to zero, `1+0` does not — the
bug and its fix differ by a divide.

### 5. My own buffer "fix" made the station deaf, and the number lied

A decode takes ~4 s and ALSA's default capture buffer is ~0.5 s, so a
bigger buffer looked right. I asked for five seconds and read this as
success:

```
reopens: 168 -> 4     overruns: 0
```

The next two fields **on the same line** said what had happened:

```
rec=0     blocks/sec 0.0
```

No recorder at all. This card will not install a 240,000-frame capture
buffer and **arecord does not negotiate — it exits**. Every arecord
route failed to open, the walk fell through to a PortAudio endpoint
that delivers nothing, and the reopen counter stopped climbing because
there was nothing left to reopen.

**A zero can mean "fixed" or "gone". Read the whole line.**

Fixed: `CAPTURE_BUFFER_LADDER` — largest first, last rung asks for
nothing at all. What the card accepts is learned once per card
(laddering inside the sweep would turn 12 spawns into 60) and
forgotten if it stops working. This card takes 2 s.

And `--period-time` is now stated outright, because **arecord derives
the period from the buffer at a quarter of it**: a 5 s buffer meant
1.25 s periods, so the FORCED route delivered `0 blocks` inside a
1.6 s window. A period is also latency, and this station is trying to
answer in under two seconds.

### Measurement traps added this session

- **`pgrep`-style self-matching, eighth instance — and I wrote it into
  the duplicate-instance check itself.** A job reported `instances: 2`;
  the second was the check's own `bash -c`, whose command TEXT contains
  `dose_app.py`. **Match `/proc/<pid>/comm` first** (`python3` for the
  app, `bash` for a shell), then confirm with the command line.
- **Four test assertions broke during this work and all four were the
  test's fault, not the code's**: one sliced a function body as a fixed
  3000 characters (a docstring paragraph pushed the code out of the
  window), two pinned the exact text of a call that had gained an
  argument, and one grepped for a *comment* in comment-stripped source.
  A fifth asserted arithmetic that is simply false. **Assert the
  property — flag counts, adjacency, ordering from the line it is about
  — never the text you happened to write that day.**
- **The app writes `voice/selection.txt` with "why each reopen
  happened".** Three jobs went into inferring the reopen loop from
  external symptoms while that file held the answer. **Read it first.**

## THE MAC IS THE RECOGNISER NOW — verified 2026-09-18

Four real turns through the running station, after pairing:

```
13:18:50  'What time is it?'                fast=0.84  total=3.82  engine=mac
13:19:39  'Did I take my aspirin today?'    fast=0.86  total=3.69  engine=mac
13:20:32  'What do I take today?'           fast=0.93  total=4.70  engine=mac
13:21:28  'How many pills do I have left?'  fast=0.83  total=5.96  engine=mac
```

Four for four, every word correct, with punctuation the Pi's `tiny.en`
does not produce. The Mac's own log for the same turns:

```
stt 2.50s of audio in 0.68s -> 'What time is it?'
stt 1.80s of audio in 0.67s -> 'Did I take my aspirin today?'
stt 1.36s of audio in 0.67s -> 'What do I take today?'
stt 1.74s of audio in 0.68s -> 'How many pills do I have left?'
```

**0.83 s against 4.34–12.99 s local.** The same phrase, `how many pills
do i have left`, cost `fast=12.99` on the Pi an hour earlier.

Capture through all of it: `blocks/sec 46.9`, `capture reopens: 2`,
service restarts 0, 49.1 °C, `throttled 0x0`.

### Four things had to be true, and three of them were not

1. **The Pi has to be paired.** `dose_server.conf` in APP_DIR, 0600,
   address on line one and token on line two. Written by the panel's
   "Pair with the station", or by hand. **Delete it and the station
   goes back to local. That is the off switch.**
2. **The live-transcript shortcut must not answer first.** It answered
   the common phrases before any recogniser ran, so a paired, healthy
   Mac had a hit counter of zero. It is now skipped when the Mac is
   available (`_remote_ready()`).
3. **`finish()` must know the Mac exists.** It returns the local
   speculation whenever it is not "going cloud" — a test that only ever
   asked `_cloud_enabled() and _is_online()`. With no cloud credential
   that is always False, so the speculation was returned every turn and
   `_better_transcribe`, the only place the Mac is asked, was never
   reached. `going_remote` includes `_remote_ready()` now.
4. **The log has to name the right engine.** `_better_transcribe`
   records the engine on `self`, and the speculative pass runs the same
   method on another thread. Whichever finished last wrote the record,
   so the Mac answered three turns and was credited with one. Which
   pass is running is now `threading.local()`; a flag on `self` would
   be read by whichever pass asked last, which is the same bug wearing
   a hat.

### The fallback policy: three strikes, not one

`dose_remote_stt.py` used to write the Mac off for a flat 120 s after
ONE failure — so a laptop waking from sleep cost the next twenty turns
silently. Now:

- **three CONSECUTIVE refusals** before backing off at all
- back-off **10 s → 30 s → 90 s → 180 s**, and any success clears it
- a **timeout is not a refusal**: the Mac answered the door and is
  busy, so it does not count toward giving up
- a `/health` probe runs from the heartbeat thread while idle, so a Mac
  that comes back is used again within seconds, and it warms the model
- the first request after a cold start gets 12 s, not 6

`tests/test_remote_priority.py` (55 checks) pins all of it.

### Read the heartbeat, not me

```
Mac speech server: MAC   turns answered by Mac: 4   refused: 0
  slow: 0   last round trip: 41 ms
```

That line exists because Ryan asked whether the models were running on
the Mac and the only honest answer was "I would have to go and look".

### The Mac app

`/Applications/DOSE PI CONNECTOR.app`. LSUIElement — **no dock icon and
no window**; it starts a panel on `127.0.0.1:8766` and opens it in the
browser. Traps, all of which have now bitten:

- **It used to fail silently.** The launcher was one `exec` of a venv
  python; if that venv was missing the process just vanished. It now
  falls back to the system python (the panel is standard library only),
  logs to `~/.dose-server/launch.log`, and puts failures on screen with
  `osascript`.
- **Clicking it when it is already running** bound a taken port and
  exited, invisibly. It now opens the browser at the running panel.
- **"Running: no" while the server was serving.** The panel asked its
  own child-process handle, so a server started by an EARLIER panel was
  invisible. It asks the port now.
- The panel and server are **copied into the bundle**, so moving the
  checkout cannot break the app.

### Still open

- **The acceptance harness reported "NO TURN RECORDED in 45s" for all
  four turns while those four turns were being written.** The rows land
  about 50 s apart, which is suspiciously close to its own timeout.
  Either endpointing is slow in this room or the harness's detection is
  wrong — **do not guess; measure the `endpoint` field.** The room is
  loud: `blocks with signal: 6657 of 16041` (41%), against a calibration
  profile recorded at `floor 7`.
- Turn totals are 3.7–6.0 s against a 2 s goal. STT is no longer the
  cost (0.84 s); whatever remains is endpointing, the language layer and
  time-to-first-sound, and none of it has been broken down yet.

## Known limitations / TODO
- `arecord -D default` fails with `Host is down` — the PipeWire ALSA plugin is
  not serving this user. Not blocking (the pinned `plughw:5,0` route works),
  but every `default` route in the walk is dead weight until it is fixed.
- ~~`open_arecord()` probes 48 combinations~~ — **done.** It remembers the
  winning (subdevice, base, rate, channels) per card and tries it first; the
  full sweep still runs underneath if that stops working, and hot-plug clears
  the cache because a card NUMBER can be reused by different hardware.
- A USB reset needs root: the device node is `crw-rw-r-- root root` with no
  udev ACL for `claudeagent`, and `/sys/.../authorized` is root-owned. With
  sudo now in place this is finally available as a recovery step.
- One `[aplay] <defunct>` still appears at startup from a path not yet
  identified (the two known playback paths now reap correctly).
- `voice_diagnostics.py full` and per-stage latency still not run end to end.
  Room calibration IS done: `{"floor": 7, "voice": 4016, "gate": 60.0}` —
  a healthy level. Any note saying "calibrated NO" is stale.
- **Latency, measured from the device's own `turns.jsonl`:**
  `"fast": 17.26, "speak": 1.41, "total": 25.19` at worst;
  `"fast": 0.12, "speak": 1.90, "total": 2.52` at best. Endpointing is fine
  (0.49–0.57 s) — **all the variance is STT**. Local `tiny` models on a Pi 4
  will not reach conversational latency; free-tier cloud STT
  (`dose_cloud_stt.py`, already written, dormant without a credential) is the
  path to both speed and accuracy. Needs a free Groq key placed on the device
  by a human — never hardcode one, the repo is public.
- ~~Streaming TTS (synthesize + play in chunks)~~ — **done**, see
  § Time-to-first-sound above. Not yet measured on the device: compare
  `speak` in `turns.jsonl` before and after.
- **The Piper subprocess worker is DEFAULT-OFF** (`DOSE_PIPER_WORKER=1` to
  enable). It contains the ONNX abort, but it shipped on and the station
  needed two physical restarts that evening, so it stays off until a soak
  measures its RSS against a board running ONE application. See § the ONNX
  crash above.
- **`DoseVoice._run` is ~1250 lines**, with `open_capture` (209),
  `open_pipe_cmd` (164), `ingest` (156), `route_floor` and `close_capture` as
  nested closures inside it. That is why every fix this week involved hunting
  line numbers. Splitting it is right and needs a device to soak on after.
- About 220 lines of verified-unreachable code were found and deliberately
  LEFT (`_bt_action`, `mixer_summary`, `_engage_bt_mic_pw`, the draft-dose
  callbacks, the PIL helpers, `_meter_rescan`, `is_pi`). It costs nothing at
  runtime and some looks like work in progress — Ryan's call, listed in
  `docs/PI_AUDIT.md` § SESSION 3b.
- `tests/test_wer.py` now provides a Word Error Rate metric that attributes
  errors by type — deletions mean capture/VAD, insertions mean a hot capture,
  substitutions mean the model. Drop matched `tests/audio/<name>.wav` +
  `<name>.txt` pairs in to score the real pipeline (`*.wav` is git-ignored).
- `tests/test_security.py` and `tests/test_brain.py` fail on a clean checkout
  for reasons predating this work (test_security hardcodes
  `/home/user/DOSECONCEPTPROTOTYPE/`). Not regressions — but not green either.
