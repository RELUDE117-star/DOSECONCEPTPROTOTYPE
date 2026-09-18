# Raspberry Pi audit — REAL DEVICE FINDINGS

Filled in live over SSH on **2026-09-17** against the actual hardware.
Everything below was measured on the device, not inferred. Where something is
still a hypothesis it says so explicitly.

Access note: the Claude session doing this ran in Anthropic's cloud (no LAN),
bridged to the Pi through a queue-runner on Ryan's Mac that executes
`ssh dose-pi` jobs in the real macOS shell. See "Access reality" below.

---

## Identity

| | |
|---|---|
| Model | Raspberry Pi 4 Model B Rev 1.5 |
| OS | Debian GNU/Linux 13 (trixie) |
| Kernel | `6.18.39+rpt-rpi-v8` aarch64 |
| CPU | Cortex-A72 ×4, 600–1800 MHz, governor at 100% |
| Python | **3.13.5** (system `/usr/bin/python3`, no venv) |
| Hostname / IP | `raspberrypi` / `192.168.4.154/22` (wlan0, DHCP) |

## Resources (healthy — not the problem)

- RAM 3.7 GiB total, 1.2 GiB free, **2.3 GiB available**; swap 2.0 GiB, **0 B used**
- Disk `/dev/mmcblk0p2` 58 G, 23% used, 44 G free
- `vcgencmd measure_temp` → **55.0 °C**
- `vcgencmd get_throttled` → **`0x0`** (no throttling, ever, since boot)
- `vcgencmd measure_volts core` → 0.9160 V
- Network: 8.8.8.8 RTT **~11 ms**, 0% loss

**The Pi is not thermally or resource constrained.** Ruled out.

## Audio hardware

```
5 [A28] USB-Audio - AIRHUG 28    Generic AIRHUG 28 at usb-0000:01:00.0-1.4, full speed
4 [Device] USB-Audio - USB Composite Device      (Nordic Semi, at 1-1.3)
3 [UACDemoV10] USB-Audio - UACDemoV1.0           (Jieli, playback only — the speaker)
```

AIRHUG native capability (`/proc/asound/card5/stream0`) — **there is only one mode**:

```
Format: S16_LE   Channels: 2   Rates: 48000   Endpoint: 0x82 (2 IN) (ASYNC)
```

So the mic is **48 kHz stereo only**. Any 16 kHz / mono capture is a `plughw`
conversion, and `open_arecord()`'s probe over rates `(48000, 44100, 16000)` and
channels `(1, 2)` can only ever truly succeed at 48000/2 — everything else is
the plug layer resampling.

USB: `bMaxPower=100mA`, `speed=12` (full-speed USB 1.1). Adequate for
48 kHz × 2 ch × 16 bit (1.5 Mbps of 12 Mbps). Device has **not** re-enumerated
(still Bus 001 Device 006); no USB disconnect/reset in `dmesg`.

### Kernel flags the mic's own volume scale as bogus

```
usb 1-1.4: Warning! Unlikely big volume range (=8191), cval->res is probably wrong.
usb 1-1.4: [5] FU [Mic Capture Volume] ch = 1, val = 0/8191/1
```

The AIRHUG reports a 0–8191 range the kernel itself distrusts. **Percentage-based
gain control on this device is not a reliable dB scale** — `100%` presents as
`+31.99 dB`, but the mapping is the device lying. Treat capture-level percentages
here as ordinal, not calibrated.

---

## ROOT CAUSE — the capture stream is wedged, and the app cannot recover

This is the primary finding. It is not gain, not VAD, not the STT model.

### Evidence 1 — the PCM has been frozen for 23 minutes

`/proc/asound/card5/pcm0c/sub0/status`, sampled 8× over 16 s:

```
sample1  state=SETUP  hw_ptr=40054  appl_ptr=39958
...
sample8  state=SETUP  hw_ptr=40054  appl_ptr=39958     ← identical, 16 s later
trigger_time: 826.916    tstamp: 2218.633
subdevices_avail: 0
```

The stream was triggered at t=826 s. At t=2218 s it had advanced to
**hw_ptr 40054 frames = 0.83 seconds of audio, total**. The pointer never moved
again. **The microphone delivered under one second of audio in twenty-three
minutes.** That is the entirety of "the assistant hears nothing."

### Evidence 2 — the owner process is dead, the device still claimed

```
owner_pid : 5426          →  /proc/5426 does not exist
subdevices_avail : 0      →  device still counted as in use
```

An independent `arecord -D plughw:5,0` from a second account failed with
**`audio open error: Device or resource busy`**. The capture device is
stranded: claimed by a process that no longer exists, and unopenable by anyone.

### Evidence 3 — the unreaped child that proves the mechanism

```
Z+  5235  4078  rjarv1  [arecord] <defunct>
```

A **zombie `arecord` parented to the app**. The capture backend is
`open_pipe_cmd()` (`dose_voice.py:3962`), which `Popen`s `arecord` and streams
raw PCM over a pipe. Teardown is `close_capture()` (`dose_voice.py:4058`):

```python
def close_capture(cap):
    kind, h = cap
    try:
        if kind == "portaudio":
            h.stop(); h.close()
        else:
            h.kill()          # ← kill() with no wait(): the child is never reaped
    except Exception:
        pass
```

`h.kill()` with no `h.wait()` leaves a zombie every single time a pipe capture
is torn down, and `proc.stdout` is never closed, so the reader thread's pipe
end leaks too. Each capture cycle leaks one zombie and one fd.

### Why it never recovers

`open_arecord()` (`dose_voice.py:4005`) probes up to
**4 subdevices × 2 bases × 3 rates × 2 channel counts = 48 combinations**, each
with a `time.sleep(0.3)` in `open_pipe_cmd` — up to ~14 s of blocking. Once the
PCM is stranded, *every one of those 48 attempts fails with EBUSY*, the probe
burns ~14 s, and the app is left with no capture and no escalation path. It
does not reset the USB device, does not surface the EBUSY, and does not restart
itself. It simply goes deaf until someone restarts it by hand.

**Confidence: high.** Frozen hw_ptr, dead owner, EBUSY from an independent
process, a zombie `arecord`, and a teardown path that provably cannot reap —
these agree.

---

## SECOND DEFECT — one thread burning 81% of a core, permanently

```
tid 5411 ticks in 5 s: 412, then 406      (500 ticks = 100% of one core)
state over 10 samples: R R R R R R S R R R
```

Process totals: **43 minutes of CPU in a 31-minute process (~138%)**, RSS ~1.0 GB.
Per-thread CPU time:

```
  1072.39s  tid=5411   ← the hog, ~81% of a core, continuously
   158.97s  tid=5450
   147.73s  tid=5449
   146.47s  tid=5448
   116.44s  tid=5409
    50.88s  tid=5405  CameraManager
```

This is the "turns are slow" half, and it is almost certainly the same fault:
a capture/probe loop spinning against a dead device. **Not yet proven** —
proving it needs the app's own logs, which requires read access to
`/home/rjarv1` (see Access reality).

### Hypothesis tested and REJECTED: the pure-Python `audioop` shim

Python 3.13 removed stdlib `audioop`, and this device confirms it:

```
>>> import audioop
ModuleNotFoundError: No module named 'audioop'
```

`audioop-lts` is **not installed**, so the pure-Python fallback shim at
`dose_voice.py:37-99` is live. It was a strong candidate for the CPU burn.
Measured on the actual Pi, reproducing the shim exactly:

```
pure-python ratecv, 1.0 s of 48 kHz audio: 0.021 s CPU  =>  2.1% of one core
pure-python rms,    1.0 s of 48 kHz audio: 0.009 s CPU  =>  0.9% of one core
combined: 3.0% of one core
```

**3% is not 81%. Hypothesis rejected.** The shim is fine for now, though
installing `audioop-lts` would still be worth doing for exactness.

---

## THIRD DEFECT — the capture-level fix is silently overwritten

Observed on the device before any change:

```
card 5 'Mic'  Front Left: Capture 8191 [100%] [31.99dB] [on]
              Front Right: Capture 8191 [100%] [31.99dB] [on]
```

`DEFAULT_CAPTURE_LEVEL` is **70**, yet the hardware sat at exactly **100%**.
`_max_capture_by_numid()` (`dose_voice.py:1946`) is the only code that writes
`100%`, and `_max_capture()` calls it *immediately after* writing
`_capture_level` to the named control:

```python
attempts = [[lvl] + a for a in attempts] + attempts   # writes 70%
...
self._max_capture_by_numid(card)                      # then csets 100% over it
```

So the documented "symmetric levelling + 70% default" never reaches the
hardware: every `_apply_capture_level()` is undone in the same call.

**Tested live:** setting 70% by hand held at 70% for 60 s with no snap-back —
so the app is *not* continuously re-pinning it; the clobber happens at init and
on each level change, not on a timer. An earlier claim that the leveller was
being fought in real time was wrong and is corrected here.

Given the kernel's "volume range is probably wrong" warning, +31.99 dB of analog
gain on a cheap electret is a genuine accuracy problem — but it is a *secondary*
one. A mic that is not streaming at all cannot be fixed by setting its gain.

---

## How the app actually starts (answers the open question in CLAUDE.md)

**There is no systemd unit.** Nothing in `/etc/systemd/system`, nothing in
`systemctl list-unit-files`, no user units, no autostart `.desktop`, no crontab
entry. `systemctl --failed` is clean because nothing is registered at all.

What actually runs:

```
rjarv1 3995  x-terminal-emulator -e /home/rjarv1/Desktop/DOSECONCEPTPROTOTYPE-claude-quirky-brown-vkHwi/DOSE.sh
rjarv1 4005  /bin/bash /home/rjarv1/Desktop/DOSECONCEPTPROTOTYPE-claude-quirky-brown-vkHwi/DOSE.sh
rjarv1 4078  /usr/bin/python3 /home/rjarv1/dose-home-station/dose_app.py
```

Note the **two different directories**: the launcher lives in a *Desktop* folder
named after the branch, the application in `~/dose-home-station`. Consequences:

- no restart on crash, no start on boot, no log capture, no resource limits
- the app dies with the terminal window
- which of the two trees is authoritative is ambiguous

Exactly one instance is running. `user@1000` and `user@1001` managers are both
up (rjarv1 and claudeagent).

---

## Access reality (correcting the handoff brief)

The brief stated `claudeagent` has passwordless sudo. **It does not**, and
`tools/bootstrap_claude_access.py` **has never run on this Pi**:

- `sudo -n true` → `sudo: a password is required`
- no `/etc/sudoers.d/*claude*`
- no `/var/lib/dose-claude-bootstrap/`, no `~/.local/state/dose-claude-bootstrap/`

The account and its SSH key were provisioned by hand; the sudo half never
happened. `claudeagent` is in `audio video plugdev gpio i2c spi render dialout`,
so mixer and ALSA work — but `/home/rjarv1` is mode `0700`, so **the app
directory, its logs, its config and its user-site packages are unreadable**.
The app's Python deps are not in the system `pip list` (only numpy 2.2.4 is),
so they live in rjarv1's user site-packages, invisible without sudo.

To unblock, on the Pi as a sudoer:

```
echo "claudeagent ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/020_claudeagent-nopasswd
sudo chmod 0440 /etc/sudoers.d/020_claudeagent-nopasswd && sudo visudo -c
```

### Side effect worth knowing

Every `claudeagent` SSH login starts a **second PipeWire + WirePlumber stack**
(its own `user@1001` session), which can contend with rjarv1's for the USB
devices. This session masked them for `claudeagent` so diagnostics cannot
perturb the app's audio:

```
systemctl --user mask pipewire.service pipewire.socket \
    pipewire-pulse.service pipewire-pulse.socket wireplumber.service
```

Reverse with `systemctl --user unmask …` if that account ever needs audio.

Also note `card 4` pcm0c is `state: RUNNING` owned by rjarv1's PipeWire (pid
1257) — the stack is actively capturing from the *other* USB device while the
AIRHUG sits dead.

---

## Fixes applied

See the commit that accompanies this document.

1. `close_capture()` now reaps the child (`kill()` → `wait()`) and closes the
   pipe, so teardown can no longer leave zombies or leak fds.
2. `_max_capture_by_numid()` no longer forces capture volumes to `100%` when a
   `_capture_level` is set; it honours the configured level.

## Still open

- Prove what tid 5411 is spinning on (needs app logs → needs sudo).
- Install a systemd unit with restart-on-failure and boot start; resolve the
  Desktop-vs-`dose-home-station` directory ambiguity.
- Run `voice_diagnostics.py full` and room calibration — **blocked**: the app
  owns/wedges the mic and the diagnostics live in an unreadable directory.
- Recovery path for EBUSY: detect a stranded PCM and reset the USB device
  (`usbreset` / unbind-bind `1-1.4`) instead of going quietly deaf.
- Per-stage latency and idle/listening/processing CPU — blocked behind a
  working capture.
- Install `audioop-lts` for exact audio math.
- WER metric and real-audio regression tests.

---

# SESSION 2 — what was actually wrong, and what was done (2026-09-17)

Everything here was measured on the device over SSH, through a
queue-runner on the Mac (the cloud session has no LAN; see "Access
reality" above).

## The fault chain, in order

1. **arecord exits on an ALSA XRUN.** An XRUN is an overrun — it happens
   whenever something takes the CPU away from the capture for long
   enough.
2. **The reader thread just `break`s and returns.** The recorder was
   never reaped (hence the recurring `[arecord] <defunct>`), its stdout
   was never closed, and nothing told the engine the capture had died.
3. **The kernel is left with a PCM in `state: SETUP` owned by a process
   that no longer exists.** Unopenable by anyone, including a fresh
   `arecord` from another account (`Device or resource busy`).
4. **Permanent deafness from one transient overrun**, until a restart.

Measured at its worst: `hw_ptr` frozen at 40054 for 23 minutes —
**0.83 seconds of audio in 23 minutes** — while one thread burned
1,072 s of CPU spinning against the dead device.

## What caused the XRUNs in the first place

`py-spy` on the live process, once the mic was streaming again:

```
Thread (active): "tts-prewarm"
    run (onnxruntime/.../onnxruntime_inference_collection.py)
    phoneme_ids_to_audio (piper/voice.py)
    render_to_cache (dose_voice.py)
```

Piper's ONNX session plus three native ORT workers, ~100% each:
**392–398% of one core at 73.5 °C**, at the exact moment the capture
stream was coming up. `INFER_THREADS` was already 2 and
`OMP_NUM_THREADS` was exported to match — neither mattered, because a
stock onnxruntime wheel is not built with OpenMP and the only lever is
`SessionOptions.intra_op_num_threads`, which piper leaves at 0.

## Fixes, in the order they were made

| Fix | What it does |
|---|---|
| `close_capture()` TERM→reap→close | One route to a stranded PCM. SIGKILL meant arecord never released the device. |
| `_max_capture_by_numid()` honours `_capture_level` | It was csetting a hardcoded `100%` over the 70% written one line earlier. Device was at `8191 [100%] [31.99dB]`; now `5734 [70%] [22.39dB]`. |
| Class-level defaults on `DoseVoice` | PortAudio's callback raced `__init__`; `AttributeError: '_muted'` was swallowed by cffi and **silently dropped audio blocks**. |
| Rate probe asks the device first | Probed 16 kHz first; the AIRHUG supports **only 48 kHz stereo**, so every launch produced dozens of failed opens — and a failed open is what strands the PCM. |
| `_cap_onnx_threads()` | Caps Piper's ORT session. **392% → 111% of one core.** |
| Prewarm waits for audio to flow | Stops the TTS cache warm-up racing the capture at startup. |
| Playback reaping | `[aplay] <defunct>`, same bug at the other end of the pipeline. |
| **Reader teardown + `_force_reopen`** | **The one that makes it hold.** A dead recorder now reopens instead of wedging. |
| `DOSE.sh` survives no TTY | `clear` fails without `TERM`, and the `ERR` trap turned that into `exit 1` — the systemd unit restart-looped on it. |
| Updater syncs the whole branch | It shipped six hardcoded files; `voice_diagnostics.py`, `dose_cloud_stt.py` and all of `tools/` never reached a device. |
| `test_no_private_keys.py` | Had failed on **every run since it was added** — it matched its own pattern list. Now 24/24 and stricter. |

## Verified after the fixes (4-minute stress)

```
RUNNING=16  XRUN=0  SETUP=0        <- the terminal wedge is gone
capture level 5734 [70%] [22.39dB]
CPU ~95-111% of one core, 56 C, throttled=0x0
```

## Startup / boot

- **There were TWO autostart entries** both launching the app
  (`dose.desktop` → `DOSE.sh`, `dose-home-station.desktop` →
  `launch.sh`). Two instances competing for one USB mic. The duplicate
  is retired (renamed, not deleted).
- A systemd user unit is written and installed
  (`tools/dose-home-station.service` + `install_service.sh`) but left
  **disabled**: it started cleanly once `DOSE.sh` was fixed, but
  `DOSE.sh` runs an apt preflight taking **~80 s on every launch**,
  which needs sorting before a restart-on-crash unit is safe to enable.

## CORRECTIONS to earlier claims in this document

- **audioop IS available to the app.** `python3 -c "import audioop"`
  fails for the dev account, but the running app lists
  `audioop._audioop` among its loaded extension modules — the backport
  is in the kiosk user's site-packages. The 3.0%-of-a-core figure is
  what the pure-Python fallback *would* cost, not what is running.
  The method that produced the wrong answer — measuring a second
  account's interpreter — is the part worth not repeating.
- **The device HAS been calibrated**: `{"floor": 7, "voice": 4016,
  "gate": 60.0, "gain": 1.0}`. "calibrated NO" was stale.
- **The microphone was declared fixed too early.** The first teardown
  fix was real but partial; the device wedged again within minutes once
  an XRUN occurred. Only the reader-teardown fix makes it hold.

## Still open

- **Capture cycles rather than staying open**: over 4 minutes, RUNNING
  16 samples vs no-stream 32. Not wedged, but not continuously
  listening either. Next thing to chase.
- **A capture probe can hang**: py-spy caught the voice thread blocked
  in `sounddevice.stop()` inside `_probe_device` after a USB
  re-enumeration. Needs a timeout/watchdog.
- **QR scanning costs 84% of a core continuously**
  (`pyzbar.decode` in `_camera_loop`), competing with audio. Throttling
  it while a turn is in progress is the obvious win.
- **`DOSE.sh` apt preflight ~80 s every launch.**
- One `[aplay] <defunct>` still appears at startup from a path not yet
  identified.

## Latency, from the device's own `turns.jsonl`

```
"fast": 17.26, "speak": 1.41, "total": 25.19   <- worst observed
"fast": 0.12,  "speak": 1.90, "total": 2.52    <- best observed
engine "moonshine", model "tiny"
```

Endpointing is fine (0.49–0.57 s). The variance is all in STT. Local
tiny models on a Pi 4 will not reach conversational quality; free-tier
cloud STT (`dose_cloud_stt.py`, already written, off without a
credential) is the path.
