#!/usr/bin/env bash
# Launch the messenger. This is also what the Whisplay daemon runs when
# the app is picked from the desktop, so it must work from any cwd.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# The venv's own python, not `source .venv/bin/activate`: activate has the
# folder the venv was made in written into it, so after Messager became
# Messenger it quietly ran the system python3, without the ASR engine.
# .venv/bin/python finds its venv wherever the folder is now.
PYTHON=python3
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python

export PYTHONUNBUFFERED=1
exec "$PYTHON" main.py "$@"
