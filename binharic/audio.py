"""Audio in/out.

Two backends:

* PulseAudio/PipeWire tools (``pacat`` / ``parec``) when a sink or source
  name is given. This is the normal path on a PipeWire desktop, and how we
  feed the "Binharic Mic" virtual device. These streams reconnect by
  themselves if the sound server restarts (updates, suspend/resume).
* PortAudio via ``sounddevice`` for the default devices otherwise.
"""

from __future__ import annotations

import collections
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from typing import Callable

import numpy as np

F_SETPIPE_SZ = 1031
RECONNECT_SECONDS = 30.0


def _small_pipe(f, size: int = 4096) -> None:
    try:
        import fcntl  # Linux only
        fcntl.fcntl(f.fileno(), F_SETPIPE_SZ, size)
    except (ImportError, OSError):
        pass


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"`{tool}` not found - install it with: sudo pacman -S libpulse")
    return path


def _warn(msg: str) -> None:
    print(f"binharic: {msg}", file=sys.stderr, flush=True)


class _PulseStream:
    """A pacat/parec child process that is checked at start and restarted if
    the sound server goes away."""

    WATCH_SECONDS = 10.0  # how long after connecting we check where the stream went

    def __init__(self, cmd: list[str], what: str, playback: bool,
                 notify: Callable[[str], None] | None = None,
                 before_reconnect: Callable[[], None] | None = None,
                 device: str | None = None):
        self.cmd = cmd
        self.what = what            # e.g. "play to 'binharic_out'"
        self.playback = playback
        self.device = device
        self.notify = notify or _warn
        self.before_reconnect = before_reconnect
        self.closed = False
        self._errors: collections.deque[str] = collections.deque(maxlen=4)
        self.proc = self._spawn()
        self._check_started()
        self._watch()

    def _spawn(self) -> subprocess.Popen:
        proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE if self.playback else subprocess.DEVNULL,
                                stdout=None if self.playback else subprocess.PIPE, stderr=subprocess.PIPE,
                                # own session: Ctrl+C in the terminal must not kill the audio
                                # tools under us; we close them ourselves on the way out
                                start_new_session=True)
        if self.playback:
            _small_pipe(proc.stdin)

        def drain(stream=proc.stderr):  # keep the last error lines; never let the pipe fill up
            for line in stream:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._errors.append(text)
        threading.Thread(target=drain, daemon=True).start()
        return proc

    def error(self) -> str:
        return self._errors[-1] if self._errors else "the audio tool exited"

    def _check_started(self) -> None:
        """A wrong device name makes pacat/parec quit within a moment: report it
        now, with its message, instead of failing later on."""
        try:
            self.proc.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            return
        time.sleep(0.05)  # let the stderr reader catch the message
        raise RuntimeError(f"can't {self.what}: {self.error()}")

    def _watch(self) -> None:
        """PipeWire's session manager can connect a new stream to the default
        device instead of the one we named, when it is starting up at the same
        moment (a sound-server restart, logging in). The screech would then play
        on your speakers while the Binharic Mic stays silent. Watch where the
        stream went for a while, and move it back."""
        if not self.device:
            return
        proc = self.proc

        def run():
            from . import pulse
            end = time.monotonic() + self.WATCH_SECONDS
            while time.monotonic() < end and self.proc is proc and not self.closed and proc.poll() is None:
                found = pulse.stream_device(proc.pid, self.playback)
                if found and found[1] and found[1] != self.device:
                    try:
                        pulse.move_stream(found[0], self.device, self.playback)
                        self.notify(f"the sound server connected binharic to '{found[1]}'; "
                                    f"moved it back to '{self.device}'")
                    except Exception:
                        pass  # gone meanwhile; the next check tells
                time.sleep(0.5)
        threading.Thread(target=run, daemon=True).start()

    def recover(self) -> None:
        """Called when the stream broke. Reconnect, or raise if that's hopeless."""
        if self.closed:
            raise EOFError
        try:
            self.proc.kill()
            self.proc.wait(timeout=2)
        except Exception:
            pass
        reason = self.error()
        self.notify(f"lost the audio connection ({reason}); reconnecting…")
        deadline = time.monotonic() + RECONNECT_SECONDS
        while time.monotonic() < deadline and not self.closed:
            time.sleep(1.0)
            try:
                if self.before_reconnect:
                    self.before_reconnect()
                self.proc = self._spawn()
                self._check_started()
            except Exception as e:  # server not back yet
                reason = str(e)
                continue
            self.notify("audio reconnected")
            self._watch()
            return
        if self.closed:
            raise EOFError
        raise RuntimeError(f"can't {self.what}: {reason}")

    def close(self) -> None:
        self.closed = True
        try:
            if self.playback:
                self.proc.stdin.close()  # pacat plays what's buffered, then exits
                self.proc.wait(timeout=5)
            else:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class Output:
    """Blocking mono float32 sink. ``write`` returns once the audio is queued."""

    # a buffer deep enough that a busy CPU (Whisper on every core!) can't starve
    # the screech: one gap in the middle of a screech loses the whole message.
    # It costs no start delay: an idle stream fills it instantly from a screech.
    PACAT_LATENCY_MS = 250
    PACAT_START_S = 0.05  # measured: heard 20 ms (PulseAudio) to 70 ms (PipeWire) after the write

    def __init__(self, sr: int, sink: str | None = None, device=None, blocksize: int = 1024,
                 notify: Callable[[str], None] | None = None,
                 before_reconnect: Callable[[], None] | None = None):
        self.sr = sr
        self.blocksize = blocksize
        self._pulse: _PulseStream | None = None
        self._stream = None
        # PortAudio wants a steady stream of writes; pacat is happy to sit idle
        self.needs_feeding = not sink
        # latency: how long what was just written keeps playing (the buffer);
        # start_latency: how long until a new screech is heard
        self.latency = self.PACAT_LATENCY_MS / 1000 if sink else 0.0
        self.start_latency = self.PACAT_START_S if sink else 0.0
        if sink:
            self._pulse = _PulseStream(
                [_require("pacat"), "--playback", f"--device={sink}", "--raw", "--format=s16le",
                 f"--rate={sr}", "--channels=1", f"--latency-msec={self.PACAT_LATENCY_MS}",
                 "--client-name=binharic", "--stream-name=binharic"],
                f"play to '{sink}'", playback=True, notify=notify, before_reconnect=before_reconnect, device=sink)
        else:
            import sounddevice as sd
            self._stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32",
                                           device=device, blocksize=blocksize, latency="low")
            self._stream.start()
            self.latency = self.start_latency = float(self._stream.latency)  # kept full by feeding

    def ensure_alive(self) -> None:
        """An idle pacat can't notice the sound server going away until the
        next write; check now so we reconnect (and rebuild the Binharic Mic)
        straight away instead of losing the start of the next screech."""
        if self._pulse and self._pulse.proc.poll() is not None:
            self._pulse.recover()

    def write(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32).ravel()
        if self._pulse:
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            while True:
                try:
                    self._pulse.proc.stdin.write(pcm)
                    self._pulse.proc.stdin.flush()
                    return
                except (BrokenPipeError, OSError, ValueError):
                    self._pulse.recover()  # raises if the sound server doesn't come back
        else:
            self._stream.write(samples.reshape(-1, 1))

    def close(self) -> None:
        if self._pulse:
            self._pulse.close()
        if self._stream:
            self._stream.stop()
            self._stream.close()


class Input:
    """Blocks of mono float32 audio from a mic / source."""

    def __init__(self, sr: int, source: str | None = None, device=None, blocksize: int = 4800,
                 notify: Callable[[str], None] | None = None):
        self.sr = sr
        self.blocksize = blocksize
        self.notify = notify or _warn
        self.overflows = 0            # PortAudio: blocks where samples were lost
        self._warned_overflow = -1e9
        self._pulse: _PulseStream | None = None
        self._stream = None
        if source:
            self._pulse = _PulseStream(
                [_require("parec"), f"--device={source}", "--raw", "--format=s16le",
                 f"--rate={sr}", "--channels=1", "--latency-msec=20", "--client-name=binharic"],
                f"record from '{source}'", playback=False, notify=notify, device=source)
        else:
            import sounddevice as sd
            # "high": with "low", Windows' MME gets a few tiny buffers and drops input
            # whenever a read comes late; a little more input latency costs nothing here
            # (recognition takes far longer), lost samples garble speech and screeches
            self._stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                          device=device, blocksize=blocksize, latency="high")
            self._stream.start()
        # BINHARIC_DUMP_INPUT=folder: keep everything recorded, written as a WAV on close
        # (to see exactly what binharic heard when diagnosing a mic)
        self._dump: list[np.ndarray] | None = [] if os.environ.get("BINHARIC_DUMP_INPUT") else None

    def read(self) -> np.ndarray:
        block = self._read()
        if self._dump is not None:
            self._dump.append(block)
        return block

    def _read(self) -> np.ndarray:
        if self._pulse:
            while True:
                raw = self._pulse.proc.stdout.read(self.blocksize * 2)
                if raw:
                    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
                self._pulse.recover()  # EOF: parec died (server restart?) - EOFError once closed
        data, overflow = self._stream.read(self.blocksize)
        if overflow:
            self.overflows += 1
            now = time.monotonic()
            if now - self._warned_overflow > 60:
                self._warned_overflow = now
                self.notify("the microphone dropped some audio (the computer was busy); "
                            "words may be misheard")
        return data[:, 0].copy()

    def close(self) -> None:
        if self._pulse:
            self._pulse.close()
        if self._stream:
            self._stream.stop()
            self._stream.close()
        if self._dump:
            folder = os.environ["BINHARIC_DUMP_INPUT"]
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"input-{os.getpid()}-{id(self) % 10000}.wav")
            write_wav(path, np.concatenate(self._dump), self.sr)
            self._dump = None


def write_wav(path: str, samples: np.ndarray, sr: int) -> None:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Read a PCM WAV (8/16/24/32-bit, any channel count) as mono float32."""
    try:
        with wave.open(path, "rb") as w:
            sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise RuntimeError(f"can't read {path} ({e}); convert it first, e.g. "
                           f"ffmpeg -i {path} -ac 1 -ar 48000 -sample_fmt s16 out.wav") from None
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw[: len(raw) // 3 * 3], dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        data = (np.where(v >= 1 << 23, v - (1 << 24), v)).astype(np.float32) / float(1 << 23)
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"unsupported WAV sample width: {width} bytes")
    if ch > 1:
        data = data[: len(data) // ch * ch].reshape(-1, ch).mean(axis=1)
    return data, sr
