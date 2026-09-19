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
- **And the same for a GLOB — this one is silent.**
  `sudo -n rm -f $APP/*.bak226` expands in the CALLING shell, which
  runs as `claudeagent` and cannot read `/home/rjarv1` (mode 0700).
  It expands to nothing, `rm` gets a literal path, and `-f` reports
  success for a file it never saw: "backups before: 3, after: 3".
  The shell has to be the privileged one: `sudo -n bash -c '...'`.

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

### Time-to-first-sound is the last cost, and it is CONTENTION

With the Mac answering, the turn is:

| reply | endpoint | stt | speak | total |
|---|---|---|---|---|
| "The time is 1:28 PM." | 0.50 | 0.99 | **2.78** | 4.38 |
| "I could not find aspirin, Ryan." | 0.82 | 0.88 | **2.86** | 4.68 |
| "No medications are in view today, …" | 0.44 | 0.96 | **3.56** | 5.08 |
| "Current inventory: New Medication, …" | 0.50 | 0.84 | **5.53** | 6.94 |

`speak` is `_t_first_sound` — the FIRST chunk's render and nothing
else. Endpointing and transcription are now flat and small; this is
the whole remaining gap against the two-second goal.

**Two hypotheses tested and both wrong, recorded so nobody retries
them:**

1. *The background render was racing it.* `_speak()` started the
   worker for chunks 2..n before rendering chunk 1. Reordering it
   (render → stamp → start worker → play) is still correct and is in,
   but it did **not** flatten `speak`.
2. *The chunking was not splitting.* It is. Measured directly:

   ```
   'The time is 1:28 PM.'          chunks=1 first=20
   'I could not find aspirin...'   chunks=1 first=31
   'No medications are in view...' chunks=2 first=39
   'Current inventory: New Med...' chunks=2 first=38
   ```

   First chunks are 20–39 characters and `speak` does not track them
   (38 chars cost 5.53 s, 39 chars cost 3.56 s).

3. *It is CPU contention with the running app.* **Measured, and it is
   not.** The same sentence, five renders each, on the device:

   ```
   app running, engine idle   0.78 0.78 0.76 0.80 0.74   (2.6x real time)
   app stopped                0.68 0.65 0.67 0.62 0.65   (3.1x real time)
   ```

   Contention costs **15%**, not 400%. Piper renders "The time is
   1:28 PM." in 0.78 s with the whole application running.

**So the 2.78 s is not synthesis.** Raw `PiperVoice.synthesize_wav`
plus the wave write is 0.78 s; the app's `_t_first_sound` around
`render_to_cache()` is 2.78 s. Roughly two seconds is spent somewhere
between those two, and `render_to_cache` itself is thin — a cache
check, `_synth`, an `os.replace`.

**And the benchmark above has a flaw that points at the answer.** It
loaded the voice with ONNX's DEFAULT thread count, i.e. all four
cores. The application calls `_cap_onnx_threads()` and runs Piper at
`intra_op_num_threads = 2`, which exists because an uncapped Piper
took 392% of a core and starved the microphone (see § the CPU hog).
So 0.78 s and 2.78 s were never measured under the same conditions,
and the next measurement must apply the cap before drawing any
conclusion from the gap.

If the cap is the cost, it is a real trade-off rather than a bug: it
was added to stop the capture being starved. The microphone faults it
was protecting against have since been root-caused properly (channels,
buffer, the reopen loop), so raising it to 3 may now be affordable —
but raising thread counts on this board is exactly what broke the
recorder once already, so it gets measured on the device with the
reopen counter watched, not reasoned about here.

### The synthesis call itself — measured, not yet explained

Split `_synth` into its three parts and the device is unambiguous:

```
synth: {'worker': 0.0, 'load': 0.0, 'synth': 2.468, 'chars': 32}
synth: {'worker': 0.0, 'load': 0.0, 'synth': 2.183, 'chars': 31}
synth: {'worker': 0.0, 'load': 0.0, 'synth': 3.439, 'chars': 38}
```

Not the out-of-process worker (0.0), not resolving the voice (0.0).
`voice.synthesize_wav()` itself, **2.2–3.4 s for thirty-odd
characters**, where the identical sentence measured **0.72 s**
standalone in the same interpreter on the same board.

**Ruled out, with numbers. Do not re-derive these:**

| | |
|---|---|
| the cache lookup | 0.0002 s |
| the file write | 0.0002 s |
| `to_speech()` | 0.000 s |
| the chunker | first chunks measure 20–39 chars |
| the ONNX thread cap (2 vs 4) | 0.18 s |
| contention, app idle vs stopped | 15% |
| the background chunk renderer | reordered; no change |
| Vosk decoding during the reply | gated; no change, and the room was 4.5% signal that run against 54% the run before |

That is six explanations, all mine, all wrong. Then two more:

| | |
|---|---|
| the Silero VAD running per block | gated while thinking; the room that run was **0.1% signal** (17 blocks of 14,658) and synth was still 2.19–3.95 s |
| the ONNX session's construction | a fresh session and one with `_cap_onnx_threads` applied report **identical** options (`intra=0`, sequential, ORT_ENABLE_ALL, CPU provider) and **both render in 0.77 s** — the cap is a no-op on a session built this way |

**Eight. What has never differed is the PROCESS.** Every fast
measurement (0.75–0.80 s) was taken in a fresh interpreter holding
nothing but Piper. Every slow one (2.2–3.9 s) was inside the
application, which also holds faster-whisper, Vosk, Silero, picamera2
and Tkinter at ~1.55 GB on a 4 GB board. Same model, same options,
same machine, same silent room, 3x apart.

**The obvious test of that is `tools/piper_worker.py`. It had never run
once, on either side of the pipe.**

Two bugs, mirror images, each half "fixed" to match the wrong
neighbour — and BOTH hidden by the same broad `except`, which turns
any worker failure into a silent fall-back to in-process synthesis. So
the station always sounded right and the feature never ran:

| | |
|---|---|
| the child | passed a PATH to piper, which wants an open wave → `AttributeError: 'str' object has no attribute 'setframerate'` |
| the parent | passed an OPEN WAVE to the worker, which sends a filename to another process → `TypeError: expected str, bytes or os.PathLike object, not Wave_write` |

Both were invisible until the child's stderr stopped going to
`DEVNULL` and the parent's exception was put in the turn row. **That is
the third time in this project that a discarded error message cost a
day** — after `-q` on arecord and `stderr=DEVNULL` on this same worker.

`tests/test_piper_worker.py` passed all 42 checks throughout, because
its fake piper accepted whatever it was handed. **A test double more
permissive than the thing it stands in for tests the double.** The fake
now raises the same `AttributeError`, and the suite asserts the
contract from BOTH sides (47 checks).

### And with it finally working: the worker is SLOWER

```
15:29:07  The time is 3:29 PM.   speak 2.41   by in-process
15:43:45  The time is 3:43 PM.   speak 6.71   by worker
```

Same sentence, same board, fourteen minutes apart. Rendering in a
138 MB child is **2.7x slower** than rendering inside the 1.6 GB app.

So the process hypothesis is **disproved**, not merely untested, and
`DOSE_PIPER_WORKER` stays OFF. It keeps its original value — an ONNX
abort in the child costs a respawn instead of the application — so the
code and the switch stay. It is simply not a latency fix, and that is
now a number rather than a suspicion.

**Nine explanations, nine measurements, nine misses.** Then the tenth,
which finally isolates one variable — two PiperVoice objects in ONE
process, same sentence, back to back:

```
voice A loaded in 4.99s
  A render 1-3   0.91  0.79  0.75
voice B loaded in 5.01s   (A still resident)
  B render 1-3   0.82  0.81  0.75
  A render 4-5   0.83  0.79   (both resident)
```

So it is **not the voice object ageing** and **not the memory
footprint** — a process holding two full Piper sessions still renders
in 0.79 s.

### Where that leaves it, and it is a sharper question than before

| | |
|---|---|
| a separate process, WHILE the app runs | **0.78 s** |
| the app's own process, same moment, same board | **2.4 s** |

Not machine-wide CPU contention — an external process is fast at the
very moment the app is slow. Something **inside the app's interpreter**
costs 1.6 s per render.

**The GIL was the leading candidate. It is not the answer either.**
Reproduced outside the app — render alone, then render while one
thread does exactly what `ingest()` does per block (tomono, ratecv,
rms, max) at 47 a second:

```
alone                            0.90s   (median 1.38 — warming up)
with ONE capture-reader thread   0.75s   (median 0.83)
penalty                          none
```

**Eleven explanations, eleven measurements, eleven misses.** Stopping
here rather than guessing a twelfth, because every one of these cost a
device round trip and the station is not blocked on it.

### THE PROFILE NAMES IT: piper forks espeak, per sentence

`py-spy record` across a real turn, 1,668 samples in the app's own
process. The frames around synthesis:

```
#163  phonemize              phonemize_espeak.py:36
#164  phonemize              voice.py:297
#165  synthesize             voice.py:349
#166  synthesize_wav         voice.py:465
#167  _synth                 dose_voice.py
#168  render_to_cache        dose_voice.py
#201  run                    subprocess.py:554      <-- here
```

**piper shells out to espeak to phonemize, every single sentence.**
`subprocess.run` means `fork()` (or posix_spawn), and **fork cost
scales with the parent's memory map**, not with the child. Forking
from a 1.6 GB process with hundreds of mappings is far more expensive
than forking from a 200 MB bench.

That is the first hypothesis that fits EVERY measurement at once:

| measurement | explained? |
|---|---|
| external process 0.78 s, app 2.4 s, same moment | yes — the bench is small, the app is not |
| two voices in ONE process still 0.79 s | yes — that process is still small |
| the 138 MB worker at 6.71 s | it forks too, and pays IPC on top |
| a capture-reader thread costs nothing | yes — fork cost is not a GIL or CPU effect |
| ONNX thread cap irrelevant (0.18 s) | yes — the cost is not in inference |
| gating Vosk and Silero changed nothing | yes — wrong subsystem entirely |

**It is hypothesis twelve and it is not yet confirmed** — the profile
shows the call in the stack, not how much of the 1.6 s it holds (the
self-time aggregation in job 219 printed nothing; the speedscope
event parsing was wrong, not the data). Confirm by timing `phonemize`
alone in a big process against a small one before acting.

**If it holds, the fix is not to make fork cheaper.** It is the reply
cache, which already skips this path entirely — and which produced the
best turn of the session:

```
heard "What time is it?"
endpoint 0.55 + stt 0.92 (mac) + speak 0.00 = TOTAL 1.69 s
```

`speak 0.00` is a cache HIT. Under the two-second goal, on a real
turn. Pre-rendering the invariant replies at startup would make that
the normal case rather than the lucky one.

### The one tool not yet pointed at this question

Every measurement above has been a *reconstruction* — a bench that
imitates the app. The app itself has never been profiled DURING a
render. `py-spy record --pid <app> --duration 10` across a turn would
show where those 1.6 s actually go, in the real process, with no
model of it in between. That is the next step, and it is the same
lesson as `voice/selection.txt`: **the program knows; ask it.**

Facts any such attempt must respect, all measured:

| | |
|---|---|
| external process while the app runs | 0.78 s |
| the app's own process, same moment | 2.4 s |
| two voices in one process | 0.79 s — not the footprint, not ageing |
| the worker (138 MB child) | 6.71 s — **slower** |
| a capture-reader thread alongside | no penalty — not the GIL |
| ONNX thread cap 2 vs 4 | 0.18 s, and a no-op as applied |

**Do not "fix" this by moving synthesis to the worker.** Measured:
6.71 s against 2.41 s in-process.

### The reply cache: three bugs wearing each other's clothes

`voice/cache` held 32 wavs and every render logged `hit: 0`, including
replies identical across three runs. A hit costs 0.0002 s against
2.4 s, so this was the single biggest available win. It took three
passes because each fix revealed the next, and the last one was not a
cache problem at all.

**1. The keys were not the keys anybody looked up.** `prewarm_replies()`
rendered each fixed line WHOLE. `_speak()` never renders a whole line —
it splits it and renders `chunks[0]`, so the key it asks for is the
OPENING FRAGMENT. For every line long enough to split, the prewarmed
entry could not be found. *A cache whose keys are not the keys anybody
looks up is a directory of files.*

**2. The same words in a different line are a different key.** Chunking
depends on the length of the WHOLE line. `"Acknowledged. Standing by."`
is 26 characters and caches whole; `"Acknowledged. Protocol three:
protect the patient."` splits and asks for `"Acknowledged."` alone,
which had never been stored. So each fixed line's SENTENCES are cached
independently as well as its chunks.

**3. And then the report lied.** With both fixed, `"Acknowledged."` was
verified present in the cache BY KEY — and the turn that spoke it still
logged `hit=0`. There was no third cache bug. See below.

Note the trap: the cache is `voice/cache`. An earlier job looked at
`voice/tts`, found nothing, and "confirmed" the cache was empty — a
directory that does not exist reads exactly like an empty one.

### The prewarm's ORDER decides time-to-first-sound, not its coverage

Rendering the fixed lines in list order put three safety monologues —
the poison-control line, the crisis line, the dose-advice line — at
positions six, seven and eight. They are the longest things this
station can say and among the rarest, and the cache spent its first
several minutes on them while `"Acknowledged."` and `"Standing by."`
waited behind. 240 s of prewarm produced eight clips.

Only a reply's FIRST chunk is on the critical path; everything after it
renders during playback and is never waited for. So: every line's first
chunk, shortest first, then the individual sentences, then the
remainders. The openings that decide time-to-first-sound are all a few
dozen characters, so the whole first group is done inside a minute —
and the monologues still get cached, last, out of everybody's way.

Two more things the device forced:

- **It only renders while the station is idle.** `nice(10)` settles who
  gets a core; it does nothing about the four ONNX threads, and a
  background synthesis during a live reply competes with the one render
  the person is waiting for.
- **`respond()` holds 27 fixed replies that `_fixed_lines()` never knew
  about** — including the exact `"Acknowledged. Standing by."` family
  the device caught rendering from scratch. `_spoken_constants()` reads
  them out of this file's own syntax tree (string literals inside list
  literals in `respond()`), because a hand-copied list works right up
  until somebody adds a 28th, and that decay does not announce itself:
  the station just gets slower at one sentence and nobody knows why.

### Declared openings: the replies that can never be cached whole

With the prewarm fixed, the device produced its first sub-two-second
turns — three at `speak 0.00`, totals **1.51 / 1.91 / 2.30 s**. What
was left were the replies assembled at the moment of answering:

```
The time is 4:48 PM.                 speak 3.20   total 4.76
One dose remains today: Atorva...    speak 2.94   total 5.24
I could not find promotion in ...    speak 3.20   total 5.14
Negative, Ryan. New Medication ...   speak 1.95   total 3.81
```

No cache can hold any of them — the minute is different every minute.
But the OPENING of each never changes, and the opening is the only part
on the critical path. `"The time is"` has no comma and no full stop
inside it, so `_split_first()` had nothing to break on and rendered the
whole line.

`INVARIANT_OPENINGS` is that list, **declared once and used twice**: the
chunker may break after any of them, and `prewarm_replies()` renders
every one at startup. Two lists would drift and the failure would be
silent — a fragment the chunker produces that the cache does not hold
is rendered from scratch, on the critical path, forever.

**It is a last resort, not a first choice.** Applied ahead of the
boundary search it turned `"You have two doses left today, Ryan, and
the next one is at six."` from a clean break at the comma into
`"You have"`. A real pause a speaker would make beats a prefix that
happens to be cached.

Two related traps:

- **Short is not the same as cheap.** A test asserted that a short
  inventory is "short enough not to need the trick". Twenty characters
  cost 3.20 s because they could not be cached; length does not predict
  render cost, cacheability does.
- **`_spoken_constants()` cannot see an f-string.** `"Negative, Ryan."`
  and `"Partially."` are the first sentences of replies built with
  `f"..."`, so the syntax-tree harvest skips them (it takes string
  literals inside list literals). They go in the prewarm list by hand.

### NOT EVERY .onnx IN voice/ IS A VOICE — it was deleting Silero

**The answer to "who keeps wiping the cache", and it is worse than the
cache.** `dose_app._retire_other_voices()` globbed `*.onnx` and deleted
anything that was not the Piper voice. **`silero_vad.onnx` lives in the
same folder.** So on EVERY LAUNCH the station deleted its own
voice-activity model, concluded that a voice had been retired, and
wiped the entire pre-rendered reply cache as collateral. `DOSE.sh`'s
retirement loop had the identical bug.

The device's own purge log named it on the first restart after that log
existed:

```
17:24:28 pid=351218 dose_app._retire_other_voices: 125 clips, retired silero_vad.onnx
17:26:10 pid=352914 dose_app._retire_other_voices:  62 clips, retired silero_vad.onnx
```

The cache was the symptom. **The quieter cost is worse: the VAD model
was re-downloaded on every boot, so a station with no internet ran with
no voice-activity detection at all — having deleted a model it already
had.** The fetch fails open, so nothing ever complained.

The distinction the code was missing: **a Piper voice is a `.onnx` WITH
a `.onnx.json` beside it.** Nothing else in that folder has one. That
test plus `NON_VOICE_MODELS` (naming `silero_vad.onnx` outright) is the
fix, in both places. `tests/test_migration.py` runs the real function
against a seeded directory and asserts the VAD model survives, a
foreign voice is still retired on sight, and — the whole bug in one
check — with only her voice and the VAD model present, the clips are
still there afterwards.

**And the general lesson, which cost four jobs:** three different
pieces of code could delete those clips, an attribute on `self` only
witnesses its own object (the app builds more than one `DoseVoice`),
and I guessed wrong twice from the outside. Every deleter now writes
one line to `voice/cache_purges.log` — name, pid, count, reason — and
the heartbeat prints the last of them. **When several things can cause
one symptom, make each of them sign its own name before reasoning about
which it was.**

### The cache was wiped on every restart (the other half)

```
221 ended with  61 clips  ->  222 started and found 35
223 started with 116      ->  60 s later there were 22
```

`_purge_foreign_cache()` compares a stamp against
`basename(self._piper_path)`. The preflight fills that in and **has not
necessarily run when the purge does**, so the name was `""`, every
stamp differed from it, and the whole cache went. Every restart threw
the prewarm away and re-rendered it — minutes of synthesis at 68 °C for
a directory whose contents were perfectly good.

The path is a filename, not a model load, so it is resolved in the
purge when missing. If it still cannot be resolved the cache is **left
alone**: purging is for a voice that CHANGED, and a clip in the wrong
voice can never be selected anyway — that is what the voice-keyed
lookup is for. **An unresolved voice is not a different voice.**

### A ceiling on the second step is not a ceiling

```
reply                          stt      total   engine
I didn't catch that, Ryan...  105.14   106.32   whisper-tiny.en
```

A hundred and five seconds for a local pass on at most twelve seconds
of audio (`STT_MAX_AUDIO_S` was working). The station gave a sensible
answer to an empty room.

`STT_TURN_BUDGET` existed and did not help: it gates the **base.en
escalation**, and what ran long was the fast pass underneath it.
`STT_LOCAL_CEILING` (8 s) now wraps that. faster-whisper cannot be
cancelled mid-call, so the work runs on a thread and is **abandoned**
on the deadline — the turn carries on with the live transcript, the
orphan finishes into nothing, and `_stt_abandoned` counts it. Wasting
one pass beats making somebody stand at a medication cabinet for a
hundred seconds.

**Every timed step needs its own ceiling.** A budget that covers the
expensive-looking step says nothing about the one before it.

### Two threads, one attribute — FOUR TIMES NOW

1. `_last_engine`: the speculative transcription overwrote the Mac's
   answer. The Mac answered three turns and was credited with one.
2. `_t_tts`: the background chunk renderer overwrote the first chunk's
   timings. One turn logged `speak=0.67` beside `synth=3.02`.
3. `_t_tts` again, in the diagnostic added to catch (1).
4. `_t_tts` again, from the PREWARM thread. `render_to_cache()` records
   onto `self._t_tts`, the turn log prints that as `hit`, and the
   prewarm calls it hundreds of times on its own schedule — so its
   misses landed on whatever turn was in flight. `"Acknowledged."` was
   in the cache, the turn hit it, and the prewarm wrote a miss over the
   row a moment later. **I went looking for a cache bug that was a
   reporting bug**, with the lesson already written in this file three
   times above.

All four are the same shape: a method that records onto `self`, called
concurrently from two threads, last writer wins. The fix is
`threading.local()` and `_recording()`, not a flag on `self` — a flag
on `self` is the same bug with more steps. **Any method that writes a
diagnostic onto `self` needs `_recording()` before it is called from a
new thread, not after somebody notices the numbers are impossible.**

### git in `dose-agent/repo` runs in a QUEUE JOB, never from the mount

The folder bridge refuses deletes. git writes `.git/index.lock`, does
its work, and then cannot remove it — so `git merge --ff-only` printed
`Updating 43551e0..ce014fc` and left HEAD exactly where it was, and
left three lock files that would have blocked the next job's commit.

Reading with `git log` / `git status` through the mount is fine and
also leaves a lock. Anything that writes belongs in a queue job, which
runs as the real user.

**And check what a bundle is cut against.** `push38` and `push39` were
both cut from a base the Mac's clone does not have, so every fetch said
`Repository lacks these prerequisite commits` and every push said
`Everything up-to-date` — two lines that read like success while GitHub
sat four commits behind a Pi that had the code. `git bundle verify`
before trusting either one.

### UNDER A SECOND, AND WHAT IS ACTUALLY LEFT

Two models on the Mac, picked by ROUTE (`/stt` small.en, `/stt-fast`
distil-small.en — a second constant, never a name in the request, so
the body stays pure audio). The fast one answers the pass that races
the endpointer; anything it returns that does not parse is asked again
properly, which is exactly the case where a drug name matters.

Benchmarked on that Mac, identical audio, six clips:

| model | median decode | command phrases | "…after the metformin" |
|---|---|---|---|
| small.en | 0.57 s | all exact | metformin |
| distil-small.en | 0.44 s | all exact | metformin |
| base.en | 0.21 s | all exact | **medformin** |
| tiny.en | 0.12 s | all exact | **med foreman** |

base.en was tried first and delivered 10 of 11 turns under a second —
and transcribed "what do I take today" as "what two i take today
tomorrow" three times in one run. Two tenths of a second buys a
transcript that is what he said. **Decode is FLAT with audio length**
(0.61 s for 0.93 s of speech, 0.69 s for 3.22 s): Whisper pads to a
thirty-second window, so a three-word question costs what a sentence
does, on an M1 as much as on the Pi.

Best measured turn: **0.42 s**, median 0.84 s, 10 of 11 under a
second, 100% understood.

**ONE IN FLIGHT AT A TIME.** With voices in the room `_last_voice_ts`
moves constantly, a speculation fires on every change, and each is a
request to a Mac that has ONE model and queues them:

```
2.62s of audio in 3.87s
2.62s of audio in 6.19s
2.94s of audio in 9.88s      <- against 0.48s when asked once
```

A new speculation now starts only when the last has finished. Max
decode across the next run: 1.11 s.

**AND THE ROOM STOPPED BEING A LABORATORY.** Two runs of the same
build disagreed completely. The Mac's log said why:

```
'Did I take my aspirin today?  I spent like a day waiting in '
'Basically This Guy Got What Do I Take Today?  He shows every'
"How's the first person?  How many pills do I have left?"
```

Somebody is talking in that room and the station is transcribing what
is really there, correctly. **A run taken while a person is speaking
is not a measurement of the station.** Check the Mac's log for
stranger's words before believing a bad run.

**WHAT IS STILL OPEN:** roughly half the harness's turns reach the Mac
as audio whose VAD finds no speech at all (`0.02s -> ''`), and the
station falls back to Vosk's live text — which is where the sloppy
transcripts come from. The mic has AI vocal isolation ON, and the
harness plays through a loudspeaker across the room; the leading
hypothesis is that the isolation suppresses loudspeaker audio. **That
cannot be settled without a person speaking to it**, which is the one
instrument this session does not have.

### 0.45 SECONDS — transcribe while he is still talking

**The architecture that got under a second.** Measured on the device,
end of speech to first sound, with the breakdown that had never been
instrumented:

```
heard                  endpt   stt   pre    ui think speak  TOTAL
what time is it         0.44  0.00  0.44  0.01  0.00  0.00   0.45
what time is it         0.45  0.00  0.45  0.10  0.00  0.00   0.55
How many pills...       0.44  0.22  0.00  0.00  0.00  0.00   1.13
Did I take my aspirin   0.43  1.11  1.54  0.03  0.00  0.00   1.58

medians:  pre 0.45   ui 0.01   think 0.00
```

`total` is `(t1 - t_stop) + first-chunk render` — the number the owner
asked about, from "you stop talking" to "it starts talking".

**Everything except the endpoint and the Mac is now free.** `ui` (the
"thinking" state marshalling onto the Tk thread) is 0.01 s. `think`
(the whole language layer) is 0.00. `speak` is 0.00 because the reply
cache and the declared openings carry it. The floor is
`ENDPOINT_STABLE` at 0.35–0.46 s, and after that the only variable
left is whether the Mac has finished.

**The change that did it:** the speculative pass — which runs 0.18 s
into a pause, while the person may still be talking — was pinned to
the Pi's local models, and its answer was then REFUSED whenever the
Mac was available:

```
if not going_remote and spec ...
```

Both halves were right when written. The speculation ran
whisper-tiny.en on the Pi, so reusing it would have thrown away a much
better recogniser on the LAN. Point the speculation at the Mac and
both invert: its answer IS what a fresh call would return, and
re-asking is a second round trip for an identical string.

**Two mistakes on the way, both caught by the device:**

- Pointing the speculation at the Mac WITHOUT fixing the reuse test
  made it **half a second slower** — every turn made two Mac requests
  and the second queued behind the first. `spec hits: 0`, stt
  1.08–1.49 s against a 0.85 s baseline.
- The first reuse test asked `spec["by"] == "mac"` — *who answered it*
  — which cannot be known when `finish()` runs: the endpoint fires
  0.35 s after the last voice and the speculation starts at 0.18 s, so
  it has a sixth of a second of head start on a round trip of nearly a
  second. The answer was always "nobody". **Ask where it was POINTED,
  recorded when it started.**

Both counters reading zero — hits AND misses — was the tell that the
branch was never entered at all rather than entered and lost. The row
now says which of the four things happened (`spec_why`), because
"never started", "more speech arrived" and "refused" need three
different fixes and two attempts went by without knowing which.

**What is left is the Mac's round trip**, and nothing else. When it
comes back inside the endpoint window, `stt` is 0.00 and the turn is
0.45 s. When it does not, `stt` is whatever remains of it. `refused`
in the heartbeat is the Mac answering something that did not parse —
not a rejection.

### And the nine-second turns before that

Every turn the Mac answered: 0.75–1.11 s, forty-odd of them. Every
turn it did not: 9.01, 9.90, 10.95, 105.14. **There was no middle**,
and both causes were the Pi doing expensive work to disagree with a
Mac that had already answered:

- **An unusable Mac answer fell through to the Pi's own models.**
  tiny.en is not a second opinion on small.en — it is a WEAKER model,
  and it is the offline fallback, not a court of appeal. A Mac that
  answered has answered: say "I didn't catch that" in a second
  instead of taking ten. The fallback still runs when the Mac is
  absent or vanishes mid-turn.
- **0.36 s of audio at peak 2260** went to a recogniser that spent
  9.90 s on it and returned nothing. `MIN_TURN_AUDIO_S` is 0.5 s, set
  against the turns that WORKED — the shortest correct one in the log
  carried 0.88 s of trimmed audio.

### WHERE IT LANDED — verified on the device, 2026-09-18

Five real turns through the room, measured by the harness after both
of its own faults were fixed:

```
reply                            endpoint   stt  speak  total  hit  engine
The time is 6:17 PM.                 0.55  0.89   0.00   1.52   1   mac
I could not find aspirin, Ryan.      0.53  0.90   0.00   1.44   1   mac
Nothing remains. Every dose is l..   0.56  0.82   0.00   1.85   1   mac
Current inventory: New Medicatio..   0.45  0.96   0.00   1.57   1   mac

VERDICT: PASS   understood 100%   turn total (worst) 1.85s
                time to first sound 0.0s
```

**Every turn under two seconds, and `speak 0.00` on all of them** —
the first sound is already rendered when the station decides to say
it. Against the same station four days earlier: 3.7–6.9 s totals with
2.8–5.5 s of that in synthesis.

What got it there, in order of how much it was worth:

| | |
|---|---|
| the reply cache actually being HIT | 2.4 s → 0.0002 s per opening |
| declared openings for replies that can never be cached whole | the last 3.2 s reply |
| the cache surviving a restart | it was wiped on every launch |
| the Mac as recogniser | 0.83–0.96 s against 4.3–13 s local |
| ceilings on every timed step | a 105 s turn, and then an 11.96 s one |

Board at 50 °C, 2.5 GB free, capture reopens 1, voice gate reading
450 of 450 blocks as speech, Mac round trip 66 ms.

### THE HARNESS WAS THE FAULT, TWICE. The station was fine.

**Four jobs went into proving this station deaf. It was not.** A plain
440 Hz tone through its own speaker, service running:

```
live level:  peak 16798
voice gate:  loud 172   called speech 172 (100%)
signal:      live
```

Capture, energy gate, Silero, speaker — all fine. Two separate faults
in `tools/acceptance_test.py` produced `VERDICT: FAIL` against a
working station, three runs in a row, while the owner was saying it
worked well. **Sixth time in this project that he read his device
better than my instrument did.**

**1. The hook.** Live turns are started by writing
`voice/ptt_request`, which the app only consumes when
`DOSE_TEST_HOOKS=1` is set for the service. It was removed at the end
of an earlier job and not put back. Writing the file always succeeds —
it is a file — so the harness could not tell, and reported "engine
never entered 'listening'". It now checks whether the hook was
CONSUMED, names the switch, and when every live turn was blocked that
way the verdict is **NOT RUN**, not FAIL. *A test that could not run
did not fail.*

**2. `turns.jsonl` IS CAPPED AND THE HARNESS COUNTED LINES.**
`_log_turn()` keeps the last `TURN_LOG_MAX` (60) lines and rewrites the
file, so **once the log is full the line count never rises again**. The
harness waited for `count() > before` — which after 60 turns is false
forever, on any station, however fast. It printed "NO TURN RECORDED in
45s" for four phrases while the station answered every one of them in
1.49–1.63 s, with the rows sitting at the bottom of the file it was
reading. It compares the LAST ROW's content now.

This file previously blamed the 45 s timeout ("the rows land about
50 s apart, which is suspiciously close to its own timeout"). That was
a coincidence, and believing it cost several jobs. **When a measurement
and the owner disagree, suspect the measurement first.**

### Still open
- Turn totals are 3.7–6.0 s against a 2 s goal. STT is no longer the
  cost (0.84 s); whatever remains is endpointing, the language layer and
  time-to-first-sound, and none of it has been broken down yet.

## THE MAC COMPOSES CONVERSATION. THE PI OWNS EVERY FACT.

**2026-09-19.** Ryan, after watching four scripted questions answered
perfectly:

    "What if it just wants to talk and say how are you. It should be
     able to handle any conversation."

and, separately, the rule that makes it safe:

    "Keeping all sensitive information like medical on the pi where
     its hold completely locally and then any unique talking info
     thats not sensistve through the mac"

`tools/dose_reply.py` is that split, and it is **structurally
incapable of stating a fact** — no digit, no drug name, no time,
ever. Anything touching medication returns `"defer"` and the Pi
answers from its own data with the Mac shut.

- **The reply rides back with the transcript**, in the same HTTP
  response. A second request would cost a full round trip (0.85 s
  measured) to replace a decision that already costs nothing: every
  turn row reads `think: 0.00`. There was never any latency to win
  here, and saying so to Ryan before building it was worth more than
  the feature.
- **Keyed by the transcript it was composed for**, not guarded by a
  flag. The speculative pass and the real pass both write it, by
  design. That is the fifth time this project has needed the
  "two threads, one attribute" lesson and the first time it was
  designed in rather than debugged out — a stale reply *cannot* be
  spoken, because it cannot match.
- **The replies are a CLOSED list, and the prewarm renders
  `corpus()`** — the module's own tables, never a copy. A line that
  is not in the TTS cache costs 2.2–3.4 s on the critical path. A
  station that takes three seconds to say "Hello, Ryan" is not more
  human; it is worse at the only thing it is for. This is also why
  there is no model here: `compose()` returns `kind="none"` exactly
  where one would go, on the rare path, never on "how are you".
- **The same file runs on both machines.** Two copies would drift and
  the drift would be silent and slow.
- `tests/test_reply.py` (54 checks) caught two real faults while it
  was being written: the medication guard classified **"good
  morning"** as a medication question (it listed `morning`), so the
  station would have gone silent on a greeting; and three chat
  replies leaked medication words. Both were mine. Hence
  `MED_WORDS` (strong, never exempt) split from `CONTEXT_WORDS`
  (temporal, exempt only for an utterance that is nothing but a
  greeting, anchored at both ends).

## The Mac was keeping a medication record nobody called one

Every transcript went to `~/.dose-server/server.log`, in full,
forever:

    stt[small.en] 1.80s of audio in 0.67s -> 'Did I take my aspirin today?'

It was *useful* — reading those lines is how a bad test run turned
out to be a person talking in the room — and being useful is exactly
how a health record accumulates somewhere nobody thinks of as one.
**102 such lines were on that Mac.**

`_say()` now logs shape (`(6 words, 28 chars)`); the words need
`DOSE_SERVER_LOG_TEXT=1`, off by default and unreachable from the
network. Every diagnosis this project has actually needed from those
lines — empty, prompt echo, a stranger talking — was a question about
SHAPE. `--redact-log` cleaned the existing file and moved the
original to `server.log.with-transcripts` rather than deleting it.

## TLS, pinned, with no way to downgrade quietly

Ryan: *"jsut make sure its encrypted"* and, a minute later, *"The MAC
has to hear the audio in order to do the computing so please make
sure it stays that way."* Both, and they are not in tension.

- `tools/dose_cert.py --make` creates a self-signed certificate. **In
  a separate tool on purpose**: `dose_server.py` is the one thing on
  the Mac the Pi can reach, and its rule — no subprocess, anywhere,
  with a test behind it — is not worth trading to save a file.
- The Pi **pins that certificate**: `CERT_REQUIRED`,
  `load_verify_locations(cafile=CERT)`, `check_hostname=False`
  (the Mac's address is DHCP, so a name in the certificate is a thing
  that silently stops matching). That is *stronger* than ordinary
  HTTPS here — a compromised public CA buys an attacker nothing,
  because the station will not accept a certificate it was not handed
  during pairing.
- **Neither end may fall back.** The server *refuses to serve* if its
  certificate is broken; the client never retries in the clear. A
  client that downgrades on handshake failure is a client an attacker
  downgrades by breaking the handshake.
- Verified on the device: `DOSE server on https://192.168.4.21:8765`,
  and the station's own `dose_remote_stt.encrypted()` → `True`.
- `tests/test_tls.py` (29 checks).

## A DOCSTRING IS NOT AN INVARIANT

`token()` said, in the file:

    That happens ONCE, when the server starts — not per request

and four lines away `_allowed()` built `"Bearer " + token()` on
**every request**. Reading the keychain prompts. The Pi probes
`/health` every few seconds while idle, so protecting the token
bought Ryan a password dialog every few seconds, forever, unaffected
by quitting the app — which is precisely what he reported, three
times:

    "it keeps reasking a bunch of tiems is that normal"
    "i jsut exited the platform but it keeps asking for it"
    "as I exited but it still keeps asking"

I fixed the panel's five-second refresh first. That was real and it
was the smaller half; *"I exited and it still asks"* was him telling
me so, and it took two more turns to hear it. **Eighth time he read
his device better than my instrument did.**

Now: `_resolve_token()` asks once, `token()` caches, and the request
path uses `token_now()`, which cannot reach the vault. Asserted from
the syntax tree, because the next person to add a call in a handler
will be as sure as I was.

**And the worse bug underneath it:** when the keychain refused, the
old code fell through, found no plaintext file (the protection step
moves it aside), **minted a brand-new token and wrote it to disk**.
Every Deny quietly re-keyed the server against a station that could
no longer talk to it. A secret that is PRESENT but withheld is not a
missing secret; `_resolve_token()` refuses rather than inventing one.

## `git merge --ff-only` prints "Updating" and then fails

    Updating ceeb258..9a4d1ed
    head: ceeb258

Both lines, from one job, one after the other. The merge announced
the fast-forward and then could not check out, because the working
tree was dirty — earlier jobs `cp` files into `repo/tools/` and those
edits were still sitting there. `git push` then said **"Everything
up-to-date"** and the sync check said **"IN SYNC"**, and both were
true and meaningless: origin and the clone agreed, at the old commit.
The station ran code that existed nowhere but two machines.

The error explaining this is printed AFTER the Updating line, and
every job in this project piped the merge through `tail -1`. **Fourth
time a discarded error message has cost this project a day** — after
`-q` on arecord, `stderr=DEVNULL` on the piper worker, and the
worker's own exception.

Rules: never `tail` a git merge; and never trust its output at all —
`git rev-parse HEAD` against the **known wanted hash**, and treat a
mismatch as fatal before pushing. `git reset --hard` to the bundle
ref is the recovery, because the bundle is the truth (the container
cut it and the device is already running those files).

## READING A CAPPED FILE AS THOUGH IT WERE A RUN

`voice/turns.jsonl` keeps the **last sixty rows** and rewrites the
file. So `tail -12` returns the last twelve rows *that exist*, not
the last twelve of this run — and after a short run, most of them are
history.

I read twelve rows, found four empty ones, and started diagnosing a
fault. Two details in the same output disagreed with me:

    reply=The time is 8:05 PM.        <- a "current" row
    2026-09-18 21:54:50  stt[...]     <- the actual run

and one row read `heard=Did I take my aspirin today?  I sp` — the
tail of *"I spent like a day waiting in"*, the stranger talking in
the room from job 255, days earlier.

**This file already records this trap** (the harness once counted
lines in this same capped file and called a healthy station FAIL,
twice). I wrote that down and then made the neighbouring version of
it.

**Anything read out of `turns.jsonl` must be filtered by a timestamp
inside a window recorded before the run**, and the Mac's request
count in the same window is the cross-check: six phrases should
produce six to twelve requests, and `requests: 1` is the tell that
nothing else in the row set is about this run.

## `ast.parse` IS NOT A COMPILE CHECK

Every job in this project gates an install with

    python3 -c "import ast; ast.parse(open(f).read())"

and that is weaker than it looks. `ast.parse` builds a tree; it does
not run the compiler's symbol-table pass. So this sailed through it
and onto the device:

    SyntaxError: name 'PHRASES' is used prior to global declaration

`global X` partway down a function that already read `X` higher up.
A real error, in the installed file, invisible to the gate that
exists to catch exactly that.

It cost nothing this time — the harness refused to start and the
station was never touched, which is the best possible version of the
mistake. Next time it might be a file the app imports at boot.

**Use the compiler:**

    python3 -c "import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)" FILE

and run it in the container before staging as well as on the device
before installing. `ast.parse` stays useful for READING a file's
structure in a test; it is not a substitute for compiling it.

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
