# DOSE PI CONNECTOR — read this first

You are a Claude session on Ryan's Mac, or reaching it. This file is
installed at two stable paths so you can always find it:

    ~/.dose-server/CLAUDE_INSTRUCTIONS.md
    /Applications/DOSE PI CONNECTOR.app/Contents/Resources/CLAUDE_INSTRUCTIONS.md

## What this is

The DOSE Home Station is a Raspberry Pi 4 medication cabinet with a
touchscreen and a voice assistant. This Mac app is two things:

1. **A speech server.** The Pi's recogniser has a floor of about 2.1 s
   of pure inference and cannot go faster. This M1 Pro runs a model
   several sizes larger in a fraction of that. The cabinet keeps the
   microphone, the speaker and every decision; the Mac is handed audio
   and hands back text.
2. **A control panel** — the thing Ryan clicks. Station status, the
   speech server's on/off, a GitHub token box, and an SSH shortcut.

**When the Mac is asleep, closed or out of the house, nothing changes.**
The Pi falls back to its own models exactly as before. That fallback is
not a nicety; it is the reason the remote path is allowed to exist.

## The rules this was built under — do not relax them

Ryan's standing instructions, in his words:

- *"nothing has the ability to hack or take over my mac … the raspberry
  pi is simply having information taken from it through the mac but
  there should be nothing that has the ability to send commands to the
  mac"*
- *"its all supposed to be local"*, *"dont openly expose anything"*
- Only free / open-source software. No paid APIs.
- The repo is **public**. Never commit a token, a key, a Wi-Fi password
  or a transcript.

What that means concretely, and what `tests/test_dose_server.py`
enforces on every build by reading the parse tree:

| | |
|---|---|
| `tools/dose_panel.py` | binds **127.0.0.1 only**. The Pi cannot reach it, the router cannot, a phone on the Wi-Fi cannot. It holds a GitHub token and can open SSH, so it is not on the network at all. |
| `tools/dose_server.py` | binds **one LAN address**, never `0.0.0.0`. Bearer token. Peer must be a private address. Two routes: `POST /stt` (WAV in, string out) and `GET /health`. No subprocess, no eval, no exec; every file it opens is a fixed path it owns. |
| `dose_remote_stt.py` (on the Pi) | refuses any address that is not RFC1918 **before a socket is opened**. No proxy. Never raises into a turn. Sends audio and nothing else — no hostname, no identifier, no medication data. |

Two listeners on purpose: two jobs with two different blast radii do
not share one.

## How to use it

```bash
# install or reinstall (safe to re-run)
bash <repo>/tools/mac_app/install_dose_app.sh

# from the panel: Start, then "Pair with the station"
# or by hand:
python3 tools/dose_server.py --serve
python3 tools/dose_server.py --token     # the secret the Pi needs
```

Pairing writes `dose_server.conf` (mode 0600) into the Pi's app
directory: address on line one, token on line two. **Delete that file
and the Pi goes back to local recognition.** That is the off switch.

## Reaching the Pi

- `ssh dose-pi` — user `claudeagent`, key auth, passwordless sudo.
- The app lives at **`/home/rjarv1/dose-home-station`** — a *different*
  user's home, mode 0700. Any `cd` into it must be **inside**
  `sudo -u rjarv1`, or you land in `/home/claudeagent` and every
  relative path after it is wrong.
- The Pi is at `192.168.4.154`; this Mac at `192.168.4.21`.
- **`ssh` is the only reachability test that counts.** `ping
  raspberrypi.local` failed for hours once while ssh worked fine.

## Things that have cost hours here — do not repeat them

- **`pgrep -f "[a]record"` matches the script that contains the word.**
  The remote shell killed itself mid-block, silently, twice. Match
  `/proc/<pid>/comm` instead: a shell's comm is `bash`, never `arecord`.
- **A file transfer can report success and leave the old file.** Use a
  uniquely named destination and compare hashes on both ends.
- **`DOSE.sh` re-fetches four application files on every launch.** It
  honours `DOSE_FREEZE=1` now; before that, every hand-deploy had a
  lifetime of one restart.
- **Measuring the station while running models next to it measures the
  contention.** `tools/acceptance_test.py --live-only` exists for this.
- **Read `voice/live.txt` before attaching a profiler.** It is written
  once a second from its own thread and says what the engine is doing.

## Where to look

| | |
|---|---|
| `CLAUDE.md` | durable device facts — read before believing older notes |
| `docs/PI_AUDIT.md` | every session's findings, with the measurements |
| `voice/live.txt` | live heartbeat on the Pi |
| `voice/selection.txt` | why the microphone was chosen |
| `voice/turns.jsonl` | per-turn timings, stage by stage |
| `~/.dose-server/server.log` | what the Mac transcribed |
