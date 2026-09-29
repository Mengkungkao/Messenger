#!/usr/bin/env bash
#
# Set up the LoRa Messenger on a Raspberry Pi or an Orange Pi Zero 2W
# (Whisplay HAT + Waveshare SX126X LoRa HAT).
#
#   ./setup.sh                      walk through every step, asking before changes
#   ./setup.sh --yes                accept every prompt
#   ./setup.sh --check              report only; change nothing
#   ./setup.sh --asr vosk           lighter ASR, for a 512 MB Pi Zero 2 W
#
# The board-level fixes -- UART on, serial console off the LoRa port, the
# Whisplay DC patch, provisioning the module -- are the same as for
# WalkieTalkie. A board that already runs WalkieTalkie passes them as-is.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
HERE="$(pwd)"

ASSUME_YES=0
CHECK_ONLY=0
ASR=auto
while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)   ASSUME_YES=1 ;;
        -n|--check) CHECK_ONLY=1 ;;
        --asr)      ASR="$2"; shift ;;
        -h|--help)  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done
case "$ASR" in auto|faster-whisper|vosk|none) ;; *) echo "--asr: faster-whisper, vosk or none" >&2; exit 1 ;; esac

BOLD=$(tput bold 2>/dev/null || true); RESET=$(tput sgr0 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true); GREEN=$(tput setaf 2 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true)
STEP=0; STEPS=7; FAILURES=0
step() { STEP=$((STEP + 1)); echo; echo "${BOLD}==> ${STEP}/${STEPS}  $*${RESET}"; }
ok()   { echo "    ${GREEN}ok${RESET}   $*"; }
warn() { echo "    ${YELLOW}warn${RESET} $*"; }
bad()  { echo "    ${RED}fail${RESET} $*"; FAILURES=$((FAILURES + 1)); }
info() { echo "         $*"; }
ask() {
    [ "$CHECK_ONLY" = 1 ] && { info "(check only: skipping)"; return 1; }
    [ "$ASSUME_YES" = 1 ] && return 0
    read -r -p "         $1 [y/N] " reply
    [[ "$reply" =~ ^[Yy] ]]
}

# ------------------------------------------------------------------ host
step "Host"
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
ok "$MODEL · $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") · python $(python3 -V 2>&1 | cut -d' ' -f2)"
MEM_MB=$(awk '/MemTotal/ { print int($2 / 1024) }' /proc/meminfo)
# Measured peak memory while transcribing: faster-whisper tiny.en 315 MB,
# vosk small 199 MB. A Zero 2 W has 415 MB, ~140 MB of it free.
if [ "$ASR" = auto ]; then
    ASR=faster-whisper
    [ "$MEM_MB" -lt 900 ] && ASR=vosk
    ok "${MEM_MB} MB of RAM: using $ASR for speech recognition"
elif [ "$ASR" = faster-whisper ] && [ "$MEM_MB" -lt 900 ]; then
    warn "${MEM_MB} MB of RAM: Whisper needs ~315 MB and will swap. Consider --asr vosk."
fi

# -------------------------------------------------------------- packages
step "System packages"
PACKAGES=(python3-serial python3-yaml python3-pil python3-numpy python3-venv alsa-utils)
OPTIONAL=(espeak-ng)   # only for reading messages aloud
MISSING=()
for pkg in "${PACKAGES[@]}" "${OPTIONAL[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 && ok "$pkg" || { warn "$pkg missing"; MISSING+=("$pkg"); }
done
if [ ${#MISSING[@]} -gt 0 ]; then
    if ask "install ${MISSING[*]} with apt?" && sudo apt-get update -qq \
            && sudo apt-get install -y "${MISSING[@]}"; then
        ok "installed"
    else
        # Declined, check-only, or apt failed: only required ones count.
        REQUIRED_MISSING=()
        for pkg in "${MISSING[@]}"; do
            [[ " ${OPTIONAL[*]} " == *" $pkg "* ]] || REQUIRED_MISSING+=("$pkg")
        done
        if [ ${#REQUIRED_MISSING[@]} -gt 0 ]; then
            bad "needed: sudo apt install ${REQUIRED_MISSING[*]}"
        else
            info "optional, for read-aloud: sudo apt install ${MISSING[*]}"
        fi
    fi
fi
id -nG "$USER" | tr ' ' '\n' | grep -qx dialout && ok "$USER is in dialout" \
    || { warn "$USER is not in the dialout group"; ask "add?" && sudo usermod -aG dialout "$USER" \
         && info "log out and back in for it to apply"; }

# ------------------------------------------------------------------- ASR
step "Speech recognition ($ASR)"
if [ "$ASR" = none ]; then
    info "skipped: messages can still be typed"
else
    if [ ! -x .venv/bin/python ]; then
        if ask "create .venv (it sees the apt packages too)?"; then
            python3 -m venv --system-site-packages .venv && ok ".venv created" || bad "venv failed"
        fi
    fi
    if [ -x .venv/bin/python ]; then
        PIP_PKG=$ASR; [ "$ASR" = vosk ] && PIP_PKG="vosk"
        if .venv/bin/python -c "import ${ASR//-/_}" 2>/dev/null; then
            ok "$ASR installed"
        elif ask "pip install $PIP_PKG pytest into .venv?"; then
            # Wheels only for the compiled parts: without one for this
            # board, building CTranslate2 on a Zero 2 W takes hours or runs
            # out of memory. Better to fail here and suggest --asr vosk.
            .venv/bin/pip install -q --only-binary=ctranslate2,onnxruntime,av,tokenizers,vosk \
                "$PIP_PKG" pytest && ok "installed" \
                || bad "pip install failed (no wheel for this board? try --asr vosk)"
        fi
        if [ "$ASR" = faster-whisper ] && [ "$CHECK_ONLY" = 0 ]; then
            info "fetching the tiny.en model (~75 MB, once)"
            .venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', compute_type='int8')" \
                && ok "tiny.en model ready" || bad "model download failed"
        fi
        if [ "$ASR" = vosk ]; then
            VOSK_MODEL=models/vosk-model-small-en-us-0.15
            if [ -d "$VOSK_MODEL" ]; then
                ok "$VOSK_MODEL"
            elif ask "download the small English vosk model (~40 MB)?"; then
                mkdir -p models && curl -fL -o models/vosk.zip \
                    https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip \
                    && (cd models && python3 -m zipfile -e vosk.zip . && rm vosk.zip) \
                    && ok "$VOSK_MODEL" || bad "vosk model download failed"
            fi
            USING=$(python3 -c "import config; a = config.load().asr; print(a.engine, a.model)" 2>/dev/null | tail -1)
            if [ "$USING" = "vosk $HERE/$VOSK_MODEL" ]; then
                ok "config.yaml uses it"
            elif [ -d "$VOSK_MODEL" ] && ask "point config.yaml at vosk?"; then
                sed -i -E "s|^(  engine:).*|\1 vosk|; s|^(  model:).*|\1 $HERE/$VOSK_MODEL|" config.yaml
                ok "config.yaml: asr.engine vosk"
            fi
        fi
    fi
fi

# ----------------------------------------------------------- serial port
step "LoRa serial port"
PORT=$(python3 -c "import config; print(config.load().radio.port)" 2>/dev/null || echo /dev/ttyS0)
if [ -e "$PORT" ]; then
    ok "$PORT exists"
    if grep -qE "console=(serial0|${PORT##*/})" /proc/cmdline; then
        bad "the kernel console is on $PORT: it corrupts every packet"
        info "Raspberry Pi: remove console=serial0,115200 from /boot/firmware/cmdline.txt"
        info "Orange Pi:    set console=display (or none) in /boot/orangepiEnv.txt"
        info "then: sudo systemctl mask serial-getty@${PORT##*/}; sudo reboot"
        [ -x ../WalkieTalkie/setup.sh ] && info "(../WalkieTalkie/setup.sh does all of this)"
    else
        ok "no kernel console on $PORT"
    fi
    if fuser "$PORT" >/dev/null 2>&1; then
        warn "$PORT is held by: $(fuser -v "$PORT" 2>&1 | tail -1 | awk '{print $NF}')"
        info "one process per radio: stop WalkieTalkie (or a getty) before the messenger"
    else
        ok "$PORT is free"
    fi
else
    bad "$PORT does not exist: enable the UART (enable_uart=1 on a Pi) and reboot"
fi

# ---------------------------------------------------------- Whisplay HAT
step "Whisplay HAT daemon"
RUNTIME=""
for dir in "${WHISPLAY_RUNTIME:-}" "$HOME/Whisplay/runtime" "$HERE/../Whisplay/runtime" \
           /opt/whisplay/runtime /usr/local/share/whisplay/runtime; do
    [ -n "$dir" ] && [ -f "$dir/whisplay_client.py" ] && { RUNTIME=$dir; break; }
done
if [ -z "$RUNTIME" ]; then
    bad "no Whisplay runtime: install PiSugar's driver first"
    info "git clone --depth 1 https://github.com/PiSugar/Whisplay.git ~/Whisplay"
else
    ok "runtime at $RUNTIME"
    # The stock driver leaves DC -- the radio's M1 -- high after drawing,
    # which parks the module in configuration mode: deaf and mute.
    if grep -q "DC doubles as the LoRa module's M1" "$RUNTIME/whisplay.py" 2>/dev/null; then
        ok "the driver parks DC (the radio's M1) low"
    else
        warn "the driver leaves DC high after each frame, and DC is the radio's M1"
        if ask "apply docs/whisplay-dc-fix.patch and restart whisplay-daemon?"; then
            (cd "$(dirname "$RUNTIME")" && patch -p1 --forward -s < "$HERE/docs/whisplay-dc-fix.patch") \
                && sudo systemctl restart whisplay-daemon && ok "patched" || bad "patch failed"
        else
            bad "the radio cannot send or receive until DC is parked low"
        fi
    fi
    if systemctl is-active --quiet whisplay-daemon; then
        ok "whisplay-daemon is running"
        [ "$CHECK_ONLY" = 0 ] && ./install.sh | sed 's/^/    /'
    else
        warn "whisplay-daemon is not running: the app will drive the HAT directly"
    fi
fi

# ------------------------------------------------------------- the radio
step "Radio module"
info "The module keeps its frequency and air rate in non-volatile memory."
info "A module already provisioned for WalkieTalkie is ready as it is."
info "Otherwise, once, on a Raspberry Pi:"
info "    sudo systemctl stop whisplay-daemon"
info "    python3 provision_radio.py --frequency 868"
info "    sudo systemctl start whisplay-daemon"

# ----------------------------------------------------------------- tests
step "Tests"
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
if $PY -m pytest tests -q >/tmp/messenger-tests.log 2>&1; then
    ok "$(tail -1 /tmp/messenger-tests.log)"
else
    bad "tests failed -- see /tmp/messenger-tests.log"
fi

echo
[ "$FAILURES" -eq 0 ] && echo "${BOLD}${GREEN}Ready.${RESET}" \
    || echo "${BOLD}${YELLOW}Finished with $FAILURES issue(s) above.${RESET}"
echo "Start it:  ./run.sh   (or pick 'Messenger' on the HAT desktop)"
echo "Controls:  hold = talk · 1 click = history · 2 = resend/live · 3 = read aloud · 4 = exit"
