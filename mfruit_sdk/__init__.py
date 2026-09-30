"""MFruit App SDK: one input controller and one look for every MFruit OS app.

The source lives here, inside MFruit OS, which uses it itself. Apps carry a
copy named ``mfruit_sdk`` (``scripts/sdk-sync.sh <app dir>``), so they run
with or without MFruit OS installed. Every module uses relative imports only,
which is what lets the same files work under either package name.

    keys       USB / Bluetooth keyboards from /dev/input (no Pillow needed)
    gestures   one button -> tap, 2x, 3x, 4x, hold
    input      InputController: button + keyboard -> the MFruit OS actions
    status     WiFi level and battery for the status bar
    ui         theme, fonts, status bar, footer hints, lists, toast, RGB565

See APP_DEVELOPMENT.md ("MFruit App SDK") and docs/APP_RULES.md.
"""

SDK_VERSION = "1.2.0"
