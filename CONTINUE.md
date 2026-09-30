# CONTINUE — what is left

Temporary handoff note, updated 2026-09-29. Delete it once the steps
below are done. `deploy.sh` copies it along with the code.

## Where things stand

- The development machine is the Jetson (`~/Messenger`); 131 tests pass
  there and on both radios.
- Both radios run the chat version, registered on their HAT desktops:
  - the Orange Pi Zero 2W, `orangepi@192.168.0.130`, with faster-whisper;
  - the Pi Zero 2 W, `jarvis@192.168.0.33`, with Vosk.
- Spoken messages have gone both ways, at 868 MHz and 2400 bps.
  WalkieTalkie's `--range long` set that rate, and the Messenger follows
  it.

## Steps

### 1. Set both boards' timezone (needs sudo)

The chat shows each message's time, and the boards disagree: the Orange
Pi is on UTC and the Pi Zero on British time. On each:

```bash
sudo timedatectl set-timezone Australia/Sydney    # or your city
```

### 2. Try the button and a keyboard

The controls are now MFruit OS's (2026-09-30):

- 2 clicks on the chat → quick replies. Tap moves, a hold (then release)
  sends, 2 clicks go back up the list, and 4 clicks close it.
- 4 clicks on the chat leave the app.
- Plug a USB keyboard into either board, type, and press Enter. Hold
  Space to talk. It has only been tested without a real keyboard.

## Done when

- [ ] Both boards show the same time in the chat
- [ ] A quick reply and a typed message arrive on the other radio
- [ ] This file is deleted
