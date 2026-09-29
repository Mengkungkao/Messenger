#!/usr/bin/env bash
# Register the messenger with whisplay-daemon so it appears on the HAT
# desktop as "Messenger".
#
#   ./install.sh                 register
#   ./install.sh --autostart     also launch it at login, through the daemon
#   ./install.sh --no-autostart  remove the autostart unit
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
HERE="$(pwd)"
UNIT=lora-messenger.service

if [ "${1:-}" = "--no-autostart" ]; then
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
    rm -f ~/.config/systemd/user/"$UNIT"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    echo "==> autostart removed; launch it from the HAT desktop"
    exit 0
fi

echo "==> registering with whisplay-daemon"
python3 - "$HERE" <<'REGISTER'
import json, socket, sys

root = sys.argv[1]
body = {
    "version": 1, "cmd": "app.register",
    "payload": {
        "app_id": "whisplay-lora-messenger",
        "display_name": "Messenger",
        "icon": "MS",
        "launch_command": f"{root}/run.sh",
        "cwd": root,
        # The app owns every gesture: single clicks scroll the history,
        # so the daemon's 4-clicks-in-3-seconds exit would fire while
        # reading.
        "exit_gesture": "none",
        # Esc on a plugged-in keyboard cancels typing here (controls/keys.py);
        # left on, the daemon would take it as "quit the app".
        "disable_esc_exit_key": True,
        "priority": 44,
        "persist": True,
        "use_daemon_default_log": True,
    },
}
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect("/tmp/whisplay-daemon.sock")
        client.sendall((json.dumps(body) + "\n").encode())
        reply = json.loads(client.makefile("r").readline())
    print("    ", "registered" if reply.get("ok") else f"failed: {reply}")
except OSError as exc:
    print(f"     daemon not reachable ({exc}); the app still runs with ./run.sh")
REGISTER

if [ "${1:-}" = "--autostart" ]; then
    echo "==> installing systemd user service"
    mkdir -p ~/.config/systemd/user
    # Ask the daemon to launch it rather than running it from systemd:
    # the daemon ties the screen to the process it spawned. See
    # tools/launch_via_daemon.py.
    cat > ~/.config/systemd/user/"$UNIT" <<UNITFILE
[Unit]
Description=LoRa Messenger (launched through whisplay-daemon)
After=whisplay-daemon.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${HERE}
ExecStart=/usr/bin/python3 ${HERE}/tools/launch_via_daemon.py

[Install]
WantedBy=default.target
UNITFILE
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT"
    echo "     enabled. Survives reboot only with: sudo loginctl enable-linger $USER"
fi
