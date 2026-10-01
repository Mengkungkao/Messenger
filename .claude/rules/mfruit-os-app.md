# MFruit OS app rules

This app runs on **MFruit OS**: a 240×280 LCD with rounded corners, one
button, an RGB LED, and optionally a USB or Bluetooth keyboard, on a
Raspberry Pi Zero 2 W or Orange Pi Zero 2W. whisplay-daemon owns the
hardware; MFruit OS launches the app and takes the screen back when it
leaves. Every app must feel like part of the same device. These rules are
the contract; the MFruit App SDK (`mfruit_sdk/`, vendored) implements it.

Source of truth: `MFruitOS/docs/APP_RULES.md`. Copies live in each app at
`.claude/rules/mfruit-os-app.md`; change the source, then copy.

Use this list when creating, developing, packaging and integrating an app.
The detailed package format is in `MFruitOS/APP_DEVELOPMENT.md`. Platform
architecture is documented in `MFruitOS/docs/ARCHITECTURE.md`, and core
contributor workflow in `MFruitOS/CONTRIBUTING.md`. Current SDK apps declare
MFruit OS **1.4.0 or newer** in their native package manifest.

## 1. Input: one controller, the same controls everywhere

- All input goes through `mfruit_sdk.input.InputController`. Never read the
  button edges or `/dev/input` yourself and never write another gesture
  detector: two interpreters of one button is how apps drift apart.
- The controls, in every app and in MFruit OS itself:

  | Action | Button | Keyboard |
  |---|---|---|
  | next | tap | Down, Right, Tab |
  | previous | 2× | Up, Left |
  | select / open | hold, then **release** | Enter |
  | back | 4× | Esc |
  | extra (screen-specific) | 3× | a letter, documented on screen or in the README |
  | talk (talk screens only) | hold — talks while held | Space — talks while held |
  | type | — | letters, digits, Backspace (only where a screen takes text) |

- **Menus and lists** use exactly: tap next · 2× previous · hold open ·
  4× back. A hold only *arms* at the threshold — MFruit OS's long press,
  `long_press_ms=700` — (`on_armed(True)`: show "release to …") and acts on
  release, never while the button is down.
- **Back from the app's first screen leaves the app** (4× or Esc). Show a
  short toast ("Exiting") and exit.
- Menus with a parent screen include a visible Back row, selectable with
  the same hold-and-release action as any other row.
- **Talk screens** (push-to-talk, voice input) set `talk=` to return True:
  there, hold or Space talks while held, and 3× opens the selected item
  because the hold is taken. Nowhere else may a hold transmit or record.
  Talking may start sooner than a menu hold arms (`talk_press_ms=350`), so
  the first word is kept; choosing stays a deliberate 700 ms hold.
- While the user is typing (`typing=` returns True), Space types a space.
- **Act only while the app owns the screen.** Pass
  `active=lambda: board.foreground_ready` and call `controller.reset()` when
  focus is revoked. Pass `app_id=APP_ID` to the controller (or inherit
  `WHISPLAY_APP_ID` from mfruit-run). MFruit OS exclusively grabs keyboards
  and forwards keys through its key hub; direct `/dev/input` readers receive
  nothing while it runs. SDK 1.2.0 uses the hub and falls back to evdev only
  without a reachable hub. The controller ignores keys that went down while
  another app had the screen. Background apps must never act on input.
- Footer hints and the handler come from **one table**, so the screen can
  never advertise a gesture the code does not implement.
- A press on a dark (dimmed-off) screen only wakes it.
- **One exception, stated where it applies:** an app whose button logic lives
  outside Python (the existing AI chatbot's Node core, which talks on the
  press and counts 2×/3× itself) keeps it there. That chatbot currently relies
  on the daemon's `quad_click` exit gesture for 4× to leave; do not change its
  registration to `none` until the Node core owns exit too. Its keyboard
  still goes through `InputController`, in the Python process that owns the
  screen. This is a compatibility exception, not the template for new apps.

## 2. Registration and lifecycle

- Register with `exit_gesture: "none"` (the app implements 4× = back) and
  `disable_esc_exit_key: true` in the packaging JSON **and** call
  `mfruit_sdk.daemon.own_escape_key(APP_ID)` at start-up (the Whisplay
  runtime client's `register()` does not send that flag) — right after
  registering and **before taking the screen**: every `app.register` makes
  whisplay-daemon redraw its own desktop, which flashes over an app that
  already owns the screen.
- The existing Node chatbot retains `exit_gesture: "quad_click"` under the
  compatibility exception above. Track that exception explicitly when
  assessing native package readiness.
- Preserve MFruit OS's managed `mfruit-run` launch command. A native or
  adopted app must not replace it with its standalone command at startup
  or during an install hook. Let MFruit OS own managed registration and
  autostart; keep standalone setup separate.
- Draw the first frame as soon as possible: MFruit OS shows "Opening <App>"
  until the app's first frame.
- On `app_exit_requested`, stop within 3 seconds. When the user leaves, exit
  completely — MFruit OS stops the process group 3 s later — unless the
  manifest sets `"background": true` (the app must keep receiving, e.g.
  messages); a background app releases the screen and stays quiet.
- Never keep the screen while not in the foreground; never fight for focus.

## 3. Screen layout and look

- Status bar on every screen: `mfruit_sdk.ui.status_bar(canvas, page_name,
  status)` — page name top-left (bold 17), WiFi and battery top-right from
  `mfruit_sdk.status.StatusMonitor`. No app-name header row; the page name
  is the title. An app state light goes in `dot=` / `badge=`.
- Content between `CONTENT_TOP` (40) and `CONTENT_BOTTOM` (248); footer hints
  with `mfruit_sdk.ui.footer()` at `FOOTER_Y` (256). Keep text 20 px
  (`CORNER_INSET`) from the side edges near the top and bottom corners.
- Lists use `mfruit_sdk.ui.draw_list` rows (or the same geometry); toasts use
  `toast()`; empty and error states use `message()`.
- Colours from `mfruit_sdk.ui.theme.DARK`; fonts from `mfruit_sdk.ui.fonts`
  (Inter from MFruit OS, DejaVu elsewhere). An app may keep one brand accent
  inside its content (e.g. Bitcoin orange); chrome stays MFruit.
- Sentence case for words on screen ("Refreshing…", "Network error"), not
  ALL CAPS. Hint labels: `tap`, `2×`, `3×`, `4×`, `hold`, `release`.
- Render only when something changed; convert with
  `mfruit_sdk.ui.to_rgb565` (no numpy needed).

## 4. The vendored SDK

- `mfruit_sdk/` is a copy of `MFruitOS/mfruitos/sdk`. Do not edit it in the
  app. Change it in MFruit OS (with its tests), then run
  `MFruitOS/scripts/sdk-sync.sh <dir containing mfruit_sdk>`;
  `sdk-sync.sh <dir> --check` reports a stale copy.
- The SDK imports only itself, the standard library and Pillow (UI only),
  and runs on Python 3.9+ (the Orange Pi has Python 3.10 and Pillow 9.0).

## 5. Tests and verification

- Controls tests drive the real `InputController` (`threaded=False`, a fake
  clock, `keyboard=False`, `key_event()` for keys) into the app's real
  dispatch: every action on every screen, hints matching handlers, keys
  that went down elsewhere ignored, nothing while inactive.
- A rendering test composes each screen with the chrome and checks content
  stays between the status bar and the footer.
- On hardware, check with the button **and** a keyboard: navigate, open,
  back out of the app, and confirm that typing while another app is on
  screen does nothing in this one. Report anything not verified as such.

## 6. Create and develop an app

- [ ] Start from `MFruitOS/templates/whisplay-app-template`, including its
  `.claude/rules/mfruit-os-app.md`. Choose a stable app id and use it in the
  manifest, daemon registration and `InputController(app_id=...)`.
- [ ] Use one owner for hardware: whisplay-daemon. Subscribe to its events
  and use its shared framebuffer; do not create a second GPIO, display or
  keyboard owner while MFruit OS runs.
- [ ] Separate app state, actions and drawing. Keep imports free of device
  operations so a smoke test can import and render without taking focus.
  Keep slow network, radio and model work away from the input/render path.
- [ ] List every screen and its actions, including loading, empty, offline
  and error states. Use the SDK controls and chrome from sections 1–3.
  Show a useful recovery action when a dependency or service is unavailable.
- [ ] Keep code in `WHISPLAY_OS_APP_DIR` and user data in
  `WHISPLAY_OS_APP_DATA`. Treat the version folder as replaceable. Do not
  commit credentials, generated recordings, personal messages or device
  configuration; provide examples with placeholders.
- [ ] Refresh the SDK with `MFruitOS/scripts/sdk-sync.sh <python root>` and
  copy this canonical rules file when its contract changes. Verify SDK
  parity with `sdk-sync.sh <python root> --check`.
- [ ] Test real dispatch through the SDK, all screens and rendering bounds,
  focus loss during a press, restart/reconnect and app exit. Record the
  tested OS, Python and Pillow versions and any hardware checks outstanding.

## 7. Package for production

- [ ] Put `manifest.json` at the package root. Set stable `id`, `name`,
  semantic `version`, a real relative `entrypoint` and the correct
  `repository`. Declare `min_os_version: "1.4.0"` or the newer version the
  app actually requires, `exit_gesture: "none"` and
  `disable_esc_exit_key: true`. Declare `background: true` only when work
  must continue after leaving, and test that it stays silent and unfocused.
- [ ] Include the actual entrypoint, required assets, vendored SDK and this
  rules file. Shell entrypoints and hooks use LF endings, a valid shebang
  and executable permissions. Use `exec` for the final app process so
  shutdown and exit status reach MFruit OS correctly.
- [ ] Make installation repeatable and noninteractive. `install.sh`, when
  needed, runs on initial install, reinstall, update and downgrade. Check
  prerequisites with clear errors; never require an interactive `sudo`
  prompt. Keep app dependencies in a venv in the data directory, or use an
  explicitly persisted venv. The SDK needs Pillow for UI; frame conversion
  does not require NumPy.
- [ ] Declare a fast, deterministic `test` hook that imports the app and
  checks essential local behaviour. It must not claim the display, send
  messages, transmit radio audio or require network credentials. A failure
  must exit nonzero so MFruit OS can retain the previous version.
- [ ] Supply `update.sh` only for necessary migrations; handle upgrades and
  downgrades without losing user data. Use `persist` only for intended
  user-managed paths. If installation changes anything outside the app
  folder, provide an `uninstall.sh` that reverses those changes.
- [ ] When converting an existing clone, migrate model paths, external data
  directories and assumptions about sibling repositories explicitly.
  Installer snapshots cover managed app data, not every file in the user's
  home. Do not present a manifest-only conversion as a tested migration.
- [ ] Exclude `__pycache__`, bytecode, test caches, local logs, real `.env`
  files and personal runtime data from the release. Retain needed assets,
  vendored SDK source and built runtime output such as a Node app's `dist`.
  A source-only Node archive is incomplete if the installer does not build it.
- [ ] Run `python3 MFruitOS/scripts/check-app.py <package>` against the exact
  package directory, plus app tests. Resolve failures before testing release
  installation. The checker verifies static structure and contract settings;
  it does not certify visual quality, runtime behaviour or hardware support.

## 8. Integrate, release and record evidence

- [ ] Sideload the actual package on a configured device with
  `mfruitctl sideload <folder-or-archive>`. Launch it from Home and through
  `mfruitctl launch <id>`. Confirm the first frame, correct status/footer
  layout, loading/error states and return to Home.
- [ ] Test button and USB/Bluetooth keyboard actions separately, including
  Back rows, Esc, wake-only presses and keys held across a focus change.
  Verify that a background app receives no foreground input or drawing time.
- [ ] Exercise app-specific hardware and services: audio capture/playback,
  radio delivery, pairing, Wi-Fi reconnect or API responses as applicable.
  Automated SSH checks cannot verify perceived sound, physical buttons or
  LED colours; record those checks separately.
- [ ] Test clean install, reinstall, update, failed smoke-test rollback,
  manual rollback and removal using disposable test data. Confirm intended
  data survives updates and explain what removal deletes. Check logs and
  that closing the app leaves no unwanted processes, ports or focus claims.
- [ ] Publish only after the required checks pass. Match release tag and
  manifest version (`v1.2.0` ↔ `"1.2.0"`); add GitHub topic `whisplay-app` for
  discovery. Prefer a release archive with SHA-256 verification material.
- [ ] Record the tested commit/version, package, target device, automated
  results, manual results and remaining limitations in the release notes.
  Mark untested checks as untested, and distinguish app failures from absent
  hardware, credentials or services.

An app adopted from whisplay-daemon's registry can run under MFruit OS
without being a complete native release package. A draft manifest under
`contrib/manifests/` is not a replacement for the app's own package,
dependency installation/build steps and smoke test. Do not describe an app
as production-ready solely because SDK parity or an automated suite passes.
