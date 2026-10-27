"""Audio modem: turns frames into beeps/screeches and back.

Every transmission starts with a rising chirp (the "sync screech") that the
decoder locks onto, followed by one tone per symbol:

* ``binary``   - 1 bit per symbol, two tones (classic 0/1 beeps)
* ``hex``      - 4 bits per symbol, 16 tones (one per hex digit)
* ``binharic`` - like hex, but each tone warbles, buzzes and glides into the
                 next one so it sounds like Mechanicus scrapcode. The decoder
                 only looks at the centre of each tone, so it stays readable.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import codec

DEFAULT_SR = 48000

PREAMBLE_SECONDS = 0.15
PREAMBLE_F0, PREAMBLE_F1 = 500.0, 3500.0
PREAMBLE_GAP = 0.03


@dataclass(frozen=True)
class ModeSpec:
    name: str
    id: int
    bits: int
    tones: tuple[float, ...]
    symbol: float  # seconds of steady tone
    guard: float   # seconds between tones
    screech: bool = False

    def scaled(self, speed: float) -> "ModeSpec":
        return ModeSpec(self.name, self.id, self.bits, self.tones,
                        self.symbol / speed, self.guard / speed, self.screech)

    @property
    def symbols_per_byte(self) -> int:
        return 8 // self.bits


_HEX_TONES = tuple(800.0 + 150.0 * i for i in range(16))

MODES: dict[str, ModeSpec] = {
    "binary": ModeSpec("binary", 0, 1, (1200.0, 2200.0), 0.025, 0.006),
    "hex": ModeSpec("hex", 1, 4, _HEX_TONES, 0.045, 0.012),
    "binharic": ModeSpec("binharic", 2, 4, _HEX_TONES, 0.035, 0.015, screech=True),
}
MODES_BY_ID = {m.id: m for m in MODES.values()}


def get_mode(name: str, speed: float = 1.0) -> ModeSpec:
    try:
        return MODES[name].scaled(speed)
    except KeyError:
        raise ValueError(f"unknown mode {name!r}; choose from {', '.join(MODES)}") from None


# --------------------------------------------------------------------------
# symbols

def bytes_to_symbols(data: bytes, bits: int) -> list[int]:
    mask = (1 << bits) - 1
    return [(b >> shift) & mask for b in data for shift in range(8 - bits, -1, -bits)]


def symbols_to_bytes(symbols: list[int], bits: int) -> bytes:
    per = 8 // bits
    out = bytearray()
    for i in range(0, len(symbols) - len(symbols) % per, per):
        b = 0
        for s in symbols[i:i + per]:
            b = (b << bits) | s
        out.append(b)
    return bytes(out)


# --------------------------------------------------------------------------
# synthesis

def preamble(sr: int) -> np.ndarray:
    n = int(PREAMBLE_SECONDS * sr)
    t = np.arange(n) / sr
    k = (PREAMBLE_F1 - PREAMBLE_F0) / PREAMBLE_SECONDS
    sig = np.sin(2 * np.pi * (PREAMBLE_F0 * t + 0.5 * k * t * t))
    return (sig * _fade(n, int(0.005 * sr))).astype(np.float32)


def _fade(n: int, ramp: int) -> np.ndarray:
    env = np.ones(n)
    ramp = min(ramp, n // 2)
    if ramp:
        r = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, ramp))
        env[:ramp] = r
        env[n - ramp:] = r[::-1]
    return env


def _beeps(symbols: list[int], spec: ModeSpec, sr: int) -> np.ndarray:
    n_sym, n_guard = int(spec.symbol * sr), int(spec.guard * sr)
    t = np.arange(n_sym) / sr
    env = _fade(n_sym, int(0.004 * sr))
    gap = np.zeros(n_guard)
    parts = []
    for s in symbols:
        parts.append(np.sin(2 * np.pi * spec.tones[s] * t) * env)
        parts.append(gap)
    return np.concatenate(parts) if parts else np.zeros(0)


def _screech(symbols: list[int], spec: ModeSpec, sr: int, rng: np.random.Generator) -> np.ndarray:
    n_sym, n_guard = int(spec.symbol * sr), int(spec.guard * sr)
    if not symbols:
        return np.zeros(0)
    freqs = []
    prev = spec.tones[symbols[0]]
    t_sym = np.arange(n_sym) / sr
    for s in symbols:
        f = spec.tones[s]
        # glide from the previous tone into this one (the "scrape")
        g = np.linspace(0, 1, n_guard) ** 0.5
        freqs.append(prev + (f - prev) * g)
        # warble around the steady tone
        rate = rng.uniform(18, 32)
        freqs.append(f + 14.0 * np.sin(2 * np.pi * rate * t_sym + rng.uniform(0, 2 * np.pi)))
        prev = f
    freq = np.concatenate(freqs)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    sig = np.sin(phase) + 0.30 * np.sin(2 * phase) + 0.12 * np.sin(3 * phase + 0.7)
    # buzzy amplitude modulation + a little grit
    t = np.arange(len(sig)) / sr
    sig *= 1.0 + 0.45 * np.sign(np.sin(2 * np.pi * 67 * t)) * 0.5
    sig += 0.05 * rng.standard_normal(len(sig))
    sig = np.tanh(1.4 * sig)
    # dip the level during glides so the steady part dominates
    env = np.concatenate([np.concatenate([np.full(n_guard, 0.55), _fade(n_sym, int(0.003 * sr)) * 0.45 + 0.55])
                          for _ in symbols])
    return sig * env


_LEAD_IN: dict[int, np.ndarray] = {}


def lead_in(sr: int = DEFAULT_SR, volume: float = 0.8) -> np.ndarray:
    """Sync chirp + gap. Identical for every message, so it can start playing
    before the rest of the message has been synthesised."""
    if sr not in _LEAD_IN:
        _LEAD_IN[sr] = np.concatenate([preamble(sr), np.zeros(int(PREAMBLE_GAP * sr), np.float32)])
    return _LEAD_IN[sr] * np.float32(volume)


def modulate_body(frame: bytes, spec: ModeSpec, sr: int = DEFAULT_SR, volume: float = 0.8,
                  seed: int | None = None) -> np.ndarray:
    """Everything after ``lead_in``: the data tones plus a short tail."""
    symbols = bytes_to_symbols(frame, spec.bits)
    if spec.screech:
        body = _screech(symbols, spec, sr, np.random.default_rng(seed))
    else:
        body = _beeps(symbols, spec, sr)
    peak = float(np.max(np.abs(body))) if len(body) else 1.0
    return np.concatenate([body / (peak or 1.0) * volume, np.zeros(int(0.05 * sr))]).astype(np.float32)


def modulate(frame: bytes, spec: ModeSpec, sr: int = DEFAULT_SR, volume: float = 0.8,
             seed: int | None = None) -> np.ndarray:
    return np.concatenate([lead_in(sr, volume), modulate_body(frame, spec, sr, volume, seed)])


def encode_text(text: str, mode: str = "binharic", key: str | None = None, sr: int = DEFAULT_SR,
                speed: float = 1.0, volume: float = 0.8) -> np.ndarray:
    spec = get_mode(mode, speed)
    return modulate(codec.build_frame(text, spec.id, key), spec, sr, volume)


def encode_body(text: str, mode: str = "binharic", key: str | None = None, sr: int = DEFAULT_SR,
                speed: float = 1.0, volume: float = 0.8) -> np.ndarray:
    spec = get_mode(mode, speed)
    return modulate_body(codec.build_frame(text, spec.id, key), spec, sr, volume)


# --------------------------------------------------------------------------
# demodulation

def find_preambles(sig: np.ndarray, sr: int, threshold: float = 0.45) -> list[int]:
    """Return sample offsets where a sync chirp starts, in order."""
    tpl = preamble(sr).astype(np.float64)
    m, n = len(tpl), len(sig)
    if n < m:
        return []
    x = sig.astype(np.float64)
    size = 1 << int(np.ceil(np.log2(n + m)))
    corr = np.fft.irfft(np.fft.rfft(x, size) * np.conj(np.fft.rfft(tpl, size)), size)[:n - m + 1]
    c2 = np.concatenate([[0.0], np.cumsum(x * x)])
    local = np.sqrt(np.maximum(c2[m:] - c2[:-m], 0.0))
    ncc = corr / (np.linalg.norm(tpl) * local + 1e-9)

    peaks = []
    above = np.flatnonzero(ncc > threshold)
    i = 0
    while i < len(above):
        # group consecutive-ish indices and keep the best one in each group
        j = i
        while j + 1 < len(above) and above[j + 1] - above[j] < m // 4:
            j += 1
        seg = above[i:j + 1]
        peaks.append(int(seg[np.argmax(ncc[seg])]))
        i = j + 1
    # drop peaks that overlap a stronger one
    peaks.sort(key=lambda p: -ncc[p])
    kept: list[int] = []
    for p in peaks:
        if all(abs(p - q) >= m for q in kept):
            kept.append(p)
    return sorted(kept)


def _demod_symbols(sig: np.ndarray, sr: int, start: int, spec: ModeSpec, first: int, count: int) -> list[int]:
    n_sym, n_guard = int(spec.symbol * sr), int(spec.guard * sr)
    step = n_sym + n_guard
    margin = int(n_sym * 0.15)
    win_len = n_sym - 2 * margin
    sym_off = n_guard if spec.screech else 0  # screech puts the glide before each tone
    window = np.hanning(win_len)
    t = np.arange(win_len) / sr
    basis = np.exp(-2j * np.pi * np.outer(np.asarray(spec.tones), t))  # (tones, win)
    out = []
    for k in range(first, first + count):
        s = start + k * step + sym_off + margin
        seg = sig[s:s + win_len]
        energies = np.abs(basis @ (seg * window))
        out.append(int(np.argmax(energies)))
    return out


def _symbols_available(n_samples: int, sr: int, start: int, spec: ModeSpec) -> int:
    n_sym, n_guard = int(spec.symbol * sr), int(spec.guard * sr)
    step = n_sym + n_guard
    return max(0, (n_samples - start) // step)


def data_start(pre_at: int, sr: int) -> int:
    return pre_at + int(PREAMBLE_SECONDS * sr) + int(PREAMBLE_GAP * sr)


def try_decode_at(sig: np.ndarray, sr: int, pre_at: int, spec: ModeSpec, key: str | None = None):
    """Decode a frame whose preamble starts at ``pre_at``.

    Returns (Message, end_sample). Raises codec.NeedMore with ``needed`` in
    *samples* (absolute), or codec.BadFrame.
    """
    start = data_start(pre_at, sr)
    step = int(spec.symbol * sr) + int(spec.guard * sr)
    avail = _symbols_available(len(sig), sr, start, spec)
    per = spec.symbols_per_byte
    have_bytes = 0
    data = b""
    want = 1
    while True:
        if want * per > avail:
            raise codec.NeedMore(start + want * per * step)
        syms = _demod_symbols(sig, sr, start, spec, have_bytes * per, (want - have_bytes) * per)
        data += symbols_to_bytes(syms, spec.bits)
        have_bytes = want
        try:
            msg, used = codec.parse_frame(data, key, spec.id)
            return msg, start + used * per * step
        except codec.NeedMore as e:
            want = e.needed


def decode_audio(sig: np.ndarray, sr: int = DEFAULT_SR, mode: str = "auto", key: str | None = None,
                 speed: float = 1.0, threshold: float = 0.45) -> list[codec.Message]:
    """Decode every complete transmission found in ``sig``."""
    specs = [get_mode(m, speed) for m in (MODES if mode == "auto" else [mode])]
    sig = np.asarray(sig, dtype=np.float32)
    out = []
    end = 0
    for p in find_preambles(sig, sr, threshold):
        if p < end:
            continue
        for spec in specs:
            try:
                msg, end = try_decode_at(sig, sr, p, spec, key)
            except (codec.NeedMore, codec.BadFrame):
                continue
            out.append(msg)
            break
    return out


class StreamDecoder:
    """Incremental decoder for live audio (microphone / monitor source)."""

    def __init__(self, sr: int = DEFAULT_SR, mode: str = "auto", key: str | None = None,
                 speed: float = 1.0, threshold: float = 0.45, max_buffer_seconds: float = 120.0):
        self.sr = sr
        self.key = key
        self.threshold = threshold
        self.specs = [get_mode(m, speed) for m in (MODES if mode == "auto" else [mode])]
        self.buf = np.zeros(0, dtype=np.float32)
        self.max_buffer = int(max_buffer_seconds * sr)
        self._pre_len = int(PREAMBLE_SECONDS * sr)
        self._last_scan = 0
        self._wait_until = 0  # buffer length needed before the next scan is worthwhile

    def feed(self, samples: np.ndarray) -> list[codec.Message]:
        self.buf = np.concatenate([self.buf, np.asarray(samples, dtype=np.float32).ravel()])
        if len(self.buf) > self.max_buffer:
            drop = len(self.buf) - self.max_buffer
            self.buf = self.buf[drop:]
            self._wait_until = max(0, self._wait_until - drop)
        if len(self.buf) < self._wait_until or len(self.buf) - self._last_scan < self.sr // 4:
            return []
        return self._scan()

    def flush(self) -> list[codec.Message]:
        """Scan whatever is buffered now (call when the input pauses or ends)."""
        return self._scan() if len(self.buf) else []

    def _scan(self) -> list[codec.Message]:
        out: list[codec.Message] = []
        while True:
            self._last_scan = len(self.buf)
            self._wait_until = 0
            peaks = find_preambles(self.buf, self.sr, self.threshold)
            progressed = False
            for p in peaks:
                waiting = None
                for spec in self.specs:
                    try:
                        msg, end = try_decode_at(self.buf, self.sr, p, spec, self.key)
                    except codec.NeedMore as e:
                        waiting = e.needed if waiting is None else min(waiting, e.needed)
                        continue
                    except codec.BadFrame:
                        continue
                    out.append(msg)
                    self.buf = self.buf[end:]
                    progressed = True
                    break
                if progressed:
                    break
                if waiting is not None:
                    # a plausible frame is still arriving: keep it, drop what's before
                    self.buf = self.buf[p:]
                    self._wait_until = waiting - p
                    self._last_scan = len(self.buf)
                    return out
            if not progressed:
                # nothing pending: keep only enough tail to catch a chirp that's cut off
                keep = self._pre_len * 2
                if len(self.buf) > keep:
                    self.buf = self.buf[-keep:]
                self._last_scan = len(self.buf)
                return out
