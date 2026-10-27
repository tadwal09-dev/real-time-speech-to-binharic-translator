"""Audio in/out.

Two backends:

* PulseAudio/PipeWire tools (``pacat`` / ``parec``) when a sink or source
  name is given - this is how we feed the "Binharic Mic" virtual device.
* PortAudio via ``sounddevice`` for the default devices otherwise.
"""

from __future__ import annotations

import fcntl
import shutil
import subprocess
import wave

import numpy as np

F_SETPIPE_SZ = 1031


def _small_pipe(f, size: int = 8192) -> None:
    try:
        fcntl.fcntl(f.fileno(), F_SETPIPE_SZ, size)
    except OSError:
        pass


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"`{tool}` not found - install it with: sudo pacman -S libpulse")
    return path


class Output:
    """Blocking mono float32 sink. ``write`` returns once the audio is queued."""

    def __init__(self, sr: int, sink: str | None = None, device=None, blocksize: int = 1024):
        self.sr = sr
        self.blocksize = blocksize
        self._proc = self._stream = None
        if sink:
            self._proc = subprocess.Popen(
                [_require("pacat"), "--playback", f"--device={sink}", "--raw", "--format=s16le",
                 f"--rate={sr}", "--channels=1", "--latency-msec=40",
                 "--client-name=binharic", "--stream-name=binharic"],
                stdin=subprocess.PIPE)
            _small_pipe(self._proc.stdin)
        else:
            import sounddevice as sd
            self._stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32",
                                           device=device, blocksize=blocksize)
            self._stream.start()

    def write(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32).ravel()
        if self._proc:
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            self._proc.stdin.write(pcm)
            self._proc.stdin.flush()
        else:
            self._stream.write(samples.reshape(-1, 1))

    def close(self) -> None:
        if self._proc:
            try:
                self._proc.stdin.close()
            except BrokenPipeError:
                pass
            self._proc.wait(timeout=5)
        if self._stream:
            self._stream.stop()
            self._stream.close()


class Input:
    """Iterator of mono float32 blocks from a mic / source."""

    def __init__(self, sr: int, source: str | None = None, device=None, blocksize: int = 4800):
        self.sr = sr
        self.blocksize = blocksize
        self._proc = self._stream = None
        if source:
            self._proc = subprocess.Popen(
                [_require("parec"), f"--device={source}", "--raw", "--format=s16le",
                 f"--rate={sr}", "--channels=1", "--latency-msec=40", "--client-name=binharic"],
                stdout=subprocess.PIPE)
        else:
            import sounddevice as sd
            self._stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                          device=device, blocksize=blocksize)
            self._stream.start()

    def read(self) -> np.ndarray:
        if self._proc:
            raw = self._proc.stdout.read(self.blocksize * 2)
            if not raw:
                raise EOFError
            return np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        data, _overflow = self._stream.read(self.blocksize)
        return data[:, 0].copy()

    def close(self) -> None:
        if self._proc:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        if self._stream:
            self._stream.stop()
            self._stream.close()


def write_wav(path: str, samples: np.ndarray, sr: int) -> None:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV sample width: {width}")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr
