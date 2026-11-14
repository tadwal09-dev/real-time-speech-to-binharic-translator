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

import random
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


def _grid(spec: ModeSpec, sr: int, count: int) -> np.ndarray:
    """Symbol boundaries in samples, on an exact time grid (k * step seconds,
    rounded once). Sender and receiver share it, so they agree at any sample
    rate and speed - no rounding drift over long messages."""
    return np.round(np.arange(count + 1) * (spec.symbol + spec.guard) * sr).astype(np.int64)


def _steady(spec: ModeSpec, sr: int) -> int:
    return int(round(spec.symbol * sr))


def _beeps(symbols: list[int], spec: ModeSpec, sr: int) -> np.ndarray:
    if not symbols:
        return np.zeros(0)
    b = _grid(spec, sr, len(symbols))
    n_sym = _steady(spec, sr)
    t = np.arange(n_sym) / sr
    env = _fade(n_sym, int(0.004 * sr))
    out = np.zeros(b[-1])
    for k, sym in enumerate(symbols):  # tone first, then the gap
        out[b[k]:b[k] + n_sym] = np.sin(2 * np.pi * spec.tones[sym] * t) * env
    return out


def _screech(symbols: list[int], spec: ModeSpec, sr: int, rng: np.random.Generator) -> np.ndarray:
    if not symbols:
        return np.zeros(0)
    b = _grid(spec, sr, len(symbols))
    n_sym = _steady(spec, sr)
    freq = np.empty(b[-1])
    env = np.empty(b[-1])
    steady_env = _fade(n_sym, int(0.003 * sr)) * 0.45 + 0.55
    t_sym = np.arange(n_sym) / sr
    prev = spec.tones[symbols[0]]
    for k, sym in enumerate(symbols):  # glide first (the "scrape"), then the warbling tone
        f = spec.tones[sym]
        n_guard = b[k + 1] - b[k] - n_sym
        g0, g1 = b[k], b[k] + n_guard
        freq[g0:g1] = prev + (f - prev) * np.linspace(0, 1, n_guard) ** 0.5
        env[g0:g1] = 0.55  # dip the level during glides so the steady part dominates
        rate = rng.uniform(18, 32)
        freq[g1:b[k + 1]] = f + 14.0 * np.sin(2 * np.pi * rate * t_sym + rng.uniform(0, 2 * np.pi))
        env[g1:b[k + 1]] = steady_env
        prev = f
    phase = 2 * np.pi * np.cumsum(freq) / sr
    sig = np.sin(phase) + 0.30 * np.sin(2 * phase) + 0.12 * np.sin(3 * phase + 0.7)
    # buzzy amplitude modulation + a little grit
    t = np.arange(len(sig)) / sr
    sig *= 1.0 + 0.45 * np.sign(np.sin(2 * np.pi * 67 * t)) * 0.5
    sig += 0.05 * rng.standard_normal(len(sig))
    return np.tanh(1.4 * sig) * env


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


MAX_DECOYS = 8


def frames_with_decoys(text: str, mode_id: int, key: str | None, decoys: int = 0) -> list[tuple[bool, bytes]]:
    """[(is_real, frame)]: the real frame hidden at a random place among
    ``decoys`` gibberish frames of similar (random) length. Needs a key."""
    real = codec.build_frame(text, mode_id, key)
    if not key or decoys <= 0:
        return [(True, real)]
    rng = random.SystemRandom()
    n = len(text.encode("utf-8"))
    out = [(False, codec.build_decoy(mode_id, max(1, int(n * rng.uniform(0.5, 1.6)) + rng.randint(0, 6))))
           for _ in range(min(decoys, MAX_DECOYS))]
    out.insert(rng.randint(0, len(out)), (True, real))
    return out


def encode_text(text: str, mode: str = "binharic", key: str | None = None, sr: int = DEFAULT_SR,
                speed: float = 1.0, volume: float = 0.8, decoys: int = 0) -> np.ndarray:
    """Text -> audio. Long text becomes several frames back to back (each
    with its own sync chirp), so nothing is ever cut off. With a key,
    ``decoys`` gibberish frames per chunk hide which chirp is the real one."""
    spec = get_mode(mode, speed)
    chunks = codec.split_text(text) or [""]
    return np.concatenate([modulate(frame, spec, sr, volume)
                           for c in chunks for _, frame in frames_with_decoys(c, spec.id, key, decoys)])


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


class _Demod:
    """Reads the symbols of one frame, following the sender's timing.

    The receiver's clock is never the sender's. Sound cards drift apart, and
    a voice call does worse: its jitter buffer cuts out or repeats a few
    milliseconds of sound several times a second, and a lost packet is
    papered over with a guess. So the tones are not looked for on a fixed
    grid. The strength of every tone is measured every millisecond, and the best
    path through those measurements is found (Viterbi): one position per
    symbol, each a symbol's length after the one before, give or take a
    slip that costs a little. A cut or a repeat then costs at most the tone
    it landed on, and the error correction in the frame repairs that.
    """

    HOP = 0.001         # seconds between measurements
    SLIP_COST = 2.6     # for slipping a whole symbol's length, in units of a tone's usual strength:
                        # high enough that reading everything one symbol early or late never pays
    MAX_SLIP = 0.020    # seconds one symbol may slip against the one before
    LEAD = 0.020        # seconds the first symbol may be off (a chirp found a little early or late); it
                        # costs like any slip: the chirp is the only thing that says where symbol 0 is

    def __init__(self, sig: np.ndarray, sr: int, start: int, spec: ModeSpec):
        self.sig, self.sr, self.start, self.spec = sig, sr, start, spec
        self.n_sym = _steady(spec, sr)
        self.hop = max(1, int(round(self.HOP * sr)))
        step = (spec.symbol + spec.guard) * sr
        self.max_slip = max(1, int(min(self.MAX_SLIP * sr, 0.45 * step) / self.hop))
        self.lead = max(1, int(min(self.LEAD * sr, 0.4 * step) / self.hop))
        self.slip_cost = self.SLIP_COST * self.hop / step  # per hop
        window = np.hanning(self.n_sym)
        t = np.arange(self.n_sym) / sr
        arg = 2 * np.pi * np.outer(t, np.asarray(spec.tones))
        self.basis = np.concatenate([np.cos(arg), np.sin(arg)], axis=1).astype(np.float32) * \
            window[:, None].astype(np.float32)  # (window, 2 * tones)
        self._grid = _grid(spec, sr, 64)
        self._track = np.zeros((0, len(spec.tones)), np.float32)  # tone strengths, one row per hop
        self.ends: list[int] = []  # sample just after each symbol read so far

    def _nominal(self, k: int) -> int:
        """Where symbol ``k``'s steady tone starts if nothing slipped, in samples after ``start``."""
        if k + 1 >= len(self._grid):
            self._grid = _grid(self.spec, self.sr, max(2 * len(self._grid), k + 2))
        # screech: glide first, then the tone
        return int(self._grid[k + 1]) - self.n_sym if self.spec.screech else int(self._grid[k])

    def need(self, count: int) -> int:
        """Samples of signal wanted before reading ``count`` symbols."""
        return self.start + self._nominal(count - 1) + self.n_sym + self.hop

    def _strengths(self, hops: int) -> np.ndarray:
        """Tone strengths for measurement rows 0..hops-1. Row j looks at the samples from
        ``start + (j - reach) * hop``; beyond the signal's ends it reads silence."""
        have = len(self._track)
        if hops > have:
            reach = self._reach
            rows = []
            for lo in range(have, hops, 2048):  # in pieces: the windows of a long frame are many MB
                hi = min(hops, lo + 2048)
                first = self.start + (lo - reach) * self.hop
                last = self.start + (hi - 1 - reach) * self.hop + self.n_sym
                a, b = max(first, 0), min(last, len(self.sig))
                seg = np.zeros(last - first, np.float32)
                if b > a:
                    seg[a - first:b - first] = self.sig[a:b]
                frames = np.lib.stride_tricks.sliding_window_view(seg, self.n_sym)[::self.hop]
                proj = frames @ self.basis
                n = len(self.spec.tones)
                rows.append(np.hypot(proj[:, :n], proj[:, n:]))
            self._track = np.concatenate([self._track, *rows])
        return self._track

    @property
    def _reach(self) -> int:
        return self.lead + 2

    def read(self, count: int) -> tuple[list[int], list[float]]:
        """The first ``count`` symbols and how doubtful each is (0 = certain, 1 = a coin toss)."""
        hop, reach = self.hop, self._reach
        centre = [int(round(self._nominal(k) / hop)) + reach for k in range(count)]
        # how far the path may stray from the nominal grid: the lead, plus room for drift
        # and slips that grows with the message
        band = self.lead + int(0.02 * (centre[-1] - centre[0])) + 4 * self.max_slip
        track = self._strengths(centre[-1] + band + 1)
        peak = track.max(axis=1)
        width = 2 * band + 1
        offs = np.arange(-band, band + 1)
        score = np.empty((count, width), np.float32)
        for k, c in enumerate(centre):
            idx = np.clip(c + offs, 0, len(peak) - 1)
            score[k] = peak[idx]
        usual = float(np.median(score.max(axis=1))) or 1.0
        score = np.minimum(score / usual, 1.5)
        slips = np.arange(-self.max_slip, self.max_slip + 1)
        cost = (self.slip_cost * np.abs(slips)).astype(np.float32)
        pad = np.full(self.max_slip, -np.inf, np.float32)
        best = np.where(np.abs(offs) > self.lead, -np.inf, score[0] - self.slip_cost * np.abs(offs)).astype(np.float32)
        back = np.zeros((count, width), np.int16)
        for k in range(1, count):
            prev = np.lib.stride_tricks.sliding_window_view(np.concatenate([pad, best, pad]), len(slips)) - cost
            pick = prev.argmax(axis=1)
            back[k] = pick
            best = prev[np.arange(width), pick] + score[k]
        at = int(best.argmax())
        path = [0] * count
        for k in range(count - 1, -1, -1):
            path[k] = at
            at += int(back[k][at]) - self.max_slip if k else 0
        symbols, doubt, self.ends = [], [], []
        for k in range(count):
            row = track[min(centre[k] + path[k] - band, len(track) - 1)]
            order = np.argsort(row)
            top, second = float(row[order[-1]]), float(row[order[-2]])
            symbols.append(int(order[-1]))
            # unsure when another tone is nearly as strong, or when there's hardly any tone at all
            doubt.append(max(second / top if top > 0 else 1.0, 1.0 - min(top / usual, 1.0)))
            self.ends.append(self.start + (centre[k] + path[k] - band - reach) * hop + self.n_sym)
        return symbols, doubt


def looks_like_screech(block: np.ndarray, sr: int = DEFAULT_SR) -> bool:
    """Is this short block of sound (tens of ms) mostly one or two tones in the screech's
    band? Any mode's data tones are; speech rarely is. Used to tell our own screech,
    coming back late through speakers and a mic, from someone talking."""
    n = len(block)
    if n < 256:
        return False
    x = np.asarray(block, np.float64)
    spec = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(n))) ** 2
    total = float(spec[1:].sum())
    if total < 1e-7 * n:
        return False  # silence
    hz = sr / n
    lo, hi, width = int(600 / hz), int(3500 / hz), max(1, int(round(110 / hz)))
    tones = 0.0
    for _ in range(2):  # a block often straddles two symbols
        k = lo + int(np.argmax(spec[lo:hi]))
        a, b = max(k - width, 1), k + width + 1
        tones += float(spec[a:b].sum())
        spec[a:b] = 0
    return tones / total > 0.6


def data_start(pre_at: int, sr: int) -> int:
    return pre_at + int(PREAMBLE_SECONDS * sr) + int(PREAMBLE_GAP * sr)


SLACK = 0.06  # seconds of sound wanted past a frame's nominal end: a call may have stretched it


def try_decode_at(sig: np.ndarray, sr: int, pre_at: int, spec: ModeSpec, key: str | None = None,
                  final: bool = False):
    """Decode a frame whose preamble starts at ``pre_at``.

    Returns (Message, end_sample). Raises codec.NeedMore with ``needed`` in
    *samples* (absolute), or codec.BadFrame. ``final``: no more sound is
    coming, so read what there is.
    """
    d = _Demod(sig, sr, data_start(pre_at, sr), spec)
    per = spec.symbols_per_byte
    slack = int(SLACK * sr)
    want = codec.HEADER_LEN
    while True:
        count = want * per
        # the header is read as soon as it's there; the whole frame with room for stretching
        need = d.need(count) + (slack if want > codec.HEADER_LEN else 0)
        if need > len(sig) and not final:
            raise codec.NeedMore(need)
        if d.need(count) - d.n_sym > len(sig):
            raise codec.NeedMore(need)  # final, and it isn't all there: it was cut off
        symbols, doubt = d.read(count)
        data = symbols_to_bytes(symbols, spec.bits)
        byte_doubt = [max(doubt[i * per:(i + 1) * per]) for i in range(len(data))]
        try:
            msg, used = codec.parse_frame(data, key, spec.id, byte_doubt)
        except codec.NeedMore as e:
            want = e.needed
            continue
        end = d.ends[used * per - 1]
        if end > len(sig) and not final:
            raise codec.NeedMore(end + slack)  # stretched past what we have: its last tones aren't here yet
        return msg, end


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
                msg, end = try_decode_at(sig, sr, p, spec, key, final=True)
            except (codec.NeedMore, codec.BadFrame):
                continue
            out.append(msg)
            break
    return out


class StreamDecoder:
    """Incremental decoder for live audio (microphone / monitor source).

    Keeps a list of candidate frames (where a sync chirp was heard). A
    candidate that is still arriving never blocks the ones after it: if a
    transmission is cut off half way (someone switched off mid-screech),
    the next complete message is still decoded straight away.
    """

    def __init__(self, sr: int = DEFAULT_SR, mode: str = "auto", key: str | None = None,
                 speed: float = 1.0, threshold: float = 0.45, max_buffer_seconds: float = 120.0):
        self.sr = sr
        self.key = key
        self.threshold = threshold
        self.specs = [get_mode(m, speed) for m in (MODES if mode == "auto" else [mode])]
        self.buf = np.zeros(0, dtype=np.float32)
        self.base = 0                 # absolute sample index of buf[0]
        self.max_buffer = int(max_buffer_seconds * sr)
        self._pre_len = int(PREAMBLE_SECONDS * sr)
        self._searched = 0            # absolute index up to which chirps were searched
        self._cands: dict[int, int] = {}  # absolute chirp position -> samples needed before retrying
        # screeches heard but not readable: the header was right for its mode, the checksum
        # wasn't (noise suppression, a bad connection). Random noise almost never gets that far
        self.garbled = 0

    def feed(self, samples: np.ndarray) -> list[codec.Message]:
        self.buf = np.concatenate([self.buf, np.asarray(samples, dtype=np.float32).ravel()])
        if len(self.buf) > self.max_buffer:
            self._drop(len(self.buf) - self.max_buffer)
        if self.base + len(self.buf) - self._searched < self.sr // 4:
            return self._try(final=False)  # not enough new audio to look for chirps yet
        self._search()
        return self._try(final=False)

    def flush(self) -> list[codec.Message]:
        """Decode whatever is buffered now (call when the input pauses or ends)."""
        if not len(self.buf):
            return []
        self._search()
        return self._try(final=True)

    # -- internals -------------------------------------------------------
    def _drop(self, n: int) -> None:
        n = min(n, len(self.buf))
        self.buf = self.buf[n:]
        self.base += n
        self._cands = {p: need for p, need in self._cands.items() if p >= self.base}
        self._searched = max(self._searched, self.base)

    def _search(self) -> None:
        # search only the new audio, with a chirp's length of overlap so one
        # straddling the boundary is still found
        lo = max(self.base, self._searched - self._pre_len)
        for p in find_preambles(self.buf[lo - self.base:], self.sr, self.threshold):
            p += lo
            if all(abs(p - q) >= self._pre_len for q in self._cands):
                self._cands[p] = 0
        self._searched = self.base + len(self.buf)

    def _try(self, final: bool) -> list[codec.Message]:
        out: list[codec.Message] = []
        end = self.base + len(self.buf)
        for p in sorted(self._cands):
            if p not in self._cands or (self._cands[p] > end and not final):
                continue  # still arriving
            waiting = None
            broken = False
            for spec in self.specs:
                try:
                    msg, stop = try_decode_at(self.buf, self.sr, p - self.base, spec, self.key, final)
                except codec.NeedMore as e:
                    need = self.base + e.needed
                    waiting = need if waiting is None else min(waiting, need)
                    continue
                except codec.BadFrame as e:
                    broken = broken or str(e) == "crc mismatch"
                    continue
                out.append(msg)
                stop += self.base
                # everything before the end of this frame is settled
                self._cands = {q: n for q, n in self._cands.items() if q >= stop}
                break
            else:
                if waiting is None or final:
                    self._cands.pop(p, None)  # not a frame after all (or it never finished)
                    self.garbled += broken
                else:
                    self._cands[p] = waiting
        # keep audio from the oldest open candidate, or a chirp's worth of tail;
        # never anything not yet searched for chirps (it would be lost unseen)
        keep_from = min(self._cands) if self._cands else end - 2 * self._pre_len
        keep_from = min(keep_from, self._searched - self._pre_len)
        if keep_from > self.base:
            self._drop(keep_from - self.base)
        return out
