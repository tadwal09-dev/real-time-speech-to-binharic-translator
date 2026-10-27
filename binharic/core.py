"""The translator runtime shared by the CLI, TUI and GUI.

Threads:
  mic      - reads the microphone into a queue (never blocks on recognition)
  stt      - VAD + speech recognition; queues text to send
  out      - owns the output: screech, voice passthrough, or silence
  hear     - optional: decodes binharic arriving on another source
  control  - unix socket so hotkeys can run `binharic toggle` etc.

While ON your voice is muted on the output and only the screech is sent.
``send_mode`` picks when: ``phrase`` (after you finish a sentence) or
``live`` (words go out as you say them).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from . import codec, modem, pulse
from .audio import Input, Output
from .stt import LIVE, PHRASE, STT_SR, Transcriber, make_engine
from .vad import Decimator

SR = 48000
BLOCK = SR // 50  # 20 ms


@dataclass
class Settings:
    enabled: bool = True
    send_mode: str = PHRASE
    mode: str = "binharic"
    key: str | None = None
    speed: float = 1.0
    volume: float = 0.6
    passthrough: bool = False
    engine: str = "auto"
    model: str | None = None
    live_model: str | None = "base.en"
    device: str = "auto"
    sink: str | None = None
    source: str | None = None
    output_device: str | None = None
    input_device: str | None = None
    duplex: str = "auto"        # auto | full | half
    end_ms: int = 700           # silence that ends a phrase
    hear: str | None = None     # source to decode incoming binharic from
    hear_key: str | None = None
    beam: int = 5               # whisper beam size for the final pass (1 = fastest)
    language: str | None = None # whisper language for multilingual models (None = detect)
    partials: bool = True       # show what you're saying while you say it (phrase mode)
    test: bool = False          # test mode: screech to your speakers, decode it back, time it
    test_air: bool = False      # ...and also decode what the mic hears (speaker -> mic)
    control_socket: bool = True
    notify: bool = True         # desktop popups when toggled by hotkey

    def public(self) -> dict:
        d = asdict(self)
        d["key"] = bool(self.key)
        d["hear_key"] = bool(self.hear_key)
        return d


def socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/binharic-{os.getuid()}"
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base) / "binharic.sock"


def desktop_notify(text: str) -> None:
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Binharic", "-t", "1500", "-i", "audio-input-microphone",
                          "-h", "string:x-canonical-private-synchronous:binharic", "Binharic", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def send_command(cmd: str, timeout: float = 3.0) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(socket_path()))
    except OSError:
        s.close()
        raise RuntimeError("binharic is not running (start `binharic listen`, `tui` or `gui`)") from None
    with s:
        s.sendall(cmd.encode() + b"\n")
        return s.makefile().readline().strip()


Event = dict
Listener = Callable[[str, Event], None]


class Translator:
    def __init__(self, settings: Settings):
        self.s = settings
        self._listeners: list[Listener] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._mic_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=500)
        self._voice_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=25)
        self._pending: list[str] = []
        self._pending_lock = threading.Lock()
        self._abort = threading.Event()
        self.screeching = threading.Event()
        self.speaking = False
        self.level = 0.0
        self.ready = False
        self.engine_name = "?"
        self.transcriber: Transcriber | None = None
        self._out: Output | None = None
        self._mic: Input | None = None
        self._hear_in: Input | None = None
        self._sock: socket.socket | None = None
        self._warned_half = False
        self.fatal: str | None = None
        self.speakers: str | None = None
        self.speakers_label = "default output"
        self.mic_label = "default input"
        self._stopped_once = False
        self._local_out: Output | None = None
        self._air_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=600)
        self._mute_until = 0.0
        self._last_arrival = 0.0
        self._target_lock = threading.Lock()

    # -- events -----------------------------------------------------------
    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def emit(self, kind: str, **data) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, data)
            except Exception:  # a broken UI must not kill the audio
                traceback.print_exc()

    def log(self, text: str, level: str = "info") -> None:
        self.emit("log", text=text, level=level)

    def status(self) -> dict:
        d = self.s.public()
        d.update(ready=self.ready, engine_name=self.engine_name, speaking=self.speaking,
                 screeching=self.screeching.is_set(), full_duplex=self.full_duplex,
                 queued=len(self._pending))
        return d

    def _changed(self) -> None:
        self.emit("status", **self.status())

    @property
    def full_duplex(self) -> bool:
        if self.s.test:
            return False  # test mode screeches on your speakers, which the mic can hear
        if self.s.duplex == "auto":
            return bool(self.s.sink)  # a virtual sink can't leak back into the mic
        return self.s.duplex == "full"

    # -- controls ---------------------------------------------------------
    def set_enabled(self, on: bool) -> None:
        self.s.enabled = on
        if not on:
            self._abort.set()
            with self._pending_lock:
                self._pending.clear()
        msg = "ON: voice muted, speaking binharic" if on else "OFF" + (
            ": voice passes through" if self.s.passthrough else ": silent")
        self.log(msg)
        if self.s.notify:
            desktop_notify(msg)
        self._changed()

    def toggle(self) -> None:
        self.set_enabled(not self.s.enabled)

    def set_send_mode(self, mode: str) -> None:
        if mode == "toggle":
            mode = PHRASE if self.s.send_mode == LIVE else LIVE
        if mode not in (PHRASE, LIVE):
            raise ValueError("send mode is 'phrase' or 'live'")
        self.s.send_mode = mode
        if self.transcriber:
            self.transcriber.send_mode = mode
        msg = "sending as you talk" if mode == LIVE else "sending after you finish"
        self.log(msg)
        if self.s.notify:
            desktop_notify(msg)
        if mode == LIVE and not self.full_duplex and not self._warned_half:
            self._warned_half = True
            self.log("mic is ignored while screeching (no virtual sink); use --duplex full "
                     "if your speakers can't feed back", "warn")
        self._changed()

    def set_mode(self, mode: str) -> None:
        modem.get_mode(mode)
        self.s.mode = mode
        self.log(f"screech mode: {mode}")
        self._changed()

    def set_key(self, key: str | None) -> None:
        self.s.key = key or None
        self.log("key set" if key else "key cleared: anyone with the decoder can read you")
        self._changed()

    def set_test(self, on: bool, air: bool | None = None) -> None:
        self.s.test = on
        if air is not None:
            self.s.test_air = air
        if on and self._out is not None:
            self._target()
        if on:
            self.log("TEST MODE: say something. The screech plays on your speakers "
                     "(not the Binharic Mic) and is decoded back to check it")
        else:
            self.log("test mode off")
        self._changed()

    def set_passthrough(self, on: bool) -> None:
        self.s.passthrough = on
        self._changed()

    def set_speed(self, speed: float) -> None:
        self.s.speed = max(0.5, min(3.0, float(speed)))
        self._changed()

    def set_end_ms(self, ms: int) -> None:
        self.s.end_ms = int(ms)
        if self.transcriber:
            self.transcriber.end_ms = self.s.end_ms
            self.transcriber.vad.set_end_ms(self.s.end_ms)
        self._changed()

    def say(self, text: str) -> None:
        """Queue typed text for screeching (works even while OFF)."""
        self._queue_text(" ".join(text.split()), True, force=True)

    def request_stop(self) -> None:
        """Ask the layer to shut down (safe from a signal handler)."""
        self._stop.set()

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self.s.control_socket:
            self._start_control()  # first, so a second copy fails before touching audio
        self._resolve_devices()
        warn = lambda m: self.log(m, "warn")  # noqa: E731
        self._out = Output(SR, sink=self.s.sink or self.speakers, device=self.s.output_device, blocksize=BLOCK,
                           notify=warn, before_reconnect=self._recreate_virtual_mic)
        self._mic = Input(SR, source=self.s.source, device=self.s.input_device, blocksize=BLOCK * 2, notify=warn)
        out = pulse.describe(self.s.sink, "sinks") if self.s.sink else self.speakers_label
        self.log(f"mic: {self.mic_label}  →  screech: {out}"
                 + (f"  (test mode plays on: {self.speakers_label})" if self.s.test and self.s.sink else ""))
        if self.s.test:
            self._target()  # open the speakers now, not on the first screech
        self._spawn(self._out_loop, "out")
        self._spawn(self._air_loop, "air")
        self._spawn(self._mic_loop, "mic")
        self._spawn(self._stt_loop, "stt")
        if self.s.hear:
            self._spawn(self._hear_loop, "hear")
        self._changed()

    def stop(self) -> None:
        if self._stopped_once:
            return
        self._stopped_once = True
        self._stop.set()
        self._abort.set()
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)  # wakes the thread blocked in accept()
            except OSError:
                pass
        # let the threads finish their current block before closing the devices
        # under them (closing a PortAudio stream mid-read can crash)
        deadline = time.monotonic() + 1.5
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=max(0.05, deadline - time.monotonic()))
        for closer in (self._mic, self._hear_in):
            try:
                closer and closer.close()
            except Exception:
                pass
        for out in {id(o): o for o in (self._out, self._local_out) if o}.values():
            try:
                out.close()
            except Exception:
                pass
        if self._sock:
            self._sock.close()
            try:
                socket_path().unlink()
            except FileNotFoundError:
                pass

    def wait(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.2)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _spawn(self, fn, name):
        def run():
            try:
                fn()
            except Exception as e:
                if not self._stop.is_set():
                    if os.environ.get("BINHARIC_DEBUG"):
                        traceback.print_exc()
                    self.emit("error", text=f"{name}: {e}")
                    if name in ("stt", "mic", "out"):
                        # the layer can't work without these: say so and shut down
                        # instead of sitting there looking alive
                        self.fatal = f"{name} failed: {e}"
                        self._stop.set()
        t = threading.Thread(target=run, name=f"binharic-{name}", daemon=True)
        t.start()
        self._threads.append(t)

    def _resolve_devices(self) -> None:
        """On PipeWire, talk to it directly (parec/pacat) for the mic and the
        speakers too: PortAudio through ALSA is unreliable there. Also make
        sure "your mic" isn't our own virtual mic, and "your speakers" aren't
        the virtual sink, in case PipeWire made them the defaults."""
        self.speakers = None
        self.speakers_label = "default output"
        self.mic_label = self.s.input_device or "default input"
        if not pulse.available():
            return
        if self.s.sink == pulse.SINK and not pulse.virtual_mic_exists():
            pulse.mic_up()
        # wrong names: say so now, with the list of real ones
        if self.s.sink:
            pulse.check_device(self.s.sink, "sinks")
        if self.s.source:
            pulse.check_device(self.s.source, "sources")
        if self.s.hear:
            pulse.check_device(self.s.hear, "sources")
        if not self.s.source and not self.s.input_device:
            self.s.source = pulse.real_source()
        if self.s.source:
            self.mic_label = pulse.describe(self.s.source, "sources")
        if not self.s.output_device:
            self.speakers = pulse.real_sink()
            if self.speakers:
                self.speakers_label = pulse.describe(self.speakers, "sinks")

    def _recreate_virtual_mic(self) -> None:
        """After a sound-server restart the virtual devices are gone: rebuild
        them before reconnecting to them."""
        if self.s.sink == pulse.SINK and not pulse.virtual_mic_exists():
            pulse.mic_up()
            self.log("re-created the Binharic Mic after the sound server restarted", "warn")

    # -- threads ----------------------------------------------------------
    def _mic_loop(self):
        while not self._stop.is_set():
            try:
                block = self._mic.read()
            except EOFError:
                break
            now = time.monotonic()
            if (self.screeching.is_set() or now < self._mute_until) and not self.full_duplex:
                if self.s.test and self.s.test_air:
                    try:
                        self._air_q.put_nowait(block)
                    except queue.Full:
                        pass
                block = np.zeros_like(block)  # keep timing, drop our own echo
            try:
                self._mic_q.put_nowait((block, now))
            except queue.Full:
                self._mic_q.get_nowait()
                self._mic_q.put_nowait((block, now))

    def _stt_loop(self):
        self.log("loading speech recognition…")
        engine = make_engine(self.s.engine, self.s.model, self.s.device, self.s.live_model, self.s.beam,
                             self.s.language)
        self.engine_name = f"{engine.name} · {engine.label}" if engine.label != engine.name else engine.name
        self.transcriber = Transcriber(engine, self.s.send_mode, on_speech=self._on_speech,
                                       on_partial=lambda t: self.emit("partial", text=t),
                                       on_send=self._on_send, end_ms=self.s.end_ms,
                                       partials=self.s.partials)
        dec = Decimator(SR // STT_SR)
        while not self._mic_q.empty():  # don't transcribe what was said while loading
            self._mic_q.get_nowait()
        self.ready = True
        if getattr(engine, "notice", ""):
            self.log(engine.notice, "warn")
        self.log(f"ready ({self.engine_name})" + (" · say something" if self.s.test else ""))
        self._changed()
        last_level = 0.0
        ready_at = last_block = time.monotonic()
        peak = 0.0
        warned_dead = warned_silent = False
        while not self._stop.is_set():
            try:
                blocks = [self._mic_q.get(timeout=0.2)]
            except queue.Empty:
                if not warned_dead and time.monotonic() - last_block > 3:
                    warned_dead = True
                    self.log(f"the microphone ({self.mic_label}) isn't delivering any audio. Check it with "
                             "`binharic doctor`, pick another with `binharic devices` + --source NAME, "
                             "or in pavucontrol", "error")
                continue
            last_block = time.monotonic()
            # drain whatever piled up while recognition was busy
            while True:
                try:
                    blocks.append(self._mic_q.get_nowait())
                except queue.Empty:
                    break
            self._last_arrival = blocks[-1][1]
            blocks = [b for b, _ in blocks]
            audio = np.concatenate(blocks)
            if not warned_silent and not self.screeching.is_set():
                peak = max(peak, float(np.abs(audio).max()))
                if time.monotonic() - ready_at > 5:
                    warned_silent = True
                    if peak < 3e-4:
                        self.log(f"the microphone ({self.mic_label}) is silent: is it muted or the wrong "
                                 "device? `binharic devices` lists inputs (use --source NAME), or fix it in "
                                 "pavucontrol", "error")
            if not (self.s.enabled or self.s.test):
                if self.s.passthrough:
                    for b in blocks:
                        try:
                            self._voice_q.put_nowait(b)
                        except queue.Full:
                            pass
                if self.transcriber.speaking:
                    self.transcriber.reset()
                self.level = float(np.sqrt(np.mean(audio * audio)))
            else:
                self.transcriber.feed(dec(audio))
                self.level = self.transcriber.level
            now = time.monotonic()
            if now - last_level > 0.05:
                last_level = now
                self.emit("level", level=self.level)

    def _on_speech(self, on: bool):
        self.speaking = on
        self.emit("speech", speaking=on)

    def _on_send(self, text: str, final: bool):
        meta = {}
        tr = self.transcriber
        if final and tr is not None:
            # when you actually stopped talking, from mic timestamps + VAD's quiet count
            meta = dict(stopped_at=self._last_arrival - tr.vad.since_voice / STT_SR,
                        stt_ms=tr.last_stt_ms, speculative=tr.last_speculative)
        meta["heard_at"] = time.monotonic()
        self._queue_text(text, final, meta=meta)

    def _queue_text(self, text: str, final: bool, force: bool = False, meta: dict | None = None):
        if not text or (not (self.s.enabled or self.s.test) and not force):
            return
        meta = dict(meta or {}, forced=force)
        with self._pending_lock:
            self._pending.append((text, meta))
            self._abort.clear()  # a new message after an OFF: let it play
        self.emit("queued", text=text, final=final)

    def _take_pending(self) -> tuple[str, dict] | None:
        with self._pending_lock:
            if not self._pending:
                return None
            text = " ".join(t for t, _ in self._pending)
            meta = {}
            for _, m in self._pending:
                forced = meta.get("forced", False) or m.get("forced", False)
                meta.update(m)
                meta["forced"] = forced
            self._pending.clear()
        return text, meta

    def _target(self) -> Output:
        """Where the screech goes: the Binharic Mic, or your speakers in test mode."""
        if not self.s.test or not self.s.sink:
            return self._out
        with self._target_lock:  # the GUI thread and the output thread can both get here
            if self._local_out is None:
                self._local_out = Output(SR, sink=self.speakers, device=self.s.output_device, blocksize=BLOCK,
                                         notify=lambda m: self.log(m, "warn"))
        return self._local_out

    def _out_loop(self):
        out = self._out
        silence = np.zeros(BLOCK, dtype=np.float32)
        checked = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - checked > 0.5:  # reconnect as soon as the sound server comes back
                checked = now
                for o in {id(o): o for o in (self._out, self._local_out) if o}.values():
                    o.ensure_alive()
            item = self._take_pending()
            if item:
                self._play_text(self._target(), *item)
                continue
            block = None
            if self.s.passthrough and not self.s.enabled:
                try:
                    block = self._voice_q.get_nowait()
                except queue.Empty:
                    pass
            else:
                while not self._voice_q.empty():  # stale voice must never leak later
                    self._voice_q.get_nowait()
            if block is not None:
                out.write(block)
            elif out.needs_feeding:
                out.write(silence)
            else:
                # PipeWire/Pulse: leave the buffer empty while idle, so a new
                # screech doesn't queue up behind buffered silence
                time.sleep(0.005)

    def _play_text(self, out: Output, text: str, meta: dict | None = None):
        meta = meta or {}
        if not (self.s.enabled or self.s.test or meta.get("forced")):
            return  # switched OFF after this was queued
        chunks = codec.split_text(text)
        self.screeching.set()
        self._changed()
        s = self.s
        lead = modem.lead_in(SR, s.volume)
        try:
            for n, chunk in enumerate(chunks):
                # synthesise the whole message before playing any of it: a gap
                # in the middle (the speaker running dry while we compute)
                # corrupts the screech, so this is worth the ~50 ms
                t = time.monotonic()
                body = modem.encode_body(chunk, s.mode, key=s.key, sr=SR, speed=s.speed, volume=s.volume)
                synth_ms = (time.monotonic() - t) * 1000
                first_sound = time.monotonic() + out.latency  # when it actually leaves the speaker
                info = dict(text=chunk, hex=codec.to_hex(chunk), binary=codec.to_binary(chunk),
                            seconds=(len(lead) + len(body)) / SR, mode=s.mode, synth_ms=synth_ms,
                            test=s.test)
                if n == 0 and "stopped_at" in meta:
                    info.update(latency_ms=(first_sound - meta["stopped_at"]) * 1000,
                                stt_ms=meta["stt_ms"], speculative=meta["speculative"])
                self.emit("sent", **info)
                audio = np.concatenate([lead, body])
                if s.test:
                    self._verify(chunk, np.concatenate([lead, body]), s.mode, s.key, s.speed)
                step = BLOCK * 5  # 100 ms writes: fewer wake-ups for a busy CPU to miss
                for i in range(0, len(audio), step):
                    if self._abort.is_set() or self._stop.is_set():
                        self.log("screech cut off")
                        return
                    out.write(audio[i:i + step])
        finally:
            self._mute_until = time.monotonic() + 0.4  # speaker/room tail
            self.screeching.clear()
            self._changed()

    def _verify(self, text, audio, mode, key, speed):
        def run():
            t = time.monotonic()
            got = modem.decode_audio(audio, SR, mode, key=key, speed=speed)
            ms = (time.monotonic() - t) * 1000
            decoded = got[0].display() if got else None
            self.emit("verify", via="digital", sent=text, decoded=decoded, ok=decoded == text, decode_ms=ms)
        threading.Thread(target=run, daemon=True).start()

    def _air_loop(self):
        """Test mode: decode the screech as the mic heard it from the speakers."""
        dec = None
        while not self._stop.is_set():
            try:
                block = self._air_q.get(timeout=0.3)
            except queue.Empty:
                if dec is not None and not self.screeching.is_set() and time.monotonic() > self._mute_until:
                    for m in dec.flush():  # the tail of the screech may still be unscanned
                        self.emit("verify", via="air", sent=None, decoded=m.display(), ok=not m.locked,
                                  decode_ms=0.0)
                    dec = None  # this attempt is over; start clean for the next screech
                continue
            if dec is None:
                dec = modem.StreamDecoder(SR, "auto", key=self.s.key, speed=self.s.speed)
            for m in dec.feed(block):
                self.emit("verify", via="air", sent=None, decoded=m.display(), ok=not m.locked, decode_ms=0.0)

    def _hear_loop(self):
        self._hear_in = Input(SR, source=self.s.hear, blocksize=BLOCK * 5, notify=lambda m: self.log(m, "warn"))
        dec = modem.StreamDecoder(SR, "auto", key=self.s.hear_key or self.s.key, speed=self.s.speed)
        self.log(f"decoding incoming binharic from {self.s.hear}")
        while not self._stop.is_set():
            try:
                block = self._hear_in.read()
            except EOFError:
                break
            for m in dec.feed(block):
                name = modem.MODES_BY_ID.get(m.mode_id)
                self.emit("received", text=m.display(), locked=m.locked,
                          mode=name.name if name else "?")

    # -- control socket ---------------------------------------------------
    def _start_control(self):
        path = socket_path()
        if path.exists():
            try:
                send_command("ping", timeout=0.5)
            except RuntimeError:
                path.unlink()  # stale socket from a crash
            else:
                raise RuntimeError("binharic is already running (the background service?) - "
                                   "control it with `binharic toggle`, or `systemctl --user stop binharic`")
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        os.chmod(path, 0o600)
        self._sock.listen(4)
        self._spawn(self._control_loop, "control")

    def _control_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            with conn:
                conn.settimeout(2.0)  # a client that connects and says nothing can't block hotkeys
                try:
                    line = conn.makefile().readline().strip()
                    reply = self.handle(line)
                except Exception as e:
                    reply = f"error: {e}"
                try:
                    conn.sendall(reply.encode() + b"\n")
                except OSError:
                    pass  # the client gave up waiting; the next one is fine

    def handle(self, line: str) -> str:
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        if cmd == "ping":
            return "pong"
        if cmd == "toggle":
            self.toggle()
        elif cmd == "on":
            self.set_enabled(True)
        elif cmd == "off":
            self.set_enabled(False)
        elif cmd == "send":
            self.set_send_mode(arg or "toggle")
        elif cmd == "mode":
            self.set_mode(arg)
        elif cmd == "passthrough":
            self.set_passthrough(arg != "off" if arg else not self.s.passthrough)
        elif cmd == "test":
            self.set_test(arg != "off" if arg else not self.s.test)
        elif cmd == "say":
            self.say(arg)
        elif cmd == "quit":
            self._stop.set()
        elif cmd != "status":
            return f"error: unknown command {cmd!r}"
        return json.dumps(self.status())
