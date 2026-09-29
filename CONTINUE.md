# CONTINUE — setting up Messenger on the next machine

Temporary handoff note, written 2026-09-29. Delete it once the next
machine is working. `deploy.sh` copies it along with the code, so it
will be at `~/Messenger/CONTINUE.md` on the device.

## Where things stand

- The code lives at `~/Messenger` on the Raspberry Pi 4 (the development
  machine). `./setup.sh` there: the tests pass (89 passed).
- On the Pi 4, setup steps 4 and 5 fail. This only matters if the HATs
  are fitted to it:
  - **UART off.** There is no `enable_uart=1` in `/boot/firmware/config.txt`,
    so `/dev/ttyS0` does not exist.
  - **Serial console on the LoRa port.** `/boot/firmware/cmdline.txt`
    still has `console=serial0,115200`.
  - **No Whisplay driver.** `~/Whisplay` is not installed.
- The folder was renamed from **Messager** to **Messenger**, but not
  everywhere:
  - `deploy.sh` still defaults the remote folder to `Messager` (line 28),
    so always pass `Messenger` to it.
  - The README's offline-install section still says `~/Messager` and
    `~/.cache/messager-wheels`. The wheels folder can keep its old name.
  - Devices deployed earlier may still have `~/Messager`.

## Steps

### 1. Copy the code over (run on the Pi 4)

```bash
ssh-copy-id USER@HOST                      # once, so rsync stops asking for a password
cd ~/Messenger && ./deploy.sh USER@HOST Messenger
```

Add `--setup` to run setup straight after the copy. For a 512 MB Pi Zero 2 W,
also add `--asr vosk`.

### 2. If the device still has the old `~/Messager` folder

```bash
ls -d ~/Messenger 2>/dev/null && echo "both exist: merge by hand, don't mv"
mv ~/Messager ~/Messenger
cd ~/Messenger
sed -i 's|/Messager/|/Messenger/|g' config.yaml   # the Vosk model path, if any
rm -rf .venv                                     # the venv has the old path built in; setup.sh rebuilds it
```

If you did step 1 before step 2, both folders exist. Keep the new
`~/Messenger`, copy over any `config.yaml` or `models/` you still need
from `~/Messager`, then delete `~/Messager`.

### 3. See what's missing (on the device)

```bash
cd ~/Messenger && ./setup.sh --check     # reports only, changes nothing
```

### 4. Fix the board

**If the UART is off or the serial console is on it** (step 4 fails):

- Raspberry Pi:
  ```bash
  echo enable_uart=1 | sudo tee -a /boot/firmware/config.txt
  sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
  sudo sed -i -E 's/console=serial0,[0-9]+ ?//' /boot/firmware/cmdline.txt
  sudo systemctl mask serial-getty@ttyS0
  ```
- Orange Pi: set `console=display` (or `none`) in `/boot/orangepiEnv.txt`,
  then run `sudo systemctl mask serial-getty@<port>`.
- If `~/WalkieTalkie` is on the device, `cd ~/WalkieTalkie && ./setup.sh`
  does all of this for either board, and asks before each change.

**If the Whisplay driver is missing** (step 5 fails):

```bash
git clone --depth 1 https://github.com/PiSugar/Whisplay.git ~/Whisplay
ls ~/Whisplay/script
```

- Orange Pi Zero 2W: `cd ~/Whisplay && sudo bash script/install_orangepi_zero2w.sh`
- Raspberry Pi: run the Raspberry Pi install script in that folder. Its
  name has not been checked yet, so look before you run it.

### 5. Reboot, then run setup for real

```bash
sudo reboot
# after it comes back:
cd ~/Messenger && ./setup.sh             # or: ./setup.sh --asr vosk on a Pi Zero 2 W
```

When step 5 offers to apply `docs/whisplay-dc-fix.patch`, say **yes**.
Without the patch, the radio can neither send nor receive.

### 6. The radio module

Both radios must use the same `frequency_mhz` and `air_speed` in
`config.yaml`. The current values are 868 MHz and 9600. A module already
provisioned for WalkieTalkie works as it is. Otherwise, set it up once:

```bash
sudo systemctl stop whisplay-daemon
python3 provision_radio.py --frequency 868
sudo systemctl start whisplay-daemon
```

### 7. Run it

```bash
./run.sh          # or pick "Messenger" on the HAT's desktop
```

Only one program can use the radio at a time. If WalkieTalkie runs on
the same board, stop it first.

## Done when

- [ ] `./setup.sh --check` shows no `fail` lines
- [ ] A message sent from this radio shows up on the other one, and the reverse works too
- [ ] This file is deleted

## Follow-ups (optional)

- Change the default in `deploy.sh` line 28 from `Messager` to `Messenger`,
  and update the README's `~/Messager` paths to match.
