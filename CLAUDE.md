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
- **QR scanning is the steady-state CPU cost:** `pyzbar.decode` in
  `_camera_loop` holds ~84% of a core continuously, competing with audio.
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
- **The systemd unit is installed but DELIBERATELY DISABLED.**
  `tools/dose-home-station.service` + `install_service.sh` are ready and the
  unit *did* start cleanly once `DOSE.sh` was fixed. It is left disabled
  because the ~80 s preflight makes `Restart=always` risky until that is
  sorted, and because the boot path must stay single and known-good. Enable it
  only after retiring the remaining autostart entry, never alongside it.
- Safe headless test on a dev box: `xvfb-run -a python3.12 dose_app.py`
  (needs a display; tkinter). Set `DOSE_DISABLE_SELF_INSTALL=1` first.

## Tests
- Non-UI suites run under plain `python3 tests/test_*.py`.
- UI suites (`test_audit_page`, `test_ui_speed`, `test_overlay_*`, `test_cpu`,
  `tests/audit.py`) need a display: `xvfb-run -a python3.12 tests/<name>.py`.
- Cloud/diagnostics: `tests/test_cloud_stt.py`.

## Deployment workflow
- Durable source lives in GitHub, not only on the Pi. Commit → push to the
  monitored branch → the Pi self-updates and restarts. Never leave important
  changes only on the device. Never commit secrets, recordings, models, venvs.

## Known limitations / TODO
- **The capture CYCLES rather than staying open.** Over a 4-minute sample:
  RUNNING 16, no-stream 32. Not wedged any more (XRUN=0, SETUP=0) but not
  continuously listening either. This is the next thing to chase.
- **A capture probe can HANG.** `py-spy` caught the voice thread blocked in
  `sounddevice.stop()` inside `_probe_device` → `_pick_input_device` →
  `open_portaudio`, after the USB bus re-enumerated. Needs a timeout/watchdog;
  a hung probe means no microphone at all.
- `open_arecord()` probes 48 combinations (4 subdevices × 2 bases × 3 rates ×
  2 channel counts), each with a 0.3 s sleep — up to ~14 s of blocking, and
  every failed open is itself a chance to strand the PCM. The PortAudio path
  now asks the device for its `default_samplerate` first; `open_arecord()`
  deserves the same treatment.
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
- Streaming TTS (synthesize + play in chunks) would cut perceived response
  time far more than optimising Piper itself.
- `tests/test_wer.py` now provides a Word Error Rate metric that attributes
  errors by type — deletions mean capture/VAD, insertions mean a hot capture,
  substitutions mean the model. Drop matched `tests/audio/<name>.wav` +
  `<name>.txt` pairs in to score the real pipeline (`*.wav` is git-ignored).
- `tests/test_security.py` and `tests/test_brain.py` fail on a clean checkout
  for reasons predating this work (test_security hardcodes
  `/home/user/DOSECONCEPTPROTOTYPE/`). Not regressions — but not green either.
- `tests/test_security.py` and `tests/test_brain.py` fail on a clean checkout
  for reasons predating this work (test_security hardcodes
  `/home/user/DOSECONCEPTPROTOTYPE/`). Not regressions — but not green either.
