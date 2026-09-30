# LoRa Messenger

A chat between two radios. Hold the button and talk: the Pi turns your
speech into text **on the device** and sends it over LoRa. The other radio
shows it in the chat and sends back an ACK, and yours marks the message
**✓**. Instead of talking, you can pick a quick reply with the button, or
type on a keyboard plugged into the board. No internet, no gateway, no
cloud ASR.

It is an **MFruit OS app**: MFruit OS's status bar, fonts, lists and
footer hints, and the controls every MFruit app shares (the vendored MFruit
App SDK in `mfruit_sdk/`), with a **USB or Bluetooth keyboard** working
wherever the button does.

Hardware per radio: a Raspberry Pi or Orange Pi Zero 2W, a **Whisplay HAT**
(240×280 LCD, one button, RGB LED, microphone and speaker) and a
**Waveshare SX126X LoRa HAT** (E22-900T22S). This is a sibling of
[WalkieTalkie](../WalkieTalkie): it runs on the same hardware and reuses
WalkieTalkie's radio driver, Whisplay daemon client and recorder, all
proven on these HATs.

<img src="docs/screen.png" width="240" alt="The chat: the other radio's name, WiFi and battery at the top; received messages on the left, sent on the right with a tick; what the button does at the bottom">

### Using it

The screen is a chat with the other radio, under MFruit OS's status bar
(the other radio's name, WiFi, battery). It shows the latest 10 messages:
received on the left (with the signal they came in at), sent on the
right. Under each sent message is **✓** (delivered), *sending…* or
**✗ not confirmed**. The footer always says what the button does right
now; while something is happening (listening, sending, an error) a
coloured bar says what instead.

**On the chat** — a talk screen: holding talks.

| Button | Keyboard | |
|---|---|---|
| **hold** | **Space**, held | talk; let go to send. On a radio without speech recognition, a hold opens the quick replies instead |
| **tap** | ↑ | scroll to an older message; past the oldest (button), back to the newest |
| | ↓ | a newer message |
| **2 clicks** | Tab, Enter | quick replies. On a failed message you have scrolled to (2 clicks): send it again |
| **3 clicks** | | read the newest message aloud, or the one scrolled to (needs `espeak-ng`) |
| **4 clicks** | Esc | leave the app |
| | letters | type a message: **Enter** sends, **Esc** cancels, **Backspace** deletes; Space is a space once you are typing |

**In the quick replies** — a list, as everywhere in MFruit OS.

| Button | Keyboard | |
|---|---|---|
| **tap** / **2 clicks** | ↓, Tab / ↑ | next / previous reply |
| **hold**, then release | Enter | send it |
| **4 clicks** | Esc | back to the chat (it also closes by itself after 20 s) |

Talking starts 0.35 s into a hold (`input.hold_ms`), so the first word
is kept; in the list a hold is MFruit OS's deliberate long press, 0.7 s
(`input.long_press_ms`), and sends when you let go — the footer says
**release to send** once it is armed.

The list starts with **↻ Resend** when your last message was not
confirmed. The replies are set in `messaging.quick_replies`.

**A keyboard** (USB or Bluetooth) can be plugged in or paired at any
time; it is picked up at once. Keys only count while the Messenger has
the screen: typing into another app, while the Messenger keeps receiving
in the background, never reaches it.

### Without Wi-Fi or internet

It needs neither. Messages go radio to radio over LoRa, speech is
recognised on the board, and the screen and button are local.
Internet is needed only to **install**: `./setup.sh` fetches the
packages, the speech engine and its model, once.

- **faster-whisper** loads its model from the local cache, without
  asking the internet whether it is still current. That question used
  to cost 135 s on Wi-Fi with no internet, such as a phone hotspot with
  no data, while the screen said "Loading speech model…". **Vosk** only
  ever reads its model folder.
- **The clock** comes from the internet. Neither board has a
  battery-backed clock, so without internet the times in the chat can be
  wrong after a power cut. Messages, duplicate checks and delivery do not
  depend on the time of day.

---

## How it works

```
 Raspberry Pi                                   Orange Pi
 ─────────────                                  ─────────
 hold button → record (48 kHz, pre-roll)        SX126x → UART bytes
 → resample to 16 kHz                           → deframer: START, CRC-16
 → ASR (faster-whisper / vosk)                  → for me? duplicate?
 → show "TX → all: …" locally                   → ACK straight back
 → packet → SX126x ── LoRa 868 MHz ──────────►  → show "RX: RasPi" + text + dBm
 ← "Message delivered ✓"  ◄──────── ACK ──────
```

Transcription runs on its own thread, so the radio keeps receiving and
ACKing while Whisper works. The main loop never polls: it sleeps until a
button press, a packet or a finished transcription wakes it.

### Packet format

```
+-------+------+--------+--------+--------+--------+-----------+----------+
| START | TYPE |   ID   |  SRC   |  DST   | LENGTH |  MESSAGE  |  CRC-16  |
| 0xAA  |  1   | 2 (BE) | 2 (BE) | 2 (BE) |   1    |  0..200   |  2 (BE)  |
+-------+------+--------+--------+--------+--------+-----------+----------+
TYPE: 0x01 TEXT · 0x02 ACK · 0x03 HELLO · 0x04 HELLO_REPLY · 0x05 PING
```

This is the brief's START / TYPE / ID / LENGTH / MESSAGE with two
additions:

- **SRC/DST addresses.** An ACK has to find its way back to the sender,
  and the receiver needs to know who spoke to show "RX: OrangePi". The
  module could do the addressing itself, but only by reprovisioning,
  which needs the M0/M1 pins the LCD owns. So the addresses travel in
  the packet instead.
- **CRC-16/CCITT-FALSE** over everything after START. The SX1262 has its
  own CRC, but the UART between the Pi and the module has none, and a
  login console left on that port injects bytes the radio's CRC never
  sees.

Details are in [lora/packet.py](lora/packet.py). The deframer copes with
packets split across reads, with noise, and with the RSSI byte the module
appends after each packet. That byte can even be 0xAA (−86 dBm), the same
as START. WalkieTalkie frames on the same channel (`AA 55 …`) are ignored.

### Delivery: ACK, retry, duplicates

| Situation | What happens |
|---|---|
| ACK arrives | **Delivered ✓**, with the round-trip time |
| No ACK in `ack_timeout_seconds` + the round trip's UART and air time (0.9 s for a full message at 9600 bps, 1.4 s at 2400, 2.2 s at 1200) + up to 0.5 s of random jitter | sent again under the same ID, up to `max_retries` more times |
| Still no ACK | **Not confirmed ✗**. Not "not delivered": if only the ACKs were lost, the message did arrive, and the sender cannot tell. Two clicks resend it. |
| An ACK arrives after giving up | quietly marked delivered |
| The receiver gets a repeat | it ACKs again but shows the message only once (duplicates are remembered for 10 minutes by sender and ID) |
| A transcript is over 200 bytes | split at word boundaries into numbered parts ("part 1/2") |

Message IDs are saved across restarts, so a restarted radio never reuses
an ID the other side has just seen. Names are exchanged with
HELLO/HELLO_REPLY at start-up. If both of those are lost, the first TEXT
or ACK from an unknown radio triggers a HELLO to it. The EU 868 1% duty
cycle is enforced ([lora/airtime.py](lora/airtime.py)): a message and its
ACK take about 0.3 s of the 36 s allowed per hour.

### Speech recognition

All timings use the whisper.cpp JFK speech sample (a 3.3 s phrase and the
full 11 s). Both engines transcribed it word for word.

| Engine | Board | 3.3 s phrase | 11 s speech | Model load | Peak RAM |
|---|---|---|---|---|---|
| `faster-whisper` `tiny.en` (int8) | Pi 4 | 2.7–4.7 s | 4.9 s | 2–7 s | **315 MB** |
| `vosk` small-en-us-0.15 | Pi 4 | 2.5 s | 5.9 s | 2.8 s | 199 MB |
| `vosk` small-en-us-0.15 | **Pi Zero 2 W** | **5.8 s** | 12.5 s | 9 s | 152 MB |
| `whisper-cpp` | – | not measured | | | |

**Use Vosk on a 512 MB Pi Zero 2 W.** The board has about 140 MB free,
and Whisper's 315 MB would swap constantly. `./setup.sh` picks Vosk
itself on boards with less than 900 MB of RAM, and faster-whisper
otherwise.

Whisper always encodes a padded 30-second window, so a short phrase costs
almost as much as a long one. Vosk writes lower case, so the first letter
is capitalised for the screen. Before recognition, audio is high-passed
at 100 Hz: on the Zero 2 W's Whisplay microphone, 70% of the energy in a
quiet room was mains hum and rumble below 100 Hz, plus a DC offset.
Annotations such as `[BLANK_AUDIO]` are removed. `asr.engine: auto` uses
the first engine that is installed.

---

## Hardware facts that shape the build

These are inherited from WalkieTalkie; see its README for the full story.

1. **The radio is UART, not SPI.** The Waveshare SX126X HAT puts an EBYTE
   E22 module between the Pi and the SX1262. The module's firmware drives
   the chip over SPI internally, and the Pi only talks to it over serial
   (`/dev/ttyS0`, 9600 baud). So there are no BUSY, DIO1 or RESET lines
   to manage from Linux, only the serial port and the M0/M1 mode pins.
2. **M0/M1 share GPIO 22/27 with the LCD's backlight and DC lines.**
   Therefore:
   - The app keeps the backlight at 100% and hands it back at 100% when it
     leaves. Dimming is 1 kHz PWM on M0 and would deafen the radio, for
     this app and for whatever uses the radio next. Another HAT app can
     leave it dimmed, so `--headless`, which never touches the screen,
     can find the radio deaf. The start-up log then says **THE RADIO IS
     DEAF: module is in wake-on-radio tx mode**. Open the Messenger or
     WalkieTalkie from the desktop once to put the backlight back to 100%.
   - The Whisplay driver must park DC (the radio's M1) low after each
     frame: [docs/whisplay-dc-fix.patch](docs/whisplay-dc-fix.patch),
     applied by `setup.sh`.
   - The screen is only redrawn when something changes. Each frame makes
     the radio deaf for about 11 ms.
   - Moving M0/M1 to free GPIOs and setting `radio.mode_pins` removes all
     three constraints.
3. **The module is provisioned once, and both apps share it.**
   `provision_radio.py` writes the frequency and air rate to non-volatile
   memory. On a board that also has WalkieTalkie, use **its**
   `provision_radio.py --range normal|long|longest`. It runs on both
   boards and records what it wrote in `../WalkieTalkie/config.yaml`.
   The Messenger's `radio.frequency_mhz` and `radio.air_speed` default to
   `auto`, which reads them from that file. So after `--range long`, the
   Messenger counts airtime and waits for ACKs at 2400 bps without being
   told. This project's `provision_radio.py` refuses to write different
   settings there, because WalkieTalkie would go on believing the old ones.
4. **One app per radio.** WalkieTalkie and the Messenger both use
   `/dev/ttyS0`. If both have it open, each steals the other's bytes, and
   messages arrive broken on both sides. This can happen because the HAT
   desktop starts an app without stopping the one before, and
   WalkieTalkie keeps its radio when it loses the screen, so it can keep
   listening. An app started over SSH or at boot also counts. Both apps
   therefore lock the port when they open it (`exclusive=True`, an
   `flock`). The second one is refused, and its screen says **Radio busy:
   quit WalkieTalkie** (WalkieTalkie says *radio busy: quit Messenger*).
   Quit the named app and open this one again. The name comes from the
   holder's folder. It is only visible when both run with the same
   credentials, as they do from the HAT desktop; otherwise the screen
   says "quit another app".
5. **Both radios must run the same app.** The two apps' packets ignore
   each other (Messenger frames start `AA 01`–`05`, WalkieTalkie's
   `AA 55`). A message sent to a radio that is in WalkieTalkie ends as
   **Not confirmed**.

---

## Install

From a development machine, copy the project over and run setup on the
device:

```bash
./deploy.sh jarvis@192.168.1.120 --setup      # picks the ASR engine by RAM
```

Or on the device itself: `./setup.sh` (`--check` only reports, `--yes`
accepts everything, `--asr` overrides the engine choice). It:

1. installs the apt packages (`python3-serial python3-yaml python3-pil
   python3-numpy python3-venv alsa-utils`, plus `espeak-ng`, which is
   optional and only used for reading aloud). Without sudo, a board with
   the system pip can do without `python3-venv`.
2. creates `.venv` and installs the ASR engine from wheels only, then
   fetches its model
3. checks the serial port for a kernel console and for other programs
   using it
4. checks the Whisplay runtime and the DC patch, and registers
   **Messenger** on the HAT desktop (`./install.sh`; `--autostart` to
   launch it at login)
5. runs the tests

A board that already runs WalkieTalkie has the UART, console and DC fixes
done. `setup.sh` reports on them rather than changing boot files. Its
installers (`../WalkieTalkie/setup.sh`) handle a fresh board.

Start it with `./run.sh` or from the HAT desktop.

### A Pi with a slow connection

A Zero 2 W with weak Wi-Fi can download at a few KB/s. At that rate,
fetching packages and models on the device takes hours. The Pi Zero 2 W
at 192.168.1.120 (signal −71 to −77 dBm, Wi-Fi power saving on) was
installed without the Pi downloading anything:

```bash
# on the development machine (same platform: aarch64, Python 3.13)
.venv/bin/pip wheel -w wheels vosk                  # vosk, srt, websockets, ...
curl -LO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
# unzip it, then: tar -cf - vosk-model-small-en-us-0.15 | zstd -19 > model.tar.zst   (68 → 36 MB)
rsync -a --partial wheels/vosk-* wheels/srt-* wheels/websockets-* model.tar.zst jarvis@PI:.cache/messager-wheels/

# on the Pi
cd ~/Messenger && mkdir -p models && zstd -dc ~/.cache/messager-wheels/model.tar.zst | tar -C models -xf -
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --no-index --find-links ~/.cache/messager-wheels vosk
./setup.sh --asr vosk        # now finds everything in place
```

Send only what the Pi lacks. Its system packages already covered numpy,
cffi, requests and tqdm. `sudo iw wlan0 set power_save off` (until the
next reboot), or moving the Pi closer to the router, speeds up the link.

## Bring-up, stage by stage

| Stage | Command |
|---|---|
| Talk to the module | `python3 provision_radio.py --check` (daemon stopped) |
| Send a HELLO between the boards | `python3 tools/linktest.py listen` on one, `python3 tools/linktest.py hello` on the other |
| Test the ASR engine | `./run.sh --transcribe clip.wav` |
| Check the screen layout | `./run.sh --preview screen.png` |
| Range, packet loss and latency | `python3 tools/linktest.py ping -n 50` (the app or `linktest listen` on the far end) |
| Everything, no hardware | `python3 tools/sim_air.py`, then two `./run.sh --headless`; see below |

`linktest ping` reports loss, round-trip time and the ACK's signal
strength. Move one radio between runs and watch the numbers change.

### Two radios on one computer

[tools/sim_air.py](tools/sim_air.py) creates pseudo-terminals that behave
like E22 modules on one shared channel. Each strips the 3-byte address
header and appends an RSSI byte. `--loss 0.3` drops 30% of packets:

```bash
python3 tools/sim_air.py --loss 0.3          # prints e.g. /dev/pts/5 /dev/pts/6
MESSENGER_RADIO_PORT=/dev/pts/5 MESSENGER_IDENTITY_NAME=RasPi \
  MESSENGER_DATA_DIR=/tmp/a ./run.sh --headless
MESSENGER_RADIO_PORT=/dev/pts/6 MESSENGER_IDENTITY_NAME=OrangePi \
  MESSENGER_DATA_DIR=/tmp/b ./run.sh --headless
```

Type in either one. The apps will log `LoRa port shared`. That is the
simulator holding the other end of the pty, and it is expected here.

### Typing over SSH

When stdin is a terminal (or `input.keyboard: on`), typed lines are sent
as messages. This is for testing; a keyboard on the board is described
under [Using it](#using-it).

```
any text   send it           /talk      record; Enter stops and sends
/hello     announce us        /history   print recent messages
/retry     resend last failed /quit      leave
```

## Configuration

Everything is in [config.yaml](config.yaml), with defaults in
[config.py](config.py). Environment variables override the file, named
`MESSENGER_<SECTION>_<KEY>`, for example
`MESSENGER_ASR_ENGINE=vosk ./run.sh`.

| Setting | Default | Meaning |
|---|---|---|
| `radio.frequency_mhz` / `radio.air_speed` | `auto` | what the module holds. "auto" reads WalkieTalkie's `config.yaml` next to this folder (the module is shared; see [Hardware facts](#hardware-facts-that-shape-the-build) 3), else 868 / 9600. Used for the duty-cycle budget, ACK waits and the channel byte |
| `radio.address` | `auto` | 0–65534. "auto" derives it from the hostname, so two boards with different hostnames need no setup |
| `radio.peer_address` | 65535 | who spoken messages go to. 65535 means everyone; the radio that hears it ACKs |
| `identity.name` | `auto` | shown on the other radio as "RX: name". "auto" uses the hostname |
| `messaging.ack_timeout_seconds` / `max_retries` | 3.0 / 3 | per attempt / resends after the first |
| `asr.engine` / `asr.model` | `auto` / `tiny.en` | see [Speech recognition](#speech-recognition) |
| `messaging.quick_replies` | OK, Yes, No, On my way, Where are you?, Call me, Wait 5 minutes, Thank you | the button's canned messages. Quote them: YAML reads a bare Yes/No as true/false |
| `ui.chat_messages` | 10 | how many of the latest messages the chat shows |
| `input.hold_ms` / `input.long_press_ms` | 350 / 700 | talking starts this far into a hold / a hold in the reply list sends after this (MFruit OS's long press) |
| `input.physical_keyboard` | true | read a keyboard plugged into the board |
| `tts.enabled` | false | read received messages aloud |
| `audio.mic_level` | 80 | 100 overdrives the Whisplay preamp, and distorted audio transcribes badly |

Data lives in `~/.lora-messenger/`: `history.json` (the last 200
messages and the names learned from other radios), `ids.json` (the next
message ID) and the single-instance lock.

## Project layout

```
main.py              the app: state, input actions, main loop, --transcribe/--preview
config.py            config.yaml + environment → dataclasses
asr/                 speech_to_text.py: engines, silence gate, clean-up
lora/                sx126x.py (E22 UART driver) · packet.py (format, CRC, deframer)
                     protocol.py (IDs, dedupe, splitting) · link.py (rx thread, tx + duty cycle)
                     airtime.py · modepins.py (is the radio deaf?)
audio/               recorder.py (pre-roll PTT) · player.py (cues, TTS) · dsp.py · devices.py
display/             board.py (Whisplay daemon client) · whisplay.py (the chat in MFruit OS's
                     chrome, frames, backlight)
controls/            keyboard.py (typing over SSH)
mfruit_sdk/          MFruit App SDK, vendored: the input controller (button + USB/Bluetooth
                     keyboard), status bar, lists, fonts. Do not edit here: change
                     ~/MFruitOS/mfruitos/sdk, then ~/MFruitOS/scripts/sdk-sync.sh ~/Messenger
messaging/           sender.py (ACK/retry) · receiver.py · history.py
tools/               sim_air.py · linktest.py · launch_via_daemon.py · preview.py (every
                     screen to PNG)
tests/               141 tests; fakes.py models the E22 module
```

---

## Update 2026-09-30: MFruit OS controls and look

#### Update summary
- The Messenger now handles and looks like every other MFruit OS app:
  MFruit OS's status bar, fonts, colours, lists and footer hints, and the
  shared input controller for the button and a USB or Bluetooth keyboard.

#### What changed
- Input goes through `mfruit_sdk.input.InputController` (replacing
  `controls/button.py` and `controls/keys.py`). The chat is a talk screen:
  hold or Space talks. The quick replies are an MFruit OS list: tap next,
  2 clicks previous, hold (then release) sends, 4 clicks back — before,
  2 clicks went back and the hold sent on the press.
- 4 clicks (or Esc) on the chat leave the app; while typing they cancel
  the typing first. Enter with nothing typed opens the quick replies.
- A menu-style hold is MFruit OS's 0.7 s long press (`input.long_press_ms`);
  talking still starts after 0.35 s (`input.hold_ms`).
- Keyboards are found at once when plugged in (inotify), with no polling
  while idle; the button worker makes no wakeups either.
- The screen: MFruit OS's status bar with the other radio's name (shrunk
  rather than cut when long), WiFi and battery; the footer shows the
  hints, or a coloured bar while listening, sending or on an error; the
  reply list is an MFruit OS list; only whole messages are drawn under the
  status bar. The typing cursor is drawn (MFruit OS's font has no "▏").
- Esc is claimed from the daemon at start-up (`own_escape_key`), as well as
  by `install.sh`.

#### Validation
- `python3 -m pytest -q`: 141 pass. New `tests/test_controls.py` drives the
  app's own controller with a fake clock (talk after 0.35 s, a deliberate
  hold in the list that sends on release, 4 clicks leave, a click on a
  dark screen only wakes it, nothing while another app has the screen).
  `tests/test_chat.py` covers the keyboard through the controller (Space
  talks, then types; Esc; arrows; Tab; keys pressed elsewhere ignored).
- `python3 tools/preview.py` renders every state; checked by eye.

#### Notes
- Not yet tried with a real keyboard on a board (none attached).

---

## Update 2026-09-29 (later): a chat, quick replies and a keyboard

#### Update summary
- The Orange Pi could receive but not answer: it had no speech
  recognition, and without it the button could not send anything. Now it
  has faster-whisper, installed without sudo. And any radio can answer
  without speaking, with quick replies on the button or a keyboard.
- The screen is a chat: the latest 10 messages, received on the left and
  sent on the right, each with its time and its delivery or signal. The
  bottom bar always says what the button does. See [Using it](#using-it).

#### What changed
- [display/whisplay.py](display/whisplay.py): the chat view (`Bubble`,
  `Picker`, `View`) and its renderer. The newest message sits at the
  bottom and older ones scroll off the top. Also a typing line, the quick
  reply list, and a "▼ n newer" badge while scrolled back. A long radio
  name steps down a font size before it is cut.
- [main.py](main.py): gestures depend on what is open (the chat or the
  quick replies). A hold without speech recognition opens the replies.
  The reply list offers **↻ Resend** after a failed message and closes
  after 20 s. 2 clicks on a scrolled-to failed message resends it.
- [controls/keys.py](controls/keys.py): reads keyboards from
  `/dev/input` alongside the Whisplay daemon, which never passes keys to
  other apps. It ignores the Orange Pi's power button, ADC buttons and IR
  receiver, and picks up a keyboard plugged in later. `install.sh`
  registers with `disable_esc_exit_key`, so Esc cancels typing instead of
  quitting.
- `setup.sh` installs speech recognition without sudo. With no
  `python3-venv`, it makes the venv `--without-pip` and fills it with the
  system pip, after first installing a current pip into it: Ubuntu
  22.04's pip 22.0.2 crashes resolving faster-whisper. It asks for
  `pytest>=7`, because faster-whisper brings `anyio`, whose pytest plugin
  breaks the older system pytest.
- Fixed a race in [messaging/sender.py](messaging/sender.py). An ACK that
  arrived just as the last retry ran out was taken for the in-flight
  message and dropped, so a message that had arrived stayed "not
  confirmed". It made `test_late_ack_still_marks_delivered` fail now and
  then.
- Config: `messaging.quick_replies`, `ui.chat_messages`,
  `input.physical_keyboard`.
- faster-whisper loads a cached model with `local_files_only`. On Wi-Fi
  with no internet, its check for a newer model took 135 s before it
  fell back to the cache. See
  [Without Wi-Fi or internet](#without-wi-fi-or-internet).

#### Validation
- `python3 -m pytest tests -q`: **133 passed** on the development
  machine and the Pi Zero. The Orange Pi was off Wi-Fi for the last
  fixes, so it last ran the 129 before them, all passing.
  - New: `test_chat.py` (14: the 10 shown, left and right, titles,
    delivery marks, hints, quick replies by hold and click, resend from
    the list, typing, Esc, arrows, no keys without the screen) and
    `test_keys.py` (6: which devices are keyboards, shift, caps lock,
    repeat, events split across reads, a keyboard read through a real
    FIFO and then unplugged).
  - Also new: display tests for the layout (left and right by pixel
    colour, the newest lowest, overflow off the top, the long-name
    header) and the sender race. The race test fails without the fix.
- Loading Whisper and transcribing the JFK sample through
  `main.py --transcribe` (same faster-whisper 1.2.1 and huggingface_hub
  1.33 as the Orange Pi):

  | Network | Before | After |
  |---|---|---|
  | Online | 0.9 s | 0.6 s |
  | No network (`unshare -rn`) | 0.7 s | 0.6 s |
  | Wi-Fi, no internet (unanswered proxy) | 135 s | 0.6 s |

  The transcript was word for word each time.
- Orange Pi: `./setup.sh` installed faster-whisper and tiny.en without
  sudo. `./run.sh --transcribe jfk.wav` gave the sentence word for word,
  loading in 2.9 s and transcribing 11 s of audio in 5.9 s.
- On the radios, both running the new version from the HAT desktop: a
  hold on the Orange Pi was heard as "Hello, 1, 2, 3." in 4.4 s, and
  delivered to the Pi Zero in 658 ms. Both screens were captured from
  the daemon's framebuffer and checked. Each showed the chat with the
  message on the correct side and a ✓ on the sender's.

#### Notes
- **The two boards' clocks show different timezones.** The Orange Pi
  shows UTC and the Pi Zero British time, so the times in the chat
  disagree. On each, run `sudo timedatectl set-timezone Australia/…`
  with your city.
- **The keyboard was not tried with a real keyboard.** Neither board had
  one plugged in. The reading and decoding were tested through a FIFO
  with real input-event bytes.
- The quick-reply gestures were tested in software. A hold on the
  Orange Pi, the path that used to fail, was tried on the real button.
- The Pi Zero's Vosk small model mishears a good deal ("Lou adler do").
  Whisper on its 415 MB would swap. A larger Vosk model is the likely
  next step, if its memory allows.

---

## Update 2026-09-29: sharing a board with WalkieTalkie

#### Update summary
- The Messenger now runs on the same radios as WalkieTalkie without the
  two getting in each other's way. The first messages over the air went
  both ways between the Orange Pi Zero 2W and the Pi Zero 2 W.

#### What changed
- **The radio port is locked** (`lora/sx126x.py`: `exclusive=True`,
  `PortBusy`, `port_users`). WalkieTalkie got the same change. Before,
  a second app on `/dev/ttyS0` only logged a warning and went on
  sharing the port. See [Hardware facts](#hardware-facts-that-shape-the-build) 4.
- **The frequency and air rate follow WalkieTalkie** (`auto`,
  `config.module_settings`). Both radios had been moved to 2400 bps with
  WalkieTalkie's `--range long` while the Messenger still said 9600. That
  undercounted airtime four times over against the 1% duty cycle.
  `setup.sh` step 6 now shows the values in use and offers to switch a
  mismatched `config.yaml` to `auto`.
- **ACK waits include the round trip's air time** (`Link.round_trip_seconds`).
  A fixed 3 s left 1.6 s of slack at 2400 bps and under 1 s at 1200, so
  a message whose ACK was already on its way could be sent again.
- **Leaving hands the backlight back at 100%**, not at `ui.brightness`
  (80). At 80, M0 was high a fifth of the time, which deafened a
  WalkieTalkie still running in the background.
- **`provision_radio.py` refuses** to write a frequency or air rate other
  than the one WalkieTalkie recorded.
- **`run.sh` runs `.venv/bin/python`** instead of sourcing
  `.venv/bin/activate`. The Pi Zero's venv had been made as
  `/home/mengpi/Messager/.venv`, so after the rename `activate` quietly
  fell back to the system Python. Vosk then "could not load", although
  `setup.sh --check` (which uses `.venv/bin/python`) said it was
  installed.
- `setup.sh --check` now fails on a missing Vosk model instead of
  skipping it silently.
- The deaf-radio log names the state that deafens the radio. It used to
  say "transparent mode (16% of the time)" for a radio that was
  transparent 84% of the time.
- `deploy.sh` defaults to `~/Messenger` (it was `~/Messager`), and the
  README's offline install uses the new folder name too.

#### Validation
- `python3 -m pytest tests -q`: **105 passed** on the development
  machine (a Jetson), the Orange Pi Zero 2W and the Pi Zero 2 W. The new
  tests are in `tests/test_sharing.py` and cover:
  - a second opener refused until the first closes;
  - the holder named by its folder, from a real child process holding a
    pty;
  - the app's "Radio busy: quit WalkieTalkie";
  - `auto` from WalkieTalkie's file, with its defaults, and without it;
  - numbers and the environment still winning over `auto`;
  - the provisioning refusal.
- Also new: the ACK-wait tests in `test_messaging.py`, the backlight
  hand-back and PWM-deafness tests in `test_display.py`, and a `run.sh`
  test with a stale `activate`. Each new test failed with its fix
  undone.
- On the radios (both at 868 MHz and 2400 bps, set by WalkieTalkie):
  - The Orange Pi typed (`--headless`) to the Pi Zero, which was running
    from the daemon: **delivered on the first try, ACK in 712 ms**. Then
    the reverse: **708 ms**. Both learned the other's name from the
    HELLOs.
  - While the Messenger held the Pi Zero's port, WalkieTalkie's driver
    was refused with `/dev/ttyS0 is in use by Messenger`.
  - After the Messenger quit, M0/M1 read transparent 4000 times out of
    4000 samples. Before, with the backlight left at about 76%, only 3032
    were transparent, and a headless test heard nothing.
  - `./setup.sh --check`: **Ready** on the Pi Zero. On the Orange Pi, one
    fail: `python3-venv` is missing.

#### Notes
- **Orange Pi: speech recognition still needs installing.** It needs
  sudo: `sudo apt install python3-venv` (and `espeak-ng` for read-aloud),
  then `cd ~/Messenger && ./setup.sh`, which picks faster-whisper for its
  981 MB. Until then, the Messenger there receives and ACKs, but cannot
  turn speech into text.
- The Pi Zero's Vosk model was missing. It was copied over from the
  development machine into `~/Messenger/models`, and `config.yaml`
  points at it.
- The Whisplay daemon refuses to launch an app while another has the
  screen. So a clash needs one app running without it: WalkieTalkie
  after losing focus, or anything started over SSH or at boot.

---

## Update summary

- New project: voice → offline ASR → LoRa text messaging with ACK/retry,
  CRC-checked packets, message history and a Whisplay screen, built on
  WalkieTalkie's proven hardware layers.

## What changed

- New packet format with SRC/DST and CRC-16, and a stream deframer that
  handles the module's trailing RSSI byte, including when it equals START.
- ACK/retry with jitter, late-ACK recovery, duplicate suppression, IDs
  saved across restarts, and long transcripts split into parts.
- Pluggable ASR (faster-whisper, vosk, whisper.cpp) with a silence gate.
- `tools/sim_air.py` and `tools/linktest.py` for work without hardware
  and for range tests.
- Fixed during validation:
  - The Whisper model loaded twice under `--transcribe`.
  - The status briefly showed "Ready" between transcription and sending,
    costing an extra frame.
  - Text wrapping of very long words was 9× too slow.
  - Names were never learned when the start-up HELLOs were lost.
  - A failed send read "Not delivered" even when only the ACKs were lost.
    It now reads "Not confirmed".
- Fixed while deploying to the Pi Zero 2 W:
  - `setup.sh` was not executable, so `./deploy.sh --setup` could not
    have run it.
  - `setup.sh` now picks the ASR engine by RAM. It also treats
    `espeak-ng` as optional, so a board without sudo access can still
    report Ready.
  - Speech is high-passed at 100 Hz before recognition (hum and DC
    offset).
  - Vosk's lower-case transcripts are capitalised.
- Added `deploy.sh`.

## Validation

- `.venv/bin/python -m pytest tests -q`: **89 passed**, both here and on
  the Pi Zero 2 W (in 15 s). This includes the
  brief's final demonstration run through the real driver and app
  against fake E22 modules: "Meet me at five o'clock" goes from Pi to
  Orange Pi, and the Pi shows "Message delivered ✓".
- `./run.sh --transcribe jfk.wav` (the whisper.cpp speech sample) on a
  Pi 4: *"And so my fellow Americans ask not what your country can do for
  you ask what you can do for your country."* Load 2.0 s, transcription
  5.0 s for 11 s of audio.
- Two app processes over `tools/sim_air.py`:
  - No loss: delivered with ACK round trips of 43 and 56 ms, and the
    receiver showed "RX: RasPi" with RSSI.
  - 50% loss (15 packets dropped): 4 of 6 messages confirmed after up to
    4 tries. Repeats were ACKed but shown once. One message reached the
    far side although all of its ACKs were lost, which led to the
    "Not confirmed" wording.
- Installed on the **Pi Zero 2 W at 192.168.1.120** (Debian 13, Python
  3.13):
  - `./setup.sh --check` reports Ready.
  - The radio port opens and M0/M1 read transparent, with no other
    program on the port. Nothing was transmitted.
  - The Whisplay microphone records with the pre-roll, peaking at
    −15 dBFS with no clipping.
  - Vosk transcribes the sample word for word (timings in
    [Speech recognition](#speech-recognition)).
  - **Messenger** is registered on the HAT desktop.

## Notes

- ~~Not yet run end to end over the air.~~ Done on 2026-09-29; see the
  update above.
- **The silence gate does nothing on this microphone.** The Zero 2 W's
  filtered room noise measured 620–900 RMS against a threshold of 120,
  so only a dead microphone is caught. Vosk still returns nothing for
  noise, but takes about 11 s to decide, so an accidental press shows
  "Converting speech…" for that long. A gate based on dynamic range would
  fix this. It needs a few recordings of real speech through this
  microphone to calibrate without discarding quiet speech.
- **Speed on the Zero 2 W**: about 6 s from releasing the button to text
  for a short phrase. Feeding Vosk while recording, which it supports,
  would leave only the last moment of speech to process on release.
- Messages are not encrypted. Anyone on the frequency with this app can
  read them. WalkieTalkie's paired encryption could be carried over if
  that matters.
# Messenger
# Messenger

## MFruit OS 1.4.0 keyboard compatibility

Vendored SDK 1.2.0 reads keys from MFruit OS's foreground key hub while the
launcher holds keyboards exclusively. Standalone use falls back to evdev.
Deploy this SDK with MFruit OS 1.4.0 so keyboard input continues to work.
