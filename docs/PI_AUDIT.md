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

---

# SESSION 3 — the actual root cause (2026-09-18)

Sessions 1 and 2 fixed real bugs. Neither fixed the reason the
microphone "frequently mishears or hears nothing", because neither had
found it. This session found it, and it was not any of the things the
previous two sessions had concluded.

**Read this section before the two above it.** Where they disagree,
this one is right, and the corrections at the end of it say why.

## The measurement was wrong

Route selection decided whether a capture device was connected by
measuring `audioop.rms()`. Its own docstring said peak. In a quiet room
those are not close numbers — they are on opposite sides of the
decision. Measured on this station with the app stopped, three seconds
per route, nobody speaking:

| route | RMS | PEAK |
|---|---|---|
| card 5 (the real mic) `plughw:5,0` @16k | 0 | **29** |
| card 5 `plughw:5,0` @48k | 0 | **33** |
| card 5 via `sysdefault:5` | 0 | **107** |
| card 4 ("USB Composite Device", dead) | 0 | **0** |
| `-D default` | — | fails: `Host is down` |
| `-D hw:5,0` @48k | — | fails: `Channels count non available` |

RMS separates none of them. PEAK separates them perfectly.

So every working route scored `floor 0`, failed the `floor > 1` test
that means "this one is live", and was rejected as digitally silent.
The mixer was fine the whole time — card 5 at 90%, capture switch
`[on]` — and the hardware was fine the whole time.

`capture_is_live()` had the same bug with a harsher threshold
(`rms > 5`), so the running capture was also reported dead.

## What happened next was worse

Having rejected the real microphone, the walk continued to the end of
its route list — where PortAudio device probing sat down and never got
up. py-spy on the live app, seven minutes after a restart:

```
Thread 76603 (idle): "Thread-5 (_run)"
    stop (sounddevice.py:1143)
    _probe_device (dose_voice.py:1946)
    _pick_input_device (dose_voice.py:2339)
    open_portaudio (dose_voice.py:4111)
    open_capture (dose_voice.py:4568)
    _run (dose_voice.py:4658)
```

Not crashed. Not restarting. Not out of CPU. Still deciding which
microphone to use, holding the ALSA PCM in `SETUP`, and never coming
back. `Pa_StopStream` waits for the device to drain; on a wedged USB
device it waits forever.

`close_capture()` had the same blocking call in its PortAudio branch,
and caught the engine one frame further on as soon as the probe was
fixed. `mic_level()` had it too, on the UI thread — that one would have
frozen the touchscreen, not just the microphone.

## And it threw away the answer once it had it

With peak measurement in, selection became correct and instant. The
station's own report, three minutes after a restart:

```
selection #33 since start
took: 2.8s (budget 45s)
chose: arecord FORCED card 5,0
verdict: a route showed a real noise floor
capture reopens since start: 1
```

Thirty-three correct selections, each one discarded. The walk opened
each route, measured it, **closed it**, then called the same opener
again for the stream it would actually use — two opens of the same USB
device a fraction of a second apart. The recorder's stderr recorded
both halves:

```
arecord: pcm_read:2272: read error: Interrupted system call
arecord sysdefault card 4: audio open error: Device or resource busy
```

EINTR is fatal to arecord: that is our own `SIGTERM` to the probe
recorder, printed by the process we had just decided to trust and then
killed. The `EBUSY` is the reopen arriving before the kernel had
released the PCM.

## Nothing could be seen, which is why this took three sessions

- `DOSE.sh` ran `python3 dose_app.py 2>"$APP_DIR/error.log"`. `2>`
  truncates, so every restart destroyed the log of the run that caused
  it. Only stderr was kept. **And the file did not exist at all** — a
  live check found no `error.log`, no `logs/dose.log`, and an empty
  journal, while the app had been up seven minutes. Every traceback
  this station has produced has gone nowhere.
- `mic_report()` is only written when somebody taps the touchscreen.
  The report on the device was **three days old** while the engine had
  reopened its microphone dozens of times in the previous ten minutes.
- The recorder's stderr went to `/dev/null` (fixed late in session 2) —
  the only place that says `overrun!!!`, `EBUSY`, or `EINTR`.
- Four different things in the supervising loop could trigger a reopen
  and all four looked identical from outside: the counter went up.

## Fixes

| # | Fix |
|---|---|
| 1 | `route_floor()` / `capture_is_live()` measure **peak**, threshold `ROUTE_LIVE_PEAK = 3` (dead endpoint measures exactly 0; a real mic 29+) |
| 2 | `_shut_stream()` — `Pa_AbortStream` on a daemon thread joined 2 s. Used by the probe, `close_capture()`, `mic_level()` and engine shutdown. A stream that will not close leaks one fd instead of deafening the station |
| 3 | Deadlines everywhere in selection: 0.6 s/rate, 4 s/device, 12 s/PortAudio scan, 45 s/route walk — all env-overridable, all reported when hit |
| 4 | A live non-speaker route **returns the stream it just measured**. No second open, no EBUSY race, no self-inflicted EINTR, ~2 s off every selection |
| 5 | Recorder spawned with `start_new_session=True` — arecord dies on EINTR, so any group-directed signal killed the mic; it is out of that blast radius now |
| 6 | `_peak_rms()` — audioop when present, `array` otherwise. Both paths tested to agree. The old fallbacks were `return 999` ("accept any route") and `return True` ("it's live, honest") |
| 7 | `voice/selection.txt`, rewritten every selection: route chosen, peak/RMS of every route tried in order, time against budget, reopen count, **and why each reopen happened** |
| 8 | Both streams append to `logs/dose.log` through `tee`, rotated at 8 MB, with a per-run banner. Unit's `StandardOutput` moved to `journal` so the two views don't duplicate |
| 9 | `tests/test_route_liveness.py` — 44 checks, built on the measurements in the table above |

## CORRECTIONS to sessions 1 and 2

- **"Capture cycles rather than staying open" was not the recorder
  crash-looping.** Those were the route walk's own probes — open,
  listen 1.6 s, close, reject, next — and the `hw_ptr` resets were it
  working exactly as written. Session 2 fixed a real recurrence bug and
  then explained the wrong symptom with it. The stderr capture added to
  catch the recorder complaining found nothing to catch, because the
  recorder was never complaining.
- **"A capture probe can hang" was not a side issue to chase later.**
  It was the fault. It is listed under "Still open" in session 2 as one
  bullet among five.
- **The 283% CPU reading was not a runaway.** `ps pcpu` is an average
  over process lifetime; it decayed 283 → 180 → 148 → 127 → 113 → 102%
  as startup work finished. Temp 45–48 °C, `throttled=0x0`, load
  average 0.8–1.5 on four cores. Nothing was wrong. Two wrong
  diagnoses in one session, both from inferring a cause from an
  external symptom instead of asking the program — which is why fix 7
  exists.
- **Card 4 is confirmed genuinely dead** (peak exactly 0 in a 3-second
  recording), so `DOSE_MIC_CARD=5,0` is correct rather than merely
  plausible.
- **A large-file transfer to the Mac silently truncated**, twice.
  Commit `2500bd6`'s message describes a whole fix; its diff contains
  50 lines of it. It still parsed, so the syntax gate passed. Files now
  move in 40 KB chunks whose md5 is checked at three points — source,
  Mac, Pi — and the deploy job refuses to commit unless the reassembled
  file matches byte for byte. The only reason the truncation was caught
  is that a new test failed on the Pi with
  `module dose_voice has no attribute ROUTE_LIVE_PEAK`.

## Still open

- `arecord -D default` fails with `Host is down` — the PipeWire ALSA
  plugin is not serving this user. Not blocking (the pinned `plughw`
  route works) but it means every `default` route in the walk is dead
  weight.
- The ONNX/Piper `SIGABRT` is still not root-caused; mitigated by the
  supervisor. Running Piper synthesis in a subprocess is the fix.
- `tests/audio/` has no real recordings yet, so the WER yardstick in
  `tests/test_wer.py` has nothing to measure.
- Free-tier cloud STT is written, tested and off. It needs a Groq key
  placed at `~/dose-home-station/groq_key` (mode 0600) **by Ryan** —
  no credential is handled or hardcoded here.
- One `[aplay] <defunct>` at startup, source still unidentified.

---

# SESSION 3b — efficiency pass (2026-09-18)

Brief: find work that takes far too long and condense it, without
breaking anything. On a Pi 4 the dominant cost in the audio paths turned
out not to be computation but **process spawns** — roughly 5–15 ms each,
being done dozens of times per capture open and several times per spoken
sentence, to re-establish facts that were already true.

Measured with a fake `subprocess` in `tests/test_hot_path_cost.py`, so
these are counted, not estimated.

| what | before | after |
|---|---|---|
| unmute one card (2 controls) | 9 spawns | 9 spawns (first time) |
| rest of the six-card sweep | 45 spawns | 45 spawns (first time) |
| the next four sweeps, as `open_capture` did them per selection | **216 spawns** | **0** |
| `systemctl start pipewire` per capture open | 1 spawn each | 1 per 120 s |
| `_make_default` per spoken sentence | 3 pactl spawns | 3 per 300 s per target |
| `open_arecord` on a card that never opens | up to **60** arecord spawns + 60 × 0.3 s | remembered winner tried first |
| `sess.get_inputs()` during speech | **~31 ORT round trips/second** | 1 per session |

## The Silero one is the most egregious

`vad_speech_prob()` contained:

```python
names = {i.name for i in sess.get_inputs()}
```

A VAD frame is 512 samples at 16 kHz — 32 ms. So while anybody was
speaking, that line crossed into the ONNX Runtime C API, allocated a
`NodeArg` per input and built a fresh set about thirty-one times a
second, forever, to answer a question fixed for the life of a loaded
session. 120 frames (~4 s of speech) went from 120 metadata round trips
to 1.

It is cached against the **session object**, not merely stored: a
re-download or a different Silero build can have different input names,
and feeding a new session the old one's names would be a real bug
wearing an optimisation's clothes. The test swaps the model mid-run.

## What is deliberately NOT cached

Caching is for the happy path. These are all somebody waiting for an
answer about the hardware as it is *now*, and a cached skip would be a
regression:

- `_apply_capture_level()` — pushes a **new** level to the hardware. A
  skip means the level silently never arrives, which is exactly the bug
  `_max_capture_by_numid`'s docstring already records happening once,
  when a hardcoded 100 % overwrote the 70 % the caller had just set.
- `force_sink()` — a person just tapped that speaker.
- `mic_report()` — a person tapped the diagnostic.
- `full_mic_test()` — a person is watching it test each device.

And when **not one** capture route opens, the service-kick and mixer
caches are dropped so the retry pays in full. A dead `pipewire-pulse` is
one of the few things that causes that, and skipping the restart because
a healthy run skipped it minutes ago would be the worst possible moment
to economise.

Every window is finite and env-overridable (`MIXER_REDO_AFTER`,
`SERVICE_KICK_AFTER`, `DEFAULT_REAPPLY_AFTER`), because these are
*system* settings — somebody can move a slider in the desktop mixer, and
a station that never reasserted itself would go quiet with no way back
short of a restart. Hot-plug clears the mixer and arecord caches at
once, since a card **number** can be reused by different hardware across
a replug.

## Removed: a by-name mic picker that never worked

Five pieces existed — a `mic_device` settings key, a setter, a device
lister, a by-name PortAudio opener, and a `_mic_pref()` stub — and
**nothing ever read the key**. The picker did nothing at all. It is
superseded by `force_card()`, which selects by ALSA card number, is
wired to the Settings UI, is tested, and keeps working when a device
reports a different name.

A settings key that looks live and is not is worse than no key: anyone
reading the config would reasonably conclude the microphone could be
chosen there.

## Dead code found and deliberately LEFT

About 220 unreachable lines, verified by reference count with no dynamic
dispatch anywhere that could reach them:

| lines | where |
|---|---|
| 41 | `DoseApp._bt_action` |
| 35 | `DoseVoice.mixer_summary` |
| 27 | `DoseApp._pil_bar_chart` |
| 23 | `DoseVoice._engage_bt_mic_pw` |
| 20 | `DoseApp._inc_draft_doses` / `_dec_draft_doses` / `_adj_draft_dose` / `_toggle_draft_day` |
| 13 | `DoseApp._pil_checkmark` |
| 7 | `DoseApp._meter_rescan` |
| 7 | `DoseVoice.is_pi` |

It costs nothing at runtime, some of it looks like work in progress, and
deleting a feature somebody is mid-way through building is not an
optimisation. **Ryan's call, not mine.**

## The real structural problem, NOT fixed

`DoseVoice._run` is **1218 lines**, with `open_capture` (209),
`open_pipe_cmd` (164), `ingest` (156), `route_floor`, `close_capture`
and the rest as nested closures inside it. That is why every fix this
week involved hunting line numbers, and why a 360 KB module exists at
all.

Splitting it is the right change and it is not being made blind. It
touches the live audio engine, the Pi is off the network, and "it should
work" is exactly what this audit was told not to accept. It needs a
device to soak on.

## Latency, where it actually goes

Endpointing is fine (0.49–0.57 s). The variance is all STT: 0.12 s at
best, 17.26 s at worst, on local `tiny` models. No amount of spawn
trimming fixes that — free-tier cloud STT is the path, and it is written,
tested, and waiting on a credential only Ryan can place.

---

# SESSION 4 — two copies of the app (2026-09-17 evening)

The station needed **physical power cycles twice in one evening**. Ryan
pulled the plug both times. This section is what it actually was, and —
because it matters more — how many of my own measurements were wrong
first.

## The finding

```
pid 2543  ppid 1440  1126 MB  cgroup session-1.scope         <- desktop autostart
pid 2549  ppid 2001  1387 MB  cgroup dose-home-station.svc   <- systemd
```

**Two complete instances of `dose_app.py`.** 2.5 GB of a 3.8 GB board,
two copies of faster-whisper, Vosk, Silero and Piper, and two processes
contending for one USB microphone.

That last part explains the pages of

```
paInvalidSampleRate ... AlsaOpen failed ... PaAlsaStream_Configure failed
```

in the log, which had looked like a driver problem for days. It was not.
The other instance was holding the device.

## How it happened

`DOSE.sh` rewrote `~/.config/autostart/dose.desktop` **on every launch**,
unconditionally. Once the systemd unit became the start path that is a
loop with a one-boot period:

1. systemd runs `DOSE.sh`
2. `DOSE.sh` writes the desktop autostart entry
3. at the next graphical login the desktop starts `DOSE.sh` too

The entry had been disabled **by hand, twice**. The evidence was sitting
in the same directory the whole time:

```
dose.desktop                                       Sep 17 21:51
dose.desktop.disabled-by-claude.20260917-172816
dose-home-station.desktop.disabled-by-claude.20260917-161454
```

`CLAUDE.md` has asserted "systemd is the ONLY start path" since that
work, and the launcher quietly contradicted it every boot. **A fix the
program undoes at startup is not a fix, and writing it down did not make
it true.**

Fixed: when systemd is managing us (`INVOCATION_ID` set, or the unit is
enabled) the autostart entry is **retired**, not merely skipped. With no
unit present it is still installed, because then it is the start path.

## A self-restart left the microphone behind

Separately, and found only because the previous fix added the field that
shows it — two selections, both `#1 since start`, 54 seconds apart,
while systemd reported **zero** service restarts:

```
22:11:51   took 2.0s    chose card 5,0   peak 9542, 88 blocks
22:12:45   took 63.8s   chose NOTHING    every route, 0 blocks
           audio blocks delivered since start: 0
```

Zero restarts because the *unit* never restarted — the **app** restarted
itself, `os.execv` after an auto-update.

`os.execv` replaces the process image: no `finally`, no `atexit`, every
thread ceases. The capture recorder is a child in its own session —
deliberately, so a stray group signal cannot kill the microphone — and
that same property carries it straight through `execv` still holding the
USB device. The replacement process is deaf.

`_restart_app()` already released the **camera**. The microphone was
never released, and it is the one another process cannot simply reopen.
`release_audio()` now hands it back: TERM the recorder (never KILL — a
killed recorder strands the PCM), reap it, close the pipe, stop the
synthesis worker. Under a millisecond, so it sits in front of `execv`
without delaying anything.

## The auto-update had no brake

`_do_update_check` runs 2 s after launch, `_apply_update` ends in
`os.execv`, the relaunched app checks again 2 s later. Nothing counted,
nothing waited. Six pushes in an evening meant six update-and-restart
cycles, each reloading four speech models off an SD card; a
non-converging update would spin forever.

- **`DOSE_FREEZE=1`** — pulls nothing. Set before a demo. Read from the
  environment, so no restart can clear it.
- A **persisted** cooldown (15 min) and 3-try limit per remote hash, in
  `update_state.json` — on disk, because the whole failure mode is that
  the process restarts. A stuck station that RUNS beats one that reboots.

Ryan suggested this cause unprompted ("maybe it's updating way too
often"). He was right.

## Verified on the device

| | before | after |
|---|---|---|
| app instances | 2 | **1** |
| available memory | 997 MB | **2193 MB** |
| app RSS | 1126 + 1387 MB | **1235 MB, flat over 3 min** |
| temperature | 48–63 °C | **46.2 °C** |
| selection | 63.8 s, chose NOTHING | **2.0 s, card 5,0, peak 16357** |
| recorder | cycling | **one, continuous** |
| restarts / tracebacks / aborts | — | **0 / 0 / 0** |

## FIVE measurement errors — read this before trusting a number

Every one produced a confident wrong diagnosis, on a device that was
fine:

1. **`pgrep -fc <pattern>` counts the shell running the pgrep.** Reported
   two Piper workers when there was one; nearly justified a fix for a CPU
   regression that did not exist. Read `/proc/<pid>/cmdline`, skip the
   current pid.
2. **`ps pcpu` is an average over process LIFETIME.** 267 % twenty
   seconds after a restart is startup work, not load. Take instantaneous
   CPU from `/proc/<pid>/stat` jiffy deltas over a window.
3. **`python3 -u` was added to `DOSE.sh`** for unbuffered logging, and
   every "is the app running" check still grepped the command line
   *without* `-u`. Every "APP IS GONE" for an hour was false.
4. **A cleanup loop matched its own command line** and TERMed its own SSH
   session.
5. **A test asserted `release_audio()` precedes `os.execv`** and matched
   `os.execv` in the *comment* explaining the fix. Strip comments before
   comparing — the injection detector made this same mistake four times.

And one more, upstream of all of them: **`ping raspberrypi.local` failed
for hours while `ssh dose-pi` worked.** I called the Pi "off the network"
on the strength of a broken test. SSH is the only reachability check that
counts; `~/Documents/dose-agent/pi_host.sh` now resolves by cache →
alias → known IP → subnet sweep.

I told Ryan the lockups were "almost certainly" memory pressure from my
Piper worker. The device showed no OOM, no undervoltage,
`throttled=0x0`, 48 °C. Memory pressure was real — from two whole
applications, not from my change.

**The owner reading his own device beat five of my measurements.** Check
the instrument before the code.

## Still open

- Free-tier cloud STT is written, tested, and dormant. It needs a Groq
  key at `~/dose-home-station/groq_key` (0600), placed **by Ryan** — the
  repo is public and no credential is handled here. This is the remaining
  step for conversational latency; local `tiny` models on a Pi 4 will not
  get there.
- `tests/audio/` has no recordings, so the WER yardstick has nothing to
  measure.
- Streaming TTS (synthesise + play in chunks) would cut perceived
  response time more than optimising Piper.
- `DoseVoice._run` is ~1250 lines with `open_capture`, `open_pipe_cmd`,
  `ingest`, `route_floor` and `close_capture` as nested closures. It
  needs splitting, and it needs a device to soak on afterwards.
- The Piper subprocess worker is **default-off** pending a soak that
  measures its RSS against a board running one application.
- `arecord -D default` still fails with `Host is down`; every `default`
  route in the walk is dead weight.

---

# SESSION 5 — the station said HEARING: YES and heard nothing
## (2026-09-18, offline: the Pi was unplugged overnight)

Ryan's instruction for this session, in his words: *"let's just stick
with all the work we have been doing and just focus on optimization."*
No rebuild, no single model, no deletion. Three things came out of it,
all written and tested against the container because the device was
unplugged; none of them has run on the Pi yet.

## The fault the last session ended on

The heartbeat built in SESSION 4b was reading, on the device:

```
HEARING:        YES
blocks/sec:     46.4
blocks total:   12871
live level:     peak 0  rms 0
```

46.4 blocks/sec is exactly 48000/1024 — a flawless capture. At the same
moment:

| measurement | value | meaning |
|---|---|---|
| `hw_ptr` delta over 3 s | 144,385 frames | the card is producing |
| arecord `wchar` | +96,000 B/s | the recorder is writing |
| app `rchar` | climbing in step | the app is reading |
| standalone `arecord -D plughw:5,0` | peak 8917 | the mic works |
| what the engine received | **every sample zero** | |

**The existing capture watchdog cannot see this.** `CAPTURE_DEAD_AFTER`
asks "did the device stop delivering blocks", and the answer was no:
they arrived, on time, for ever. Every liveness check in the program
said YES. The station stayed deaf until a person noticed and told me.

For a medication cabinet somebody relies on, "deaf until a human
complains" is not a recovery story. That is the gap this session closed.

## 1. A silence watchdog, with a recovery ladder

Blocks arriving **and** the level pinned at the dead-endpoint floor
(`ROUTE_LIVE_PEAK`) for `SILENT_CAPTURE_AFTER` (60 s) while idle is now
itself a fault. A real microphone in a silent room peaks at 29–107; only
a dead endpoint reads exactly 0. The room being quiet cannot trip it.

It escalates cheapest-first, and each rung is a different hypothesis:

| rung | action | hypothesis | cost |
|---|---|---|---|
| 0 | force mixer unmute + capture level | ALSA persists a bad level across reboots | no teardown at all |
| 1 | forget the remembered arecord combination, re-select | the cached combination is wrong for what is on that card now | one reopen |
| 2 | **switch to the other recordable card** | the pin names the wrong hardware | one reopen |
| 3 | drop the pin entirely, full auto-selection | nothing else worked | one reopen |

Past the end it wraps to 0. A station that keeps trying beats one that
stops, and `SILENCE_STEP_GAP` (25 s) plus `_capture_restart_allowed()`
bound how hard it can try.

**Rung 2 exists because of a specific, still-untested suspicion.** This
board has reported card 5 as both `A28 [AIRHUG 28]` (mixer range 0–8191)
and `Device_1 [USB PnP Sound Device]` (range 0–16). If USB
re-enumeration swaps cards 4 and 5, then `DOSE_MIC_CARD=5,0` points at
the dead composite device — **and that device measures exactly the peak
0 we are looking at.** That is the leading candidate for the fault
above, and rung 2 recovers from it without anyone being present.

The decision is split from the action (`_silence_due()` /
`_silence_recover()`) so the half that can be silently wrong for hours
is testable without a microphone. `tests/test_silence_watchdog.py`
(66 checks) covers the ladder and, more importantly, **every reason not
to act** — mid-turn, muted, paused, mid-measurement, blocks stopped,
too few blocks seen. A false positive tears down a working microphone.

The heartbeat now separates the DEVICE from the SIGNAL, which is the
distinction that was missing when it said YES:

```
HEARING:            YES          <- the device is delivering
signal:             SILENT for 41s (acts at 60s)
silence recoveries: 2   next rung: 2
```

## 2. The 25-second turn had no clock in it

From the device's own `turns.jsonl`:

```
worst   "fast": 17.26   "speak": 1.41   "total": 25.19
best    "fast":  0.12   "speak": 1.90   "total":  2.52
```

Endpointing is 0.49–0.57 s. All the variance is transcription — and the
worst turn is the fast model taking seventeen seconds and then the
base.en escalation being started **on top of it**, because no stage in
the chain asked what time it was before beginning.

`STT_TURN_BUDGET` (6 s) is now a ceiling the station enforces rather
than hopes for. Six seconds is chosen against what the person does, not
what the models want: past about five seconds of silence someone
assumes the machine did not hear them and says it again, which starts a
new turn and makes everything worse.

**The escalation's cost is estimated from this device's own
measurement**, `self._t_fast * ESCALATION_COST_RATIO` — the fast model's
time on exactly this audio, on exactly this board, under exactly this
load. A throttled Pi and a cool one get different answers with nobody
tuning a constant. A hardcoded "escalation takes 4 s" is wrong on both.

| fast pass | escalates? | why |
|---|---|---|
| 0.12 s (the best logged turn) | yes | 0.12 + ~0.4 fits easily |
| 1.0 s | yes | 1.0 + ~3.0 fits |
| 2.0 s | no | 2.0 + ~6.0 does not |
| 17.26 s (the worst logged turn) | **no** | this is the 25-second turn |

When it does not fit, the best near-miss still goes to the phonetic
matcher, and the skip is **counted and explained in the turn log** —
a station whose accuracy quietly fell is worse than one that is
visibly slow.

`finish()` had the same shape of bug and it was arguably worse: it
waited **six seconds** for the in-flight speculation and then, if that
had not landed, transcribed the whole buffer **again**. Six seconds of
waiting followed by the full cost, for audio a worker was already most
of the way through — the worst of both paths. The speculation runs on
the same audio, so once it has started nothing is faster than letting
it finish; the only real question is how long the turn may take at all,
and that is now one number. Hits *and misses* are both counted: a
station whose speculation never lands is doing every turn twice, and
that was invisible.

`tests/test_stt_budget.py` (30 checks).

## 3. Time-to-first-sound was a whole sentence

Ryan: *"I want the visual overlay to be fast and quick especially tts."*

`_speak()` already renders the first chunk, plays it, and renders the
rest on a worker while that audio is in the air. Piper runs about three
times real time here, so every later chunk is ready long before the
previous one ends. **That design was right.** What counted as a chunk
was not.

`_sentences()` splits only on sentence ENDS. So

> "You have two doses left today, Ryan, and the next one is at six."

is one chunk: sixty-four characters, roughly four seconds of speech, and
nothing at all is audible until the whole of it has been synthesized.
The person is watching a screen that says nothing is happening.

Only the first chunk is on the critical path, so only the first chunk
now has a length limit (`TTS_FIRST_CHUNK_MAX`, 42). It breaks at the
**strongest** boundary in the window, not the latest:

| reply | opening fragment |
|---|---|
| "You have two doses left today, Ryan, and the next one is at six." | `You have two doses left today, Ryan,` |
| "I didn't catch that, Ryan. Tap the logo and try again." | `I didn't catch that, Ryan.` |
| "Good morning, Ryan. You have three medications scheduled today: …" | `Good morning, Ryan.` |
| "Your next dose is metformin at six in the evening, and you have taken two of three today." | `Your next dose is metformin at six in the evening,` |

Taking the *latest* boundary instead split the second one across the
word "and" — straight over a full stop that was sitting right there.
A full stop beats a comma; a comma beats a conjunction. Piper pauses at
a comma anyway, so the seam is inaudible.

A sentence whose only break falls past the window overshoots to it
rather than giving up — that case is one long clause, and a 50-character
opening beats a 90-character one. A phrase with no boundary at all is
left alone rather than chopped mid-clause.

Side benefit: the opening fragment is far more likely to be a cache hit
next time, which makes the second occurrence instant rather than fast.

`tests/test_tts_first_sound.py` (33 checks), including a corpus check
that **no word is ever lost, reordered or invented** by the split.

## What is NOT claimed here

None of this has run on the Pi. The device was unplugged overnight at
Ryan's request and every measurement above is either from the previous
session's device logs or from the container. In particular:

- the raw tap (commit `5cf0128`) is **pushed but never deployed** — the
  Pi dropped off the network four times, twice mid-install. The sequence
  when it returns is still short: deploy, `touch voice/dump_raw`, read
  `voice/raw_from_engine.wav`, and the digital-silence question is
  settled by comparing two files rather than by reasoning.
- the silence watchdog is the *recovery* for that fault, not the
  *diagnosis* of it. Both are still wanted.
- the TTS and STT numbers above are arithmetic on logged measurements,
  not new measurements.

## Still open (carried forward, plus this session)

- **Deploy and read `voice/raw_from_engine.wav`.** Everything else about
  the digital-silence fault is inference.
- Soak the silence watchdog on the device: confirm it does not fire in a
  genuinely quiet room over an hour.
- Measure time-to-first-sound on the device before and after the first-
  chunk split, from `turns.jsonl` `speak`.
- Confirm the escalation skip rate on real turns. If it skips often, the
  fast path is too slow and the budget is treating a symptom.
- Everything in SESSION 4 § Still open remains true.
