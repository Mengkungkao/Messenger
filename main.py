#!/usr/bin/env python3
"""LoRa Messenger: speak, see it as text, send it over LoRa.

    hold the button   record; on release, speech -> text -> LoRa
    1 click           step back through the message history
    2 clicks          resend the selected failed message, or back to live
    3 clicks          read the selected message aloud (text-to-speech)
    4 clicks          leave

    python3 main.py                      run (./run.sh does this)
    python3 main.py --headless           no screen: keyboard and log only
    python3 main.py --transcribe a.wav   test the ASR engine on a file
    python3 main.py --preview out.png    render a sample screen

The main loop does not poll. It draws, works out when something next
needs to change, and sleeps on an Event until then or until a button, a
packet or a finished transcription wakes it. Transcription runs on its
own thread, so the radio keeps receiving -- and ACKing -- while Whisper
thinks.
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
from controls.button import DOUBLE, QUAD, SINGLE, TRIPLE, GestureDetector
from controls.keyboard import HELP, Keyboard
from display import board as board_module
from display.whisplay import Screen, View, render
from lora import modepins, protocol
from lora.link import Link
from lora.sx126x import SX126x, port_conflicts
from messaging import history as h
from messaging.receiver import Receiver
from messaging.sender import Sender
from utils.logger import get_logger
from utils.single_instance import AlreadyRunning, SingleInstance

log = get_logger("main")

# A press this short after the hold threshold is a slip, not speech.
MIN_TALK_SECONDS = 0.4
LISTEN_FRAME_SECONDS = 0.1

TX_LINES = {
    h.QUEUED: ("TX: Waiting to send…", "busy"),
    h.SENDING: ("TX: Sending…", "busy"),
    h.DELIVERED: ("TX: Message delivered ✓", "ok"),
    # "Not confirmed", not "not delivered": when only the ACKs are lost the
    # message did arrive, and the sender cannot know that.
    h.FAILED: ("TX: Not confirmed ✗", "error"),
}


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
        self.cursor = None              # index into history; None = live view
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
        self.gestures = GestureDetector(
            on_gesture=self._on_gesture, on_hold_start=self._on_talk_start,
            on_hold_end=self._on_talk_end, debounce_ms=config.input.debounce_ms,
            click_window_ms=config.input.click_window_ms, hold_ms=config.input.hold_ms)
        self.gestures.attach(board)
        for hook, handler in (("on_exit_request", lambda *_: self.stop("daemon")),
                              ("on_focus_revoked", lambda *_: log.info("screen taken"))):
            if hasattr(board, hook):
                getattr(board, hook)(handler)

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
            self.cursor = None          # show it
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
            return "No ASR engine"
        if self.asr.error:
            return "ASR failed to load"
        return None

    def _on_talk_start(self):
        with self._lock:
            if self.listening or self.transcribing:
                return
            problem = self._asr_problem()
            if problem:
                self.flash(problem, "error")
                self.player.cue("failed")
                return
            if not self.recorder.start():
                self.flash("Microphone busy", "error")
                return
            self.listening = True
            self.cursor = None
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
            # Still show what was heard: the ASR half is worth testing alone.
            self.history.add(h.Message(h.TX, 0, self.config.radio.peer_address, text,
                                       h.FAILED))
            self.flash(self._radio_note or "Radio offline", "error")
            return
        with self._lock:
            self.cursor = None
        self.sender.send_text(text, self.config.radio.peer_address)
        self.player.cue("sent")

    # ============================================================ gestures
    def _on_gesture(self, gesture: str):
        dark = not self.screen.awake
        self.screen.poke()
        if dark and gesture != QUAD:
            self._wake.set()
            return      # a click on a dark screen only wakes it
        if gesture == SINGLE:
            self._browse()
        elif gesture == DOUBLE:
            selected = self.selected()
            if selected and selected.status == h.FAILED and self.sender \
                    and self.sender.resend(selected):
                self.flash("Resending", "busy")
            with self._lock:
                self.cursor = None
        elif gesture == TRIPLE:
            selected = self.selected() or self.history.latest(h.RX)
            if not self.player.can_speak:
                self.flash("No text-to-speech (espeak-ng)", "error")
            elif selected:
                self.player.speak(selected.text)
        elif gesture == QUAD:
            self.stop("four clicks")
        self._wake.set()

    def _browse(self):
        with self._lock:
            count = len(self.history)
            if not count:
                return
            if self.cursor is None:
                self.cursor = count - 2 if count > 1 else None
            else:
                self.cursor = self.cursor - 1 if self.cursor > 0 else None

    def selected(self) -> h.Message | None:
        with self._lock:
            if self.cursor is not None and 0 <= self.cursor < len(self.history):
                return self.history[self.cursor]
            return None

    # ============================================================ keyboard
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

    def view(self) -> View:
        view = View()
        with self._lock:
            shown = self.selected() or self.history.latest()
            if self.cursor is not None:
                view.position = f"{self.cursor + 1}/{len(self.history)}"
        if shown is not None:
            if shown.direction == h.RX:
                view.label = f"RX: {self.history.name_for(shown.peer)}"
                view.label_tone = "rx"
            else:
                view.label = f"TX → {self.history.name_for(shown.peer)}"
                view.label_tone = "tx"
            view.meta = meta_line(shown, self.history)
            view.message = shown.text

        last_tx = self.history.latest(h.TX)
        if last_tx is not None:
            line, tone = TX_LINES.get(last_tx.status, ("", "idle"))
            if last_tx.status == h.SENDING and last_tx.attempts > 1:
                line = f"TX: Sending… (try {last_tx.attempts})"
            view.tx_line, view.tx_tone = line, tone

        flash = self._flash
        if self.listening:
            view.status = f"Listening… {self.recorder.elapsed:.1f}s"
            view.status_tone, view.level = "listen", self.recorder.level
        elif self.transcribing:
            view.status, view.status_tone = "Converting speech…", "busy"
        elif flash and flash[2] > time.monotonic():
            view.status, view.status_tone = flash[0], flash[1]
        elif self.sender is not None and self.sender.busy:
            view.status, view.status_tone = "Waiting for ACK…", "busy"
        elif self._radio_note:
            view.status, view.status_tone = self._radio_note, "error"
        elif not self.asr.ready and not isinstance(self.asr, NoEngine) and not self.asr.error:
            view.status, view.status_tone = "Loading speech model…", "busy"
        else:
            view.status, view.status_tone = "Ready", "idle"

        signal_part = f" · {self.link.last_rssi} dBm" if self.link and self.link.last_rssi else ""
        view.footer = (f"{self.name} #{self.address:04X} · "
                       f"{self.config.radio.frequency_mhz} MHz{signal_part}")
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
        wait = min(waits)
        return None if wait == float("inf") else wait

    # ================================================================ run
    def run(self):
        if self.link:
            self.link.start()
            self.sender.start()
        self.gestures.start()
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
            if self._flash and self._flash[2] <= time.monotonic():
                self._flash = None
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

    def stop(self, reason: str = "normal"):
        log.info("stopping (%s)", reason)
        self.running = False
        self._wake.set()

    def _shutdown(self):
        self.recorder.close()
        self.gestures.stop()
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
    return View(label="RX: OrangePi", meta="17:00:04 · #1024 · -91 dBm",
                message="Meet me at five o'clock", tx_line="TX: Message delivered ✓",
                tx_tone="ok", status="Ready", footer="raspberrypi #1A2B · 868 MHz")


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
