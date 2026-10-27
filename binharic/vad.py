"""Voice activity detection and resampling helpers (numpy only)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class Decimator:
    """Streaming integer-factor downsampler with a windowed-sinc low-pass."""

    def __init__(self, factor: int, taps: int = 63):
        self.factor = factor
        n = np.arange(taps) - (taps - 1) / 2
        cutoff = 0.9 / factor / 2  # cycles/sample, a bit under the new Nyquist
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(taps)
        self.h = (h / h.sum()).astype(np.float32)
        self._tail = np.zeros(taps - 1, dtype=np.float32)
        self._phase = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.factor == 1:
            return np.asarray(x, dtype=np.float32)
        buf = np.concatenate([self._tail, np.asarray(x, dtype=np.float32)])
        y = np.convolve(buf, self.h, mode="valid")
        self._tail = buf[len(buf) - (len(self.h) - 1):]
        out = y[self._phase::self.factor]
        self._phase = (self._phase - len(y)) % self.factor
        return out


@dataclass
class VadEvent:
    kind: str          # "start" | "audio" | "end"
    audio: np.ndarray  # "start": pre-roll + onset; "audio": speech since last event


_EMPTY = np.zeros(0, dtype=np.float32)


class EnergyVAD:
    """Adaptive-threshold speech detector with hangover and pre-roll.

    ``feed`` returns events in order: ``start`` (carrying the audio just
    before and at onset), ``audio`` chunks while speech continues, and
    ``end`` after ``end_ms`` of quiet. The threshold follows the background
    noise floor, so it copes with fans and hum without tuning.
    """

    def __init__(self, sr: int, frame_ms: int = 20, start_ratio: float = 3.0, min_level: float = 0.006,
                 start_ms: int = 60, end_ms: int = 700, preroll_ms: int = 300):
        self.sr = sr
        self.frame_ms = frame_ms
        self.frame = sr * frame_ms // 1000
        self.start_ratio = start_ratio
        self.min_level = min_level
        self.start_frames = max(1, start_ms // frame_ms)
        self.end_frames = max(1, end_ms // frame_ms)
        self.preroll = sr * preroll_ms // 1000
        self.noise = min_level / 2
        self.level = 0.0
        self.speaking = False
        self._above = 0
        self._below = 0
        self._pending = _EMPTY
        self._history = _EMPTY

    def set_end_ms(self, ms: int) -> None:
        self.end_frames = max(1, ms // self.frame_ms)

    def feed(self, x: np.ndarray) -> list[VadEvent]:
        events: list[VadEvent] = []
        chunk: list[np.ndarray] = []
        self._pending = np.concatenate([self._pending, np.asarray(x, dtype=np.float32)])
        while len(self._pending) >= self.frame:
            f, self._pending = self._pending[:self.frame], self._pending[self.frame:]
            rms = float(np.sqrt(np.mean(f * f)) + 1e-9)
            self.level = rms
            threshold = max(self.min_level, self.noise * self.start_ratio)
            if not self.speaking:
                # noise floor tracks quiet frames quickly, loud ones slowly
                rate = 0.05 if rms < self.noise * 2 else 0.002
                self.noise += (rms - self.noise) * rate
                self._history = np.concatenate([self._history, f])[-self.preroll:]
                self._above = self._above + 1 if rms > threshold else 0
                if self._above >= self.start_frames:
                    self.speaking = True
                    self._below = 0
                    events.append(VadEvent("start", self._history))
                    self._history = _EMPTY
                continue
            chunk.append(f)
            # stay in speech while above ~half the start threshold
            self._below = 0 if rms > threshold * 0.5 else self._below + 1
            if self._below >= self.end_frames:
                events.append(VadEvent("audio", np.concatenate(chunk)))
                chunk = []
                self.speaking = False
                self._above = 0
                events.append(VadEvent("end", _EMPTY))
        if chunk:
            events.append(VadEvent("audio", np.concatenate(chunk)))
        return events
