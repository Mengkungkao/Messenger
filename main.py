#!/usr/bin/env python3
"""LoRa Messenger: a chat between radios, spoken, picked or typed.

The controls are MFruit OS's (mfruit_sdk.input: the button and any USB or
Bluetooth keyboard). The chat is a talk screen:

    hold / Space held talk; on release, speech -> text -> LoRa
                      (no speech recognition here: a hold opens the replies)
    tap               scroll to older messages; past the oldest, back to now
    2 clicks          quick replies -- or resend, on a failed message
    3 clicks          read the newest (or scrolled-to) message aloud
    4 clicks / Esc    leave the app
    letters, Enter    type a message and send it; Esc cancels it
    Up / Down         older / newer; Tab or Enter opens the quick replies

The quick replies are a list, as everywhere in MFruit OS:

    tap / Down        next reply            2 clicks / Up   previous
    hold / Enter      send it               4 clicks / Esc  back to the chat

    python3 main.py                      run (./run.sh does this)
    python3 main.py --headless           no screen: keyboard and log only
    python3 main.py --transcribe a.wav   test the ASR engine on a file
    python3 main.py --preview out.png    render a sample screen

The main loop does not poll. It draws, works out when something next
needs to change, and sleeps on an Event until then or until a button, a
key, a packet or a finished transcription wakes it. Transcription runs on
its own thread, so the radio keeps receiving -- and ACKing -- while
Whisper thinks.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

import config as config_module
from asr.speech_to_text import NoEngine, create_engine, read_wav
from audio import devices
from audio.player import Player
from audio.recorder import Recorder
from mfruit_sdk.input import (BACK, BUTTON, CHAR, ERASE, EXTRA, KEYBOARD, NEXT,
                              PREVIOUS, SELECT, TALK_END, TALK_START, InputController)
from mfruit_sdk.status import StatusMonitor

from controls.keyboard import HELP, Keyboard
from display import board as board_module
from display.whisplay import Bubble, Picker, Screen, View, render
from lora import modepins, protocol
from lora.link import Link
from lora.sx126x import PortBusy, SX126x, port_conflicts
from messaging import history as h
from messaging.receiver import Receiver
from messaging.sender import Sender
from utils.logger import get_logger
from utils.single_instance import AlreadyRunning, SingleInstance

log = get_logger("main")

# A press this short after the hold threshold is a slip, not speech.
MIN_TALK_SECONDS = 0.4
LISTEN_FRAME_SECONDS = 0.1
# The reply list closes itself if left alone this long.
PICKER_SECONDS = 20.0
# Longer than anyone types into one message; split_text handles the rest.
MAX_COMPOSE = 600

# Footer hints: (gesture, what it does), most important first -- the
# footer drops what does not fit from the end.
HINTS_TALK = [("hold", "talk"), ("2×", "replies"), ("4×", "exit")]
HINTS_NO_ASR = [("hold", "replies"), ("tap", "older"), ("4×", "exit")]
HINTS_PICKER = [("tap", "next"), ("hold", "send"), ("4×", "back")]
HINTS_TYPING = [("Enter", "send"), ("Esc", "cancel")]


class Messenger:
    def __init__(self, config, board=None, radio=None, headless: bool = False):
        self.config = config
        self.address = config.radio.address
        self.name = config.identity.name
        self.running = True
        self._wake = threading.Event()
        self._lock = threading.RLock()

        # --- state the screen is drawn from -----------------------------
        self.listening = False
        self.transcribing = False
        self.scroll = 0                 # 0 = newest at the bottom; n = n messages back
        self.picker = None              # Picker while the quick replies are open
        self._picker_rows = []          # (label, text, failed message) per row
        self._picker_until = 0.0
        self.compose = None             # text being typed on a keyboard
        self._flash = None              # (text, tone, until)
        self._statuses = {}             # id(message) -> last status seen
        self._radio_note = ""

        data = config.data_dir
        self.history = h.History(data / "history.json", config.messaging.history_size)
        self.ids = protocol.MessageIds(data / "ids.json")

        # --- screen and button -----------------------------------------
        if board is None:
            if headless:
                board, mode = board_module.NullBoard(), "headless"
            else:
                board, mode = board_module.acquire_board(
                    on_foreground_acquired=self._on_foreground)
        else:
            mode = "given"
        self.board, self.board_mode = board, mode
        self.screen = Screen(board, config.ui)
        # The button and a USB / Bluetooth keyboard, as MFruit OS actions.
        # The chat is a talk screen (hold or Space talks) while the reply
        # list is closed and there is speech recognition to talk to.
        self.armed = False
        self.input = InputController(
            self._on_action,
            talk=lambda: self.picker is None and self.can_talk,
            typing=lambda: self.compose is not None,
            active=self._has_screen,
            on_armed=self._on_armed,
            debounce_ms=config.input.debounce_ms,
            click_window_ms=config.input.click_window_ms,
            long_press_ms=config.input.long_press_ms,
            talk_press_ms=config.input.hold_ms,
            keyboard=config.input.physical_keyboard,
            app_id=board_module.APP_ID)    # MFruit OS hands its keys to this app by id
        self.input.attach(board)
        for hook, handler in (("on_exit_request", lambda *_: self.stop("daemon")),
                              ("on_focus_revoked", self._on_focus_revoked)):
            if hasattr(board, hook):
                getattr(board, hook)(handler)
        # WiFi and battery for the MFruit OS status bar.
        self.status = StatusMonitor(interval=15.0, on_change=lambda _s: self._wake.set())

        # --- radio -----------------------------------------------------
        self.radio = radio if radio is not None else self._open_radio()
        self.link = self.sender = self.receiver = None
        if self.radio is not None:
            self.link = Link(self.radio, config.radio.air_speed,
                             config.radio.duty_cycle_percent)
            self.sender = Sender(self.link, self.history, self.address, self.ids,
                                 config.messaging.ack_timeout_seconds,
                                 config.messaging.max_retries, name=self.name)
            self.receiver = Receiver(self.link, self.sender, self.history, self.address,
                                     on_message=self._on_received)
            self.link.on_packet = self.receiver.handle
            self.link.on_rssi = self._on_rssi
        self.history.subscribe(self._on_history_change)

        # --- audio and ASR ---------------------------------------------
        audio = config.audio
        capture = devices.resolve(audio.capture_device, "capture", audio.preferred_card)
        playback = devices.resolve(audio.playback_device, "playback", audio.preferred_card)
        devices.set_mic_level(capture, audio.mic_level)
        self.recorder = Recorder(capture, max_seconds=audio.max_record_seconds)
        self.player = Player(playback, config.tts.voice, config.tts.words_per_minute)
        self.asr = create_engine(config.asr)

        # --- typing ------------------------------------------------------
        self.keyboard = None
        if Keyboard.wanted(config.input.keyboard):
            self.keyboard = Keyboard(self._on_typed, self._on_command)

    # ================================================================ radio
    def _open_radio(self):
        rc = self.config.radio
        mode_pins = tuple(rc.mode_pins) if rc.mode_pins else None
        try:
            radio = SX126x(port=rc.port, addr=self.address, freq_mhz=rc.frequency_mhz,
                           uart_baud=rc.uart_baud, mode_pins=mode_pins)
        except PortBusy as exc:
            # The HAT desktop starts an app without stopping the one before,
            # and WalkieTalkie keeps its radio when it loses the screen.
            log.error("%s. One app per radio: quit it, then start the messenger "
                      "again.", exc)
            holder = exc.holders[0] if exc.holders else "another app"
            self._radio_note = f"Radio busy: quit {holder}"
            return None
        except Exception as exc:
            log.error("radio unavailable on %s: %s", rc.port, exc)
            self._radio_note = "Radio offline"
            return None

        # With the stock jumpers M0/M1 are the LCD's backlight and DC lines.
        pins = tuple(rc.wired_mode_pins or (22, 27))[:2]
        if modepins.conflicts_with_backlight(pins):
            self.screen.lock_brightness(
                f"GPIO{modepins.WHISPLAY_BACKLIGHT_BCM} is both the LCD backlight "
                "and the radio's M0; dimming it would deafen the radio")
        health = modepins.check_and_warn(*pins)
        if health["readable"] and not health["transparent"]:
            self._radio_note = "Radio deaf: check M0/M1"
        shared = port_conflicts(rc.port)
        if shared:
            log.error("the LoRa port %s is shared: %s -- messages will arrive broken. "
                      "See README, 'Serial port'.", rc.port, "; ".join(shared))
            self._radio_note = "LoRa port shared"
        return radio

    def _on_rssi(self, dbm: int):
        self.receiver.handle_rssi(dbm)
        self._wake.set()

    def _on_received(self, message: h.Message):
        with self._lock:
            self.scroll = 0             # show it
        self.screen.poke()
        self.player.cue("received")
        if self.config.tts.enabled:
            self.player.speak(message.text)
        self._wake.set()

    def _on_history_change(self, message: h.Message):
        if message.direction == h.TX:
            before = self._statuses.get(id(message))
            self._statuses[id(message)] = message.status
            if before != message.status:
                if message.status == h.DELIVERED:
                    self.player.cue("delivered")
                elif message.status == h.FAILED:
                    self.player.cue("failed")
        self._wake.set()

    # ============================================================ talking
    def _asr_problem(self) -> str | None:
        if not self.recorder.available:
            return "No microphone"
        if isinstance(self.asr, NoEngine):
            return "No speech recognition"
        if self.asr.error:
            return "Speech recognition failed"
        return None

    @property
    def can_talk(self) -> bool:
        return self._asr_problem() is None

    def _on_talk_start(self):
        with self._lock:
            if self.picker is not None:
                # The reply list is not a talk screen: there a hold picks the
                # reply, on release (the input controller never talks there).
                return
            if self.listening or self.transcribing:
                return
            problem = self._asr_problem()
            if problem:
                # No voice on this radio: the hold picks a reply instead, so
                # the button can always answer.
                log.info("%s: the hold opens the quick replies", problem)
                self.open_picker()
                self.flash("No voice here: pick one", "busy", 2.5)
                return
            if not self.recorder.start():
                self.flash("Microphone busy", "error")
                return
            self.listening = True
            self.scroll = 0
        self.screen.poke()
        log.info("listening")
        self._wake.set()

    def _on_talk_end(self, held_seconds: float = 0.0):
        with self._lock:
            if not self.listening:
                return
            self.listening = False
            pcm = self.recorder.stop()
            if held_seconds and held_seconds < MIN_TALK_SECONDS:
                self.flash("Too short: hold while speaking", "error")
                return
            self.transcribing = True
        threading.Thread(target=self._transcribe_and_send, args=(pcm,),
                         name="transcribe", daemon=True).start()
        self._wake.set()

    def _transcribe_and_send(self, pcm: bytes):
        # The outcome -- queued message, or an error on screen -- is settled
        # before `transcribing` clears, so the status never blinks "Ready"
        # in between: each needless frame is ~11 ms the radio cannot hear.
        started = time.monotonic()
        try:
            text = self.asr.transcribe(pcm)
            log.info("heard %r in %.1f s (%.1f s of audio)", text,
                     time.monotonic() - started, len(pcm) / 32000)
            if text:
                self.send(text)
            else:
                self.flash("No speech heard", "error")
                self.player.cue("failed")
        except Exception as exc:
            log.exception("transcription failed")
            self.flash(f"ASR error: {exc}"[:40], "error")
        finally:
            with self._lock:
                self.transcribing = False
            self._wake.set()

    def send(self, text: str):
        text = " ".join(text.split())
        if not text:
            return
        if self.sender is None:
            # Still show what was said: the ASR half is worth testing alone.
            self.history.add(h.Message(h.TX, 0, self.config.radio.peer_address, text,
                                       h.FAILED))
            self.flash(self._radio_note or "Radio offline", "error")
            return
        with self._lock:
            self.scroll = 0
        self.sender.send_text(text, self.config.radio.peer_address)
        self.player.cue("sent")

    # ============================================================ the chat
    def chat(self) -> list:
        """The messages on screen: the latest few, sent and received."""
        with self._lock:
            return self.history.messages[-self.config.ui.chat_messages:]

    def selected(self) -> h.Message | None:
        """The message scrolled to, if scrolled back at all."""
        with self._lock:
            chat = self.chat()
            if self.scroll and self.scroll < len(chat):
                return chat[-1 - self.scroll]
            return None

    def _scroll(self, step: int, wrap: bool = True):
        """+1 = one message older. The button wraps round to the newest
        (it has no other way back); the arrow keys stop at the ends."""
        with self._lock:
            count = len(self.chat())
            if count < 2:
                self.scroll = 0
            elif wrap:
                self.scroll = (self.scroll + step) % count
            else:
                self.scroll = max(0, min(count - 1, self.scroll + step))

    # ======================================================= quick replies
    def _picker_items(self) -> list:
        """(label, text to send, failed message to resend) for each row."""
        items = []
        last_tx = next((m for m in reversed(self.chat()) if m.direction == h.TX), None)
        if last_tx is not None and last_tx.status == h.FAILED and last_tx.msg_id:
            items.append((f"↻ Resend: {last_tx.text}", None, last_tx))
        items += [(reply, reply, None) for reply in self.config.messaging.quick_replies]
        return items

    def open_picker(self):
        with self._lock:
            self._picker_rows = self._picker_items()
            self.picker = Picker([label for label, _, _ in self._picker_rows])
            self._picker_until = time.monotonic() + PICKER_SECONDS
            self.compose = None
        self.screen.poke()
        self._wake.set()

    def close_picker(self):
        with self._lock:
            self.picker = None
        self._wake.set()

    def _move_picker(self, step: int):
        with self._lock:
            if self.picker is None:
                return
            self.picker.index = (self.picker.index + step) % len(self.picker.items)
            self._picker_until = time.monotonic() + PICKER_SECONDS
        self._wake.set()

    def _send_picked(self):
        with self._lock:
            if self.picker is None:
                return
            _, text, failed = self._picker_rows[self.picker.index]
            self.picker = None
        if failed is not None:
            if self.sender and self.sender.resend(failed):
                self.flash("Resending", "busy")
        else:
            self.send(text)
        self._wake.set()

    # =============================================================== input
    def _on_armed(self, armed: bool):
        """A hold passed the threshold in the reply list: say what release does."""
        self.armed = armed
        self._wake.set()

    def _on_focus_revoked(self, *_args):
        # The screen is someone else's now, and so is the keyboard.
        log.info("screen taken")
        self.input.reset()

    def _on_action(self, action):
        """One MFruit OS input action, from the button or a keyboard."""
        if action.name == TALK_START:
            self._on_talk_start()
            return
        if action.name == TALK_END:
            self._on_talk_end(action.held)
            return
        dark = not self.screen.awake
        self.screen.poke()
        if dark and action.source == BUTTON and action.name != BACK:
            self._wake.set()
            return      # a click on a dark screen only wakes it
        if action.source == KEYBOARD:
            self._on_key(action)
        else:
            self._on_button(action.name)
        self._wake.set()

    def _on_button(self, name: str):
        with self._lock:
            picking, composing = self.picker is not None, self.compose is not None
        if picking:
            if name in (NEXT, PREVIOUS):
                self._move_picker(+1 if name == NEXT else -1)
            elif name == SELECT:
                self._send_picked()
            elif name == BACK:
                self.close_picker()
        elif name == BACK:
            if composing:
                with self._lock:
                    self.compose = None     # four clicks go back: first out of typing
            else:
                self.stop("four clicks")
        elif name == NEXT:
            self._scroll(+1)
        elif name == PREVIOUS:
            selected = self.selected()
            if (selected and selected.direction == h.TX and selected.status == h.FAILED
                    and self.sender and self.sender.resend(selected)):
                self.flash("Resending", "busy")
                with self._lock:
                    self.scroll = 0
            else:
                self.open_picker()
        elif name == EXTRA:
            self._read_aloud()
        elif name == SELECT:
            # A hold where there is no voice to talk with: pick a reply.
            self.open_picker()
            if not self.can_talk:
                self.flash("No voice here: pick one", "busy", 2.5)

    def _read_aloud(self):
        chosen = self.selected() or next(
            (m for m in reversed(self.chat()) if m.direction == h.RX), None)
        if not self.player.can_speak:
            self.flash("No text-to-speech (espeak-ng)", "error")
        elif chosen:
            self.player.speak(chosen.text)

    # ================================================ a keyboard on the board
    def _has_screen(self) -> bool:
        # In the background the keys belong to whatever has the screen.
        return bool(getattr(self.board, "foreground_ready", True))

    def _on_char(self, char: str):
        with self._lock:
            self.picker = None
            text = (self.compose or "") + char
            if text.strip() or self.compose is not None:
                self.compose = text[:MAX_COMPOSE]

    def _on_key(self, action):
        with self._lock:
            composing, picking = self.compose is not None, self.picker is not None
        name = action.name
        if name == CHAR:
            self._on_char(action.char)
        elif name == ERASE:
            if composing:
                with self._lock:
                    self.compose = self.compose[:-1] or None
        elif name == SELECT:
            if composing:
                with self._lock:
                    text, self.compose = self.compose, None
                self.send(text)
            elif picking:
                self._send_picked()
            else:
                self.open_picker()
        elif name == BACK:
            if composing:
                with self._lock:
                    self.compose = None
            elif picking:
                self.close_picker()
            else:
                self.stop("Esc")
        elif name in (NEXT, PREVIOUS):
            if picking:
                self._move_picker(+1 if name == NEXT else -1)
            elif action.key == "tab":
                if not composing:
                    self.open_picker()
            else:
                # The chat reads top to bottom, oldest first: Up is older.
                older = action.key in ("up", "left")
                self._scroll(+1 if older else -1, wrap=False)

    # ====================================================== stdin (SSH)
    def _on_typed(self, text: str):
        self.screen.poke()
        self.send(text)

    def _on_command(self, command: str):
        if command == "talk":
            self._on_talk_start()
            if self.listening:
                self.keyboard.recording = True
                print("recording -- press Enter to stop and send", flush=True)
        elif command == "talk-stop":
            self.keyboard.recording = False
            self._on_talk_end()
        elif command == "hello":
            if self.sender:
                self.sender.send_hello()
        elif command == "history":
            for message in self.history.messages[-20:]:
                print(describe(message, self.history), flush=True)
        elif command == "retry":
            failed = next((m for m in reversed(self.history.messages)
                           if m.direction == h.TX and m.status == h.FAILED), None)
            if failed and self.sender and self.sender.resend(failed):
                print(f"resending #{failed.msg_id}", flush=True)
        elif command in ("quit", "exit", "eof"):
            if command != "eof" or self.board_mode == "headless":
                self.stop(command)
        else:
            print(HELP, flush=True)
        self._wake.set()

    # ============================================================== screen
    def flash(self, text: str, tone: str = "idle", seconds: float = 3.0):
        self._flash = (text, tone, time.monotonic() + seconds)
        log.info("status: %s", text)
        self._wake.set()

    def _title(self, chat: list) -> str:
        """Who we are talking to: the one radio heard, or how many."""
        peers = []
        for message in chat:
            for address in (message.peer, message.acked_by):
                if address not in (None, protocol.BROADCAST, 0) and address not in peers:
                    peers.append(address)
        if not peers and self.config.radio.peer_address != protocol.BROADCAST:
            peers = [self.config.radio.peer_address]
        if not peers and len(self.history.names) == 1:
            peers = list(self.history.names)
        if len(peers) == 1:
            return self.history.name_for(peers[0])
        if peers:
            return f"{len(peers)} radios"
        return "Messenger"

    def _status(self) -> tuple:
        """A state worth showing instead of the hints, or ("", "idle")."""
        flash = self._flash
        if self.listening:
            return f"Listening… {self.recorder.elapsed:.1f}s", "listen"
        if self.transcribing:
            return "Converting speech…", "busy"
        if flash and flash[2] > time.monotonic():
            return flash[0], flash[1]
        if self.compose is not None or self.picker is not None:
            return "", "idle"
        if self._radio_note:
            return self._radio_note, "error"
        if self.sender is not None and self.sender.busy:
            return "Sending…", "busy"
        if not self.asr.ready and not isinstance(self.asr, NoEngine) and not self.asr.error:
            return "Loading speech model…", "busy"
        return "", "idle"

    def _hints(self) -> list:
        """What the button (and Enter / Esc) do right now, for the footer."""
        if self.compose is not None:
            return HINTS_TYPING
        if self.picker is not None:
            return [("release", "to send")] if self.armed else HINTS_PICKER
        selected = self.selected()
        if selected is not None:
            if selected.direction == h.TX and selected.status == h.FAILED:
                return [("2×", "resend"), ("tap", "older"), ("4×", "exit")]
            return [("3×", "read aloud"), ("tap", "older"), ("4×", "exit")]
        return HINTS_TALK if self.can_talk else HINTS_NO_ASR

    def view(self) -> View:
        view = View()
        with self._lock:
            chat = self.chat()
            anchor = len(chat) - 1 - self.scroll if self.scroll else None
            senders = {m.peer for m in chat if m.direction == h.RX}
            view.bubbles = [bubble(m, self.history, len(senders) > 1,
                                   selected=anchor is not None and index == anchor)
                            for index, m in enumerate(chat)]
            view.anchor = anchor
            if anchor is not None:
                view.position = f"{anchor + 1}/{len(chat)}"
            view.picker = self.picker
            view.compose = self.compose
        view.title = self._title(chat)
        view.empty_hint = (["No messages yet", "Hold to talk",
                            "2 clicks: quick replies"] if self.can_talk else
                           ["No messages yet", "Hold: quick replies"])
        if self.input.keyboard_connected:
            view.empty_hint.append("or type, and Enter sends")
        view.status, view.status_tone = self._status()
        view.hints = self._hints()
        view.device = self.status.sample()
        if self.listening:
            view.level = self.recorder.level
        return view

    def _on_foreground(self):
        self.screen.invalidate()
        self._wake.set()

    def _next_timeout(self) -> float | None:
        waits = [self.screen.next_idle_change()]
        if self.listening:
            waits.append(LISTEN_FRAME_SECONDS)
        if self._flash:
            waits.append(max(0.05, self._flash[2] - time.monotonic() + 0.01))
        if self.picker is not None:
            waits.append(max(0.05, self._picker_until - time.monotonic()))
        wait = min(waits)
        return None if wait == float("inf") else wait

    # ================================================================ run
    def run(self):
        if self.link:
            self.link.start()
            self.sender.start()
        self.input.start()
        self.status.start()
        if not isinstance(self.asr, NoEngine):
            self.asr.warm_up(on_done=self._wake.set)
        if self.keyboard:
            self.keyboard.start()
            print("Type a message and press Enter to send it. /help for commands.",
                  flush=True)
        if self.sender:
            self.sender.send_hello()
        log.info("ready: %s #%04X, peer %s, board %s, ASR %s", self.name, self.address,
                 self.history.name_for(self.config.radio.peer_address), self.board_mode,
                 self.asr.name)

        while self.running:
            if self.listening and self.recorder.elapsed >= self.config.audio.max_record_seconds:
                log.info("recording limit reached; sending what was said")
                self._on_talk_end()
            self._expire()
            self.screen.show(self.view())
            self.screen.apply_idle_policy()
            # Keep the codec warm while the screen is awake, so the first
            # word of the next press is kept; let it power down when dark.
            if self.screen.awake:
                self.recorder.arm()
            else:
                self.recorder.disarm()
            self._wake.wait(self._next_timeout())
            self._wake.clear()
        self._shutdown()

    def _expire(self, now: float | None = None):
        """Drop what has timed out: a status flash, an idle reply list."""
        now = time.monotonic() if now is None else now
        if self._flash and self._flash[2] <= now:
            self._flash = None
        if self.picker is not None and self._picker_until <= now:
            self.close_picker()

    def stop(self, reason: str = "normal"):
        log.info("stopping (%s)", reason)
        self.running = False
        self._wake.set()

    def _shutdown(self):
        self.recorder.close()
        self.input.stop()
        self.status.stop()
        if self.sender:
            self.sender.stop()
        if self.link:
            self.link.stop()
        if self.radio is not None:
            self.radio.close()
        try:
            self.screen.set_led((0, 0, 0))
            self.screen.restore()
            for hook in ("prepare_exit", "release_focus", "cleanup"):
                if hasattr(self.board, hook):
                    getattr(self.board, hook)()
        except Exception:
            log.debug("board cleanup failed", exc_info=True)
        log.info("stopped; %d frames drawn", self.screen.frames_pushed)


def bubble(message: h.Message, history: h.History, show_sender: bool = False,
           selected: bool = False) -> Bubble:
    """One message as the chat shows it."""
    clock = time.strftime("%H:%M", time.localtime(message.created))
    part = f" · part {message.part}" if message.part else ""
    if message.direction == h.RX:
        signal_part = f" · {message.rssi} dBm" if message.rssi is not None else ""
        return Bubble(message.text, False, f"{clock}{signal_part}{part}",
                      sender=history.name_for(message.peer) if show_sender else "",
                      selected=selected)
    if message.status == h.DELIVERED:
        meta, tone = f"{clock} ✓{part}", "ok"
    elif message.status == h.FAILED:
        meta, tone = f"{clock} ✗ not confirmed{part}", "error"
    elif message.status == h.SENDING:
        tries = f" (try {message.attempts})" if message.attempts > 1 else ""
        meta, tone = f"{clock} · sending…{tries}{part}", "busy"
    else:
        meta, tone = f"{clock} · waiting…{part}", "busy"
    return Bubble(message.text, True, meta, tone, selected=selected,
                  failed=message.status == h.FAILED)


def meta_line(message: h.Message, history: h.History) -> str:
    parts = [time.strftime("%H:%M:%S", time.localtime(message.created))]
    if message.msg_id:
        parts.append(f"#{message.msg_id}")
    if message.part:
        parts.append(f"part {message.part}")
    if message.direction == h.RX and message.rssi is not None:
        parts.append(f"{message.rssi} dBm")
    if message.direction == h.TX:
        if message.status == h.DELIVERED and message.rtt_ms is not None:
            parts.append(f"ACK {message.rtt_ms} ms")
        if message.acked_by is not None and message.peer == protocol.BROADCAST:
            parts.append(f"by {history.name_for(message.acked_by)}")
        if message.attempts > 1:
            parts.append(f"{message.attempts} tries")
    return " · ".join(parts)


def describe(message: h.Message, history: h.History) -> str:
    who = (f"RX {history.name_for(message.peer)}" if message.direction == h.RX
           else f"TX {history.name_for(message.peer)} [{message.status}]")
    return f"{meta_line(message, history)}  {who}: {message.text}"


def sample_view() -> View:
    from mfruit_sdk.status import Status

    return View(
        title="OrangePi", hints=HINTS_TALK, device=Status(3, 82, False),
        bubbles=[Bubble("Where are you?", False, "17:02 · -63 dBm"),
                 Bubble("On my way", True, "17:03 ✓", "ok"),
                 Bubble("Meet me at five o'clock", False, "17:04 · -64 dBm")])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="LoRa Messenger")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("--headless", action="store_true",
                        help="no Whisplay screen: keyboard and log only")
    parser.add_argument("--transcribe", metavar="WAV",
                        help="transcribe a WAV file with the configured engine and exit")
    parser.add_argument("--preview", metavar="PNG", help="render a sample screen and exit")
    args = parser.parse_args(argv)

    if args.preview:
        render(sample_view()).save(args.preview)
        print(f"wrote {args.preview}")
        return 0

    config = config_module.load(args.config)

    if args.transcribe:
        from audio import dsp
        engine = create_engine(config.asr)
        # The same filtering live audio gets, so a file tests the real path.
        pcm = dsp.speech_band(read_wav(args.transcribe))
        started = time.monotonic()
        engine.ensure_loaded()
        loaded = time.monotonic()
        text = engine.transcribe(pcm)
        print(f"{engine.name}: {text!r}")
        print(f"load {loaded - started:.1f} s, transcribe {time.monotonic() - loaded:.1f} s "
              f"for {len(pcm) / 32000:.1f} s of audio")
        return 0

    # Two copies would fight the daemon for the screen and interleave
    # bytes into one radio. Refuse quietly, and never ask for focus.
    try:
        lock = SingleInstance(config.data_dir).acquire()
    except AlreadyRunning as exc:
        log.info("%s; leaving it alone", exc)
        return 0

    app = Messenger(config, headless=args.headless)
    signal.signal(signal.SIGTERM, lambda *_: app.stop("SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: app.stop("SIGINT"))
    try:
        app.run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
