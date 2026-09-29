#!/usr/bin/env bash
# Launch the messenger. This is also what the Whisplay daemon runs when
# the app is picked from the desktop, so it must work from any cwd.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if [ -d .venv ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

export PYTHONUNBUFFERED=1
exec python3 main.py "$@"
