# DOSE Home Station — Safety, Security, Privacy & Legal Notes

Prototype documentation. Honest scope statement up front: **no system
can be guaranteed unhackable or error-free, and this document does not
claim legal compliance** — it records the design safeguards, what has
been tested, what remains open, and what needs qualified review before
any real-world deployment with real medication.

## 1. Architecture (control plane vs. conversational plane)

```
 Microphone ─► Vosk STT (local) ─► Wake detector ("Hey Dose")
                                        │ text only
                                        ▼
                              Intent engine (deterministic,
                              no LLM, no network, dose_voice.py)
                                        │ intent id + argument
                                        ▼
                         Dispatcher (fixed intent allowlist)
                          │                        │
                informational replies      navigation requests only
                          │                        ▼
                    Piper TTS (local)      Tk UI (dose_app.py)
                                                   │
                                       PHYSICAL TOUCH ONLY:
                                       hold 3 s ► spin ► press CONFIRM
                                                   │
                                        dose logged / count changed
```

- The **conversational plane** (voice) is deterministic pattern
  matching — there is **no LLM anywhere**, so there is nothing to
  jailbreak into tool use, and replies are template-built.
- The **control plane** is the touchscreen. It is the *only* code path
  that can decrement a pill count or log a dose, and it requires three
  distinct physical acts (hold, spin, press). The voice assistant has
  no code path to it — its "dispense" intent only navigates to the
  home screen and explains where to press.
- All voice → UI communication crosses one bridge
  (`root.after` marshaling), and the voice thread can only call the
  same fixed intent set that the matcher can.

## 2. Data flow & retention

| Data | Where | Retention | Notes |
|---|---|---|---|
| Microphone audio | RAM ring (≤30 s/utterance) | discarded immediately after transcription | never written to disk except a temp WAV deleted right after optional 2nd-stage STT |
| TTS audio | temp WAV | deleted right after playback | |
| Transcripts | RAM | discarded after each exchange | shown on screen live for verification |
| Learned phrases/aliases | `voice/learning.json` | until user deletes | only from explicit user corrections; ≤300 entries, ≤200 chars each; voice command "forget everything you learned" wipes it |
| Medication data | `med_data.json` | until user changes it | names sanitized/length-capped at the QR boundary |
| Adherence log | `adherence_log.json` | indefinitely (product feature) | timestamps + med keys only; no audio, no free text |
| Camera frames | RAM | overwritten every cycle | QR decode only; never stored |

No accounts, no passwords, no tokens, no cloud services, no analytics,
no telemetry. Nothing leaves the device except the user-initiated
GitHub update check (TLS, certificate-validated).

## 3. Voice safety properties (tested)

- Wake word processed locally; assistant only listens for commands
  after the wake word (or during an explicit conversation it started).
- No action from partial transcripts; input muted while speaking
  (no self-triggering).
- One intent per utterance; conversations time out; "cancel" works at
  every step; a Settings toggle disables the microphone pipeline.
- The assistant always identifies as an AI, never claims an action
  succeeded that it didn't observe, and never claims to dispense.
- **Medical-safety gate checked before everything else** (including
  the learning layer): dose-change / interaction / safety questions
  get a hard referral to a pharmacist or doctor; recognized emergency
  phrases get a 911 + poison-control referral. Medication guidance is
  always attributed to the stored label and disclaimed — never a
  recommendation from Dose.
- Voice is treated as an input method, **never as authentication** —
  which is enforced structurally by the no-actuation rule above.

## 4. Learning safeguards

- Instance-based only (stored phrases → fixed intent ids). No model
  weights change; nothing can be learned except routes into the same
  safe intent allowlist.
- A poisoned/corrupted learning file fails closed (unknown intent →
  safe no-op; unparseable file → empty store). Bounded size.
- The medical/emergency gate cannot be shadowed by any learned phrase.

## 5. Input boundaries

- **QR codes are untrusted**: only DOSE-format JSON with known slots
  is accepted; names are stripped to printable characters and capped
  at 40 chars; payloads over 500 bytes rejected. Fuzzed with 50+
  malformed/hostile payloads in the test suite.
- Spoken text is untrusted data: it is pattern-matched, never
  executed, never used to build commands/paths/SQL (none exist).
  `eval`/`exec`/shell-from-input are not used anywhere.

## 6. Update integrity & residual risks (open items)

- Updates come from this GitHub repo over TLS. **Residual risk**: a
  compromised GitHub account = compromised device. Mitigation ideas
  for production: signed releases, version pinning, rollback.
- raw.githubusercontent CDN can serve ~5-min-stale files; the in-app
  silent check therefore never auto-applies (press UPDATE to apply).
- No inbound network services are opened by this software. The OS
  itself (SSH etc.) is outside this document's scope — harden per
  Raspberry Pi guidance for a real deployment.
- Microphone is always on while the app runs (wake-word listening).
  A hardware mute switch and a visible recording indicator are
  recommended for production hardware; the Settings toggle is
  software-level only.
- Replayed/synthesized audio can trigger the wake word and queries.
  Impact is bounded by design: voice can read schedule/counts (a
  privacy consideration for shared homes) but can never dispense.

## 7. Legal-applicability checklist (flags, not conclusions —
   needs qualified counsel before any real deployment)

- Always-listening device notice/consent (state two-party consent
  laws; bystander notice). Mitigated by local-only processing and
  zero audio retention, but review required.
- FDA / medical-device classification: a device that schedules and
  physically gates prescription medication may qualify as a medical
  device. **Required before marketing beyond a prototype demo.**
- HIPAA applies only to covered entities — likely not a consumer
  prototype, but review at productization.
- No biometrics collected (no voiceprints) — avoids BIPA-class laws.
- COPPA/child use: not designed for children; child-safety review
  required if that changes.
- Voice licensing (checked 2026-09): Piper engine MIT, Vosk
  Apache-2.0, Moonshine MIT — all commercial-friendly. The AMY
  VOICE'S dataset license is NOT clearly stated (its model card
  says "License: See URL" pointing at MycroftAI/mimic3-voices) —
  fine for the prototype/demo, but DO NOT rely on it for a sold
  product without confirming that license or swapping voices.
  Commercial-safe swap candidates in the same Piper format:
  en_US-ljspeech (public-domain dataset, female) or
  en_US-libritts_r (CC BY 4.0, attribution required). Avoid
  en_US-lessac (research-restricted dataset). Cleanest path for a
  shipped product: commission ~1-2 hours of studio recordings from
  a voice actor under a signed commercial release and fine-tune a
  private Piper voice — the assistant then ships with a voice you
  own outright. The app loads whichever .onnx is present, so the
  swap is a file replacement.
- The assistant persona is inspired by, but does not use names,
  dialogue, or assets from, any copyrighted character; no real
  person's voice is cloned.

## 8. Test evidence (all automated, all passing)

- 59-check intent/personality/learning battery (text level)
- 11-check REAL audio round trip (Piper speaks → Vosk hears → brain
  answers → Piper speaks → Vosk verifies), incl. spoken add-med
- 27-check full-app audit under a virtual display: every screen,
  every tap zone, dispense/cancel flows, editor, keyboard, alerts,
  hijack guards, extreme-data anti-clipping, transition cleanup
- 124-check adversarial suite: 30 spoken injection/jailbreak
  attempts, 50+ QR fuzz payloads, learning poisoning, corrupted
  stores, output bounds, no-dispense invariant under attack

Run them from the repo root (models required for the audio suite):
`test_brain.py`, `test_audio_loop.py`, `audit.py`, `test_security.py`
(kept in the development scratchpad; copies can be added to the repo
on request).
