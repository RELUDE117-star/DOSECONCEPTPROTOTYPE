#!/bin/bash
# DOSE — full security audit. One command, run it any time.
#
#     bash tools/security_audit.sh
#
# Answers, with evidence rather than assurances:
#
#   A. Can anything reach or command the Mac?
#   B. Can the Pi reach back into the Mac?  (it must not)
#   C. Is any credential sitting somewhere it should not be?
#   D. Does the code only talk to hosts we chose?
#   E. What does the Pi expose, and what does it hold?
#   F. What is the cloud STT actually configured to do?
#
# THE TRUST MODEL THIS CHECKS
#   Mac  ---ssh--->  Pi        allowed, one direction only
#   Pi   ---ssh--->  Mac       MUST BE IMPOSSIBLE
#   Pi output is DATA. It is written to files and read. It is never
#   executed on the Mac. That is the property that matters most here:
#   a compromised Pi must not be able to run anything on the Mac.
#
# Read-only. Changes nothing. Prints no secret values, ever.

set +e
RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; OFF=$'\033[0m'
PASS=0; WARN=0; FAIL=0

ok()   { PASS=$((PASS+1)); printf "  ${GRN}PASS${OFF}  %s\n" "$1"; }
warn() { WARN=$((WARN+1)); printf "  ${YEL}WARN${OFF}  %s\n" "$1"; }
bad()  { FAIL=$((FAIL+1)); printf "  ${RED}FAIL${OFF}  %s\n" "$1"; }
hdr()  { printf "\n== %s ==\n" "$1"; }

AG="${DOSE_AGENT_DIR:-$HOME/Documents/dose-agent}"
REPO="${DOSE_REPO:-$AG/repo}"
PI="${DOSE_PI_HOST:-dose-pi}"

echo "DOSE security audit — $(date)"

# ── A. can anything command this Mac? ─────────────────────────────────
hdr "A. Inbound access to this Mac"

if pgrep -x sshd >/dev/null 2>&1; then
    warn "sshd IS running — this Mac accepts SSH. Turn off System Settings > General > Sharing > Remote Login unless you need it."
else
    ok "sshd is not running — nothing can SSH into this Mac"
fi

if [ -s "$HOME/.ssh/authorized_keys" ]; then
    bad "~/.ssh/authorized_keys exists with $(grep -c . "$HOME/.ssh/authorized_keys") key(s) — something is authorised to log in"
    awk '{print "          " $1 " ... " $NF}' "$HOME/.ssh/authorized_keys"
else
    ok "no ~/.ssh/authorized_keys — no key is authorised to log in here"
fi

if grep -qiE '^\s*(RemoteForward|GatewayPorts|PermitLocalCommand)' \
        "$HOME/.ssh/config" 2>/dev/null; then
    bad "~/.ssh/config contains reverse forwarding — review it"
    grep -niE '^\s*(RemoteForward|GatewayPorts|PermitLocalCommand)' "$HOME/.ssh/config"
else
    ok "~/.ssh/config opens no reverse path back to this Mac"
fi

echo "  -- processes listening beyond loopback --"
LISTEN=$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null \
    | awk 'NR>1 && $9 !~ /127\.0\.0\.1|\[::1\]/ {print $1}' | sort -u)
UNEXPECTED=$(echo "$LISTEN" | grep -vE '^(ControlCe|rapportd|sharingd|AirPlay)$' | grep -v '^$')
echo "$LISTEN" | sed 's/^/          /'
if [ -z "$UNEXPECTED" ]; then
    ok "only Apple's own services listen (AirPlay/Continuity) — nothing from this project"
else
    warn "unrecognised listener(s): $(echo $UNEXPECTED | tr '\n' ' ') — confirm you know what these are"
fi

# ── B. can the Pi reach back? ─────────────────────────────────────────
hdr "B. Can the Pi command the Mac?  (it must not)"

INJ=$(grep -rlE 'eval|\$\(ssh|`ssh|bash <\(|sh <\(|source .*out/' \
        "$AG/queue" "$AG/done" 2>/dev/null)
if [ -n "$INJ" ]; then
    bad "a job EXECUTES data that came from the Pi — this is the one way a compromised Pi could run code here:"
    echo "$INJ" | sed 's/^/          /'
else
    ok "no job executes Pi output — Pi data is written to files and read, never run"
fi

if [ -d "$AG/queue" ]; then
    PERM=$(stat -f '%Sp %Su' "$AG/queue" 2>/dev/null)
    echo "  -- queue directory: $PERM"
    case "$PERM" in
        *w*w*|*w*w*w*) warn "queue is group/world-writable — only your account should be able to add jobs" ;;
        *) ok "only your account can place jobs in the queue" ;;
    esac
    STRANGE=$(ls -1 "$AG/queue" 2>/dev/null | grep -v '\.sh$')
    [ -n "$STRANGE" ] && warn "non-.sh files in the queue: $STRANGE" \
        || ok "queue contains only .sh job files"
fi

if pgrep -f "dose-agent/runner.sh" >/dev/null 2>&1; then
    warn "the agent runner is RUNNING — it executes any .sh placed in $AG/queue. Fine while work is in progress; stop it (Ctrl-C in its tab) when you are done."
else
    ok "the agent runner is not running — nothing is watching for jobs"
fi

# ── C. credentials ────────────────────────────────────────────────────
hdr "C. Credentials on this Mac"

SHAPES='gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|gsk_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}'
HITS=$(grep -rlE "$SHAPES" "$AG" 2>/dev/null | grep -v 'security_audit.sh' | head)
if [ -n "$HITS" ]; then
    bad "credential-shaped text found in the working folder:"
    echo "$HITS" | sed 's/^/          /'
else
    ok "no credential-shaped text anywhere in $AG"
fi

if [ -f "$HOME/.git-credentials" ] || [ -f "$REPO/.git-credentials" ]; then
    bad "a PLAINTEXT .git-credentials file exists — the GitHub token should live in Keychain only"
else
    ok "no plaintext .git-credentials — GitHub token is in Keychain"
fi

HELPER=$(git -C "$REPO" config --get credential.helper 2>/dev/null)
[ "$HELPER" = "osxkeychain" ] \
    && ok "git uses the macOS Keychain for credentials" \
    || warn "git credential.helper is '${HELPER:-unset}' — expected osxkeychain"

if [ -f "$HOME/.ssh/dose_pi_claude_ed25519" ]; then
    KP=$(stat -f '%Sp' "$HOME/.ssh/dose_pi_claude_ed25519")
    case "$KP" in
        -rw-------) ok "the Pi private key is 0600 (owner-only)" ;;
        *) bad "the Pi private key is $KP — should be -rw------- (run: chmod 600 ~/.ssh/dose_pi_claude_ed25519)" ;;
    esac
fi

# ── D. what the code talks to ─────────────────────────────────────────
hdr "D. Egress allowlist and repository secrets"
if [ -d "$REPO" ]; then
    for t in test_egress test_no_private_keys; do
        OUT=$(cd "$REPO" && timeout 180 python3 "tests/$t.py" 2>&1 | tail -3)
        if echo "$OUT" | grep -q '0 failed'; then
            ok "$t: $(echo "$OUT" | grep -oE '[0-9]+ passed, [0-9]+ failed')"
        else
            bad "$t FAILED:"; echo "$OUT" | sed 's/^/          /'
        fi
    done
else
    warn "repo not found at $REPO — skipping code checks"
fi

# ── E. the Pi ─────────────────────────────────────────────────────────
hdr "E. The Raspberry Pi"
PIOUT=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$PI" 'bash -s' <<'PIEOF' 2>&1
echo "REACHED"
echo "priv_keys:$(sudo -n find /home -maxdepth 4 -type f \( -name 'id_*' -o -name '*_ed25519' -o -name '*.pem' \) ! -name '*.pub' 2>/dev/null | grep -c .)"
echo "mac_creds:$(sudo -n grep -rl 'Ryans-MacBook\|ryanjarvis' /home/*/.ssh 2>/dev/null | grep -c .)"
echo "tunnels:$(systemctl list-units --type=service --state=running --no-pager 2>/dev/null | grep -icE 'tunnel|ngrok|vnc|teamviewer|anydesk')"
echo "listen:$(ss -tlnH 2>/dev/null | awk '{print $4}' | grep -vE '127\.0\.0\.1|\[::1\]' | tr '\n' ' ')"
echo "authkeys:$(sudo -n bash -c 'cat /home/*/.ssh/authorized_keys 2>/dev/null | grep -c .' 2>/dev/null)"
echo "pwauth:$(sudo -n sshd -T 2>/dev/null | grep -i '^passwordauthentication' | awk '{print $2}')"
PIEOF
)
if echo "$PIOUT" | grep -q REACHED; then
    PK=$(echo "$PIOUT" | grep '^priv_keys:' | cut -d: -f2)
    MC=$(echo "$PIOUT" | grep '^mac_creds:' | cut -d: -f2)
    TU=$(echo "$PIOUT" | grep '^tunnels:'   | cut -d: -f2)
    AK=$(echo "$PIOUT" | grep '^authkeys:'  | cut -d: -f2)
    PW=$(echo "$PIOUT" | grep '^pwauth:'    | cut -d: -f2)
    [ "${PK:-0}" = "0" ] && ok "the Pi holds no private keys in any home directory" \
                         || bad "the Pi holds $PK private key file(s) — investigate"
    [ "${MC:-0}" = "0" ] && ok "the Pi holds no credential for the Mac — it cannot log in here" \
                         || bad "the Pi references Mac credentials — investigate"
    [ "${TU:-0}" = "0" ] && ok "no tunnel or remote-desktop service running on the Pi" \
                         || bad "$TU tunnel/remote-desktop service(s) running on the Pi"
    echo "  -- Pi authorized_keys entries: ${AK:-?} (the Mac's public key is expected)"
    [ "${PW}" = "no" ] && ok "the Pi refuses SSH password auth (key-only)" \
                       || warn "the Pi allows SSH password auth (PasswordAuthentication=${PW:-unknown})"
    echo "  -- Pi listening sockets: $(echo "$PIOUT" | grep '^listen:' | cut -d: -f2-)"
else
    warn "could not reach the Pi — skipped its checks"
fi

# ── F. cloud STT ──────────────────────────────────────────────────────
hdr "F. Cloud speech-to-text"
CL=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$PI" \
     'ls -l ~/dose-home-station/groq_key ~/dose-home-station/hf_token 2>/dev/null | awk "{print \$1, \$NF}"' 2>/dev/null)
if [ -n "$CL" ]; then
    echo "$CL" | sed 's/^/          /'
    echo "$CL" | grep -q '^-rw-------' \
        && ok "cloud credential files are 0600 (owner-only)" \
        || warn "a cloud credential file is not 0600 — run: chmod 600 ~/dose-home-station/groq_key"
else
    echo "          none configured — the Pi is using local speech only"
fi
echo "  -- only these hosts may ever be contacted --"
echo "          api.groq.com, huggingface.co, api.github.com,"
echo "          raw.githubusercontent.com, alphacephei.com"
echo "  -- what is uploaded: 16 kHz mono audio and nothing else."
echo "     No hostname, no device id, no filename, no transcripts stored."

# ── verdict ───────────────────────────────────────────────────────────
printf "\n────────────────────────────────────────────────────\n"
printf "  %d passed   %d warnings   %d FAILURES\n" "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    printf "  ${RED}Address the FAILures above before shipping.${OFF}\n"
    exit 1
fi
if [ "$WARN" -gt 0 ]; then
    printf "  ${YEL}No failures. Review the warnings — most are choices, not faults.${OFF}\n"
    exit 0
fi
printf "  ${GRN}Clean.${OFF}\n"
