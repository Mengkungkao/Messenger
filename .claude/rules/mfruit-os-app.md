# MFruit OS app rules

This app runs on **MFruit OS**: a 240×280 LCD with rounded corners, one
button, an RGB LED, and optionally a USB or Bluetooth keyboard, on a
Raspberry Pi Zero 2 W or Orange Pi Zero 2W. whisplay-daemon owns the
hardware; MFruit OS launches the app and takes the screen back when it
leaves. Every app must feel like part of the same device. These rules are
the contract; the MFruit App SDK (`mfruit_sdk/`, vendored) implements it.

Source of truth: `MFruitOS/docs/APP_RULES.md`. Copies live in each app at
`.claude/rules/mfruit-os-app.md`; change the source, then copy.

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
- **Talk screens** (push-to-talk, voice input) set `talk=` to return True:
  there, hold or Space talks while held, and 3× opens the selected item
  because the hold is taken. Nowhere else may a hold transmit or record.
  Talking may start sooner than a menu hold arms (`talk_press_ms=350`), so
  the first word is kept; choosing stays a deliberate 700 ms hold.
- While the user is typing (`typing=` returns True), Space types a space.
- **Act only while the app owns the screen.** Pass
  `active=lambda: board.foreground_ready` and call `controller.reset()` when
  focus is revoked. The keyboard is shared by every process (nobody grabs
  it); the controller ignores keys that went down while another app had the
  screen. An app that keeps running in the background must never act on
  keys or the button.
- Footer hints and the handler come from **one table**, so the screen can
  never advertise a gesture the code does not implement.
- A press on a dark (dimmed-off) screen only wakes it.
- **One exception, stated where it applies:** an app whose button logic lives
  outside Python (the AI chatbot's Node core, which talks on the press and
  counts clicks itself) keeps it there, provided its gestures match MFruit
  OS's (hold talks, 4× leaves). Its keyboard still goes through
  `InputController`, in the Python process that owns the screen.

## 2. Registration and lifecycle

- Register with `exit_gesture: "none"` (the app implements 4× = back) and
  `disable_esc_exit_key: true` in the packaging JSON **and** call
  `mfruit_sdk.daemon.own_escape_key(APP_ID)` at start-up (the Whisplay
  runtime client's `register()` does not send that flag) — right after
  registering and **before taking the screen**: every `app.register` makes
  whisplay-daemon redraw its own desktop, which flashes over an app that
  already owns the screen.
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
