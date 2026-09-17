# Raspberry Pi audit — to be filled in over SSH

This template is populated by a Claude Code session running **on the Mac**
with `ssh dose-pi` (the cloud session cannot reach the home LAN). Run the
commands and paste the (secret-free) output under each heading. Do not
include Wi-Fi passwords, tokens, keys, or auth headers.

## Identity
- `cat /proc/device-tree/model` →
- `uname -a` / `uname -m` →
- `cat /etc/os-release` (PRETTY_NAME) →

## Resources
- `free -h` / `swapon --show` →
- `df -h` →
- `vcgencmd measure_temp` / `vcgencmd get_throttled` →
- idle vs listening vs STT CPU/RAM (from `voice_diagnostics.py full`) →

## Audio hardware
- `lsusb` →
- `arecord -l` / `arecord -L` (mic; confirm AIRHUG card/device index) →
- `aplay -l` / `aplay -L` (speaker) →
- `pactl info` / `pactl list short sources` / `... sinks` →
- native sample rate / channels / bit depth of the mic →

## Software
- `python3 --version`; virtualenv? →
- installed app deps (vosk, piper, faster-whisper, onnxruntime, sounddevice,
  gradio_client, rapidfuzz, jellyfish, numpy, pyzbar, picamera2) →
- `systemctl --failed` / relevant `systemctl list-units` →
- service name that launches the app + how it gets its code →

## Voice pipeline measurements
- raw mic recording saved (git-ignored) + RMS/peak/dBFS/SNR/clipping →
- VAD pre/post-roll, first/last-word integrity →
- A/B local vs cloud STT transcripts + latency →
- end-to-end latency per stage (see `voice_diagnostics.py` PHASE O) →

## Bootstrap status
- `~/dose-home-station/claude-bootstrap-status.json` (sanitized) →

## Root causes found & fixes applied
-
