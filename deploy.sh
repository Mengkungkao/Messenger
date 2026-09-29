#!/usr/bin/env bash
# Copy the messenger to a Raspberry Pi or Orange Pi, and optionally set it up.
#
#   ./deploy.sh jarvis@192.168.1.120
#   ./deploy.sh jarvis@192.168.1.120 --setup              then run ./setup.sh there
#   ./deploy.sh jarvis@192.168.1.120 --setup --asr vosk   lighter ASR (Pi Zero 2 W)
#
# --setup runs setup.sh over an interactive ssh session: it asks before
# changing anything, and sudo may want the device's password.
#
# config.yaml is copied only where there is none yet: each radio keeps its
# own, and the venv and downloaded models stay on the device.
set -euo pipefail

RUN_SETUP=0
SETUP_ARGS=()
POSITIONAL=()
while [ $# -gt 0 ]; do
    case "$1" in
        --setup) RUN_SETUP=1 ;;
        --asr)   SETUP_ARGS+=(--asr "$2"); shift ;;
        --yes)   SETUP_ARGS+=(--yes) ;;
        *)       POSITIONAL+=("$1") ;;
    esac
    shift
done
TARGET="${POSITIONAL[0]:-}"
REMOTE_DIR="${POSITIONAL[1]:-Messager}"
if [ -z "$TARGET" ]; then
    echo "usage: $0 user@host [remote-dir] [--setup] [--asr faster-whisper|vosk|none] [--yes]" >&2
    exit 1
fi

HERE="$(dirname "$(readlink -f "$0")")"
SSH_OPTS=(-o ConnectTimeout=25)   # a Zero 2 W on Wi-Fi is slow to answer

echo "==> syncing to ${TARGET}:${REMOTE_DIR}"
rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.pytest_cache' --exclude 'models' --exclude 'config.yaml' \
    -e "ssh ${SSH_OPTS[*]}" \
    "${HERE}/" "${TARGET}:${REMOTE_DIR}/"
rsync -az --ignore-existing -e "ssh ${SSH_OPTS[*]}" \
    "${HERE}/config.yaml" "${TARGET}:${REMOTE_DIR}/config.yaml"

if [ "$RUN_SETUP" = 1 ]; then
    echo
    echo "==> running setup on ${TARGET}"
    exec ssh -t "${SSH_OPTS[@]}" "$TARGET" "cd ${REMOTE_DIR} && ./setup.sh ${SETUP_ARGS[*]}"
fi

echo
echo "==> deployed. On the device:"
echo "    cd ${REMOTE_DIR} && ./setup.sh     # first time: packages, ASR, checks, register"
echo "    ./run.sh                           # or pick 'Messenger' on the HAT desktop"
