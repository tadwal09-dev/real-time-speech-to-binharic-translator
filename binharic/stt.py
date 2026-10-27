"""Speech-to-text engines and the utterance pipeline.

Engines:

* ``whisper`` - faster-whisper (OpenAI Whisper, CTranslate2). Much more
  accurate, handles odd words and accents, runs offline on CPU or GPU.
* ``vosk``    - lighter and lower-latency, less accurate.

Both are driven the same way: the VAD cuts the mic into utterances, and
``Transcriber`` turns each utterance into either one final phrase
("after I finish") or a stream of committed words ("as I talk"). Live
words are committed once two consecutive hypotheses agree on them
(LocalAgreement), so the screech doesn't send words the recogniser later
changes its mind about.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .vad import EnergyVAD

STT_SR = 16000
PHRASE, LIVE = "phrase", "live"

WHISPER_MODELS = ["tiny.en", "base.en", "small.en", "medium.en", "large-v3", "distil-large-v3"]
DEFAULT_WHISPER = "small.en"
DEFAULT_WHISPER_LIVE = "base.en"  # quick interim passes; the final pass uses the main model
DEFAULT_VOSK = "vosk-model-small-en-us-0.15"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "binharic"


def _short(e: Exception) -> str:
    return str(e).splitlines()[0][:120] if str(e) else type(e).__name__


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# engines

class Engine:
    """Per-utterance recogniser: ``begin`` / ``feed`` -> hypothesis / ``finish``."""

    name = "engine"
    label = "engine"
    live_interval = 0.0  # seconds of new audio between live hypotheses

    def begin(self) -> None: ...
    def feed(self, audio: np.ndarray, want_hypothesis: bool) -> str | None: ...
    def finish(self, fast: bool = False) -> str: ...

    def peek(self, fast: bool = False) -> str | None:
        """Transcribe what we have *without* ending the utterance (None = unsupported)."""
        return None

    def discard(self) -> None:
        self.finish()


class WhisperEngine(Engine):
    name = "whisper"

    def __init__(self, model: str = DEFAULT_WHISPER, device: str = "auto", compute_type: str = "auto",
                 language: str | None = "en", beam_size: int = 5, live_interval: float = 0.8,
                 live_model: str | None = DEFAULT_WHISPER_LIVE):
        from . import gpu

        if model.endswith(".en"):
            language = "en"
        self.language = language
        self.beam_size = beam_size
        self.live_interval = live_interval
        self.prompt = ""
        self.notice = ""  # something the user should know (e.g. GPU fallback)
        self._buf: list[np.ndarray] = []
        self._since = 0
        self._cost = 0.0  # smoothed seconds per interim pass

        if device == "auto":
            devices = ["cuda", "cpu"] if gpu.cuda_devices() > 0 else ["cpu"]
        else:
            devices = [device]
        if "cuda" in devices:
            gpu.preload_cuda_libs()
        last_error = None
        for dev in devices:
            try:
                self._load(model, live_model, dev, compute_type)
                break
            except (RuntimeError, OSError, ValueError) as e:
                last_error = e
                if dev == "cuda" and len(devices) > 1:
                    msg = str(e).lower()
                    if "driver" in msg:
                        fix = "update the NVIDIA driver (sudo pacman -Syu) and reboot"
                    else:
                        fix = "rerun ./install.sh to install the CUDA 12 libraries"
                    self.notice = (f"NVIDIA GPU found but it can't be used ({_short(e)}); "
                                   f"running on the CPU instead. To use the GPU: {fix}.")
                    continue
                raise
        else:  # pragma: no cover - loop always breaks or raises
            raise last_error
        self.device = dev
        name = model if self.fast is self.model else f"{model} + {live_model} live"
        self.label = f"{name} · {'GPU' if dev == 'cuda' else 'CPU'}"

    def _load(self, model: str, live_model: str | None, device: str, compute_type: str) -> None:
        from faster_whisper import WhisperModel

        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        # all cores but one (CTranslate2 defaults to 4): the spare one keeps audio glitch-free
        threads = max(1, min((os.cpu_count() or 4) - 1, 16))

        def load(name):
            return WhisperModel(name, device=device, compute_type=compute_type, cpu_threads=threads,
                                download_root=str(data_dir() / "whisper"))

        self.model = load(model)
        small = model.split(".")[0] in ("tiny", "base")
        self.fast = self.model if small or not live_model or live_model == model else load(live_model)
        # the first inference is several times slower, and on a GPU it is where missing
        # CUDA libraries show up, so do it now
        warm = np.zeros(STT_SR, dtype=np.float32)
        for m in {id(self.model): self.model, id(self.fast): self.fast}.values():
            list(m.transcribe(warm, language=self.language, beam_size=1, max_new_tokens=4,
                              without_timestamps=True)[0])

    def _run(self, audio: np.ndarray, beam: int, fast: bool = False) -> str:
        segments, _ = (self.fast if fast else self.model).transcribe(
            audio, language=self.language, beam_size=beam, vad_filter=False,
            condition_on_previous_text=False, initial_prompt=self.prompt or None,
            without_timestamps=True)
        # drop what Whisper invents over breath/keyboard noise ("Thank you.")
        kept = [s.text for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -0.8)]
        return _clean(" ".join(kept))

    def begin(self) -> None:
        self._buf, self._since = [], 0

    def feed(self, audio, want_hypothesis):
        self._buf.append(audio)
        self._since += len(audio)
        # wait longer between passes when the CPU is struggling, so we never fall behind
        interval = min(3.0, max(self.live_interval, self._cost * 1.5))
        total = sum(len(b) for b in self._buf)
        if not want_hypothesis or self._since < interval * STT_SR or total < STT_SR:
            return None
        self._since = 0
        t = time.monotonic()
        text = self._run(np.concatenate(self._buf), beam=1, fast=True)
        cost = time.monotonic() - t
        self._cost = cost if not self._cost else 0.6 * self._cost + 0.4 * cost
        return text

    def finish(self, fast: bool = False) -> str:
        text = self.peek(fast) or ""
        self._buf = []
        return text

    def peek(self, fast: bool = False) -> str | None:
        if not self._buf:
            return ""
        audio = np.concatenate(self._buf)
        if len(audio) < STT_SR * 0.25:
            return ""
        return self._run(audio, beam=self.beam_size, fast=fast)

    def discard(self) -> None:
        self._buf = []


class VoskEngine(Engine):
    name = "vosk"
    live_interval = 0.0
    label = "vosk"

    def __init__(self, model_path: str | None = None):
        import vosk

        vosk.SetLogLevel(-1)
        path = Path(model_path).expanduser() if model_path else _find_vosk_model()
        self.model = vosk.Model(str(path))
        self._vosk = vosk
        self.rec = None
        self._done = ""

    def begin(self) -> None:
        self.rec = self._vosk.KaldiRecognizer(self.model, STT_SR)
        self._done = ""

    def feed(self, audio, want_hypothesis):
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        if self.rec.AcceptWaveform(pcm):
            self._done = _clean(self._done + " " + json.loads(self.rec.Result()).get("text", ""))
            partial = ""
        else:
            partial = json.loads(self.rec.PartialResult()).get("partial", "")
        return _clean(self._done + " " + partial)

    def finish(self, fast: bool = False) -> str:
        if self.rec is None:
            return ""
        text = _clean(self._done + " " + json.loads(self.rec.FinalResult()).get("text", ""))
        self.rec = None
        return text


def _find_vosk_model() -> Path:
    models = data_dir() / "models"
    found = sorted(models.glob("vosk-model*")) if models.exists() else []
    if not found:
        raise RuntimeError("no Vosk model found - run `binharic download-model --engine vosk`")
    return found[0]


def have_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def make_engine(name: str = "auto", model: str | None = None, device: str = "auto",
                live_model: str | None = DEFAULT_WHISPER_LIVE, beam: int = 5) -> Engine:
    if name == "auto":
        name = "whisper" if have_whisper() else "vosk"
    if name == "whisper":
        return WhisperEngine(model or DEFAULT_WHISPER, device=device, live_model=live_model, beam_size=beam)
    if name == "vosk":
        return VoskEngine(model)
    raise ValueError(f"unknown engine {name!r}")


def download(engine: str = "whisper", model: str | None = None) -> str:
    if engine == "whisper":
        from faster_whisper.utils import download_model

        names = [model] if model else [DEFAULT_WHISPER, DEFAULT_WHISPER_LIVE]
        return "\n".join(download_model(n, cache_dir=str(data_dir() / "whisper")) for n in names)
    import urllib.request
    import zipfile

    name = model or DEFAULT_VOSK
    dest = data_dir() / "models"
    target = dest / name
    if target.exists():
        return str(target)
    dest.mkdir(parents=True, exist_ok=True)
    zpath = dest / f"{name}.zip"
    urllib.request.urlretrieve(f"https://alphacephei.com/vosk/models/{name}.zip", zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest)
    zpath.unlink()
    return str(target)


# --------------------------------------------------------------------------
# utterance pipeline

def _words(text: str) -> list[str]:
    return text.split()


def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def agreed_prefix(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if _norm(x) != _norm(y):
            break
        n += 1
    return n


@dataclass
class Transcriber:
    """Glue between VAD, an Engine, and whoever wants the text.

    Callbacks:
      on_speech(bool)      - speech started / stopped (drives muting + UI)
      on_partial(str)      - current best guess for the utterance
      on_send(str, final)  - text to screech now
    """

    engine: Engine
    send_mode: str = PHRASE
    on_speech: Callable[[bool], None] = lambda s: None
    on_partial: Callable[[str], None] = lambda t: None
    on_send: Callable[[str, bool], None] = lambda t, f: None
    end_ms: int = 700
    partials: bool = True  # interim transcripts in phrase mode (display only; costs latency on CPU)
    max_utterance_s: float = 25.0  # Whisper sees at most 30 s at once
    vad: EnergyVAD = field(init=False)

    def __post_init__(self):
        self.vad = EnergyVAD(STT_SR, end_ms=self.end_ms)
        self._prev: list[str] = []
        self._committed = 0
        self._last_hyp = ""
        self._speech_started = 0.0
        self._paused = False
        self._spec: str | None = None   # speculative final made during a pause
        self._spec_ms = 0.0
        self.last_stt_ms = 0.0          # recognition time the last phrase waited on
        self.last_speculative = False

    @property
    def level(self) -> float:
        return self.vad.level

    @property
    def speaking(self) -> bool:
        return self.vad.speaking

    def reset(self) -> None:
        if self.vad.speaking:
            self.engine.discard()
            self.on_speech(False)
        self._spec, self._paused = None, False
        self.vad = EnergyVAD(STT_SR, end_ms=self.end_ms)

    def feed(self, audio16: np.ndarray) -> None:
        for ev in self.vad.feed(audio16):
            if ev.kind == "start":
                self._prev, self._committed, self._last_hyp = [], 0, ""
                self._spec, self._paused = None, False
                self._speech_started = time.monotonic()
                self.engine.begin()
                self.on_speech(True)
                self._push(ev.audio)
            elif ev.kind == "audio":
                self._push(ev.audio)
                if time.monotonic() - self._speech_started > self.max_utterance_s:
                    # very long monologue: flush it and keep going as a new utterance
                    self._end(still_speaking=True)
                    self._speech_started = time.monotonic()
                    self.engine.begin()
            elif ev.kind == "pause":
                # you may be done: start the final pass now, while the VAD
                # waits out the rest of the pause
                self._paused = True
                t = time.monotonic()
                self._spec = self.engine.peek(fast=self.send_mode == LIVE)
                self._spec_ms = (time.monotonic() - t) * 1000
            elif ev.kind == "resume":
                self._paused, self._spec = False, None
            else:
                self._end()

    def _push(self, audio: np.ndarray) -> None:
        want = not self._paused and (self.send_mode == LIVE or self.partials)
        hyp = self.engine.feed(audio, want_hypothesis=want)
        if hyp is None:
            return
        if hyp != self._last_hyp:
            self._last_hyp = hyp
            self.on_partial(hyp)
        if self.send_mode != LIVE:
            return
        words = _words(hyp)
        stable = agreed_prefix(self._prev, words)
        self._prev = words
        if stable > self._committed:
            self.on_send(" ".join(words[self._committed:stable]), False)
            self._committed = stable

    def _end(self, still_speaking: bool = False) -> None:
        if not still_speaking:
            self.on_speech(False)
        if self._spec is not None and not still_speaking:
            # nothing but silence since the speculative pass: it is the answer
            text = self._spec
            self.engine.discard()
            self.last_stt_ms, self.last_speculative = self._spec_ms, True
        else:
            # live mode already sent most words: finish quickly with the fast model
            t = time.monotonic()
            text = self.engine.finish(fast=self.send_mode == LIVE)
            self.last_stt_ms, self.last_speculative = (time.monotonic() - t) * 1000, False
        self._spec, self._paused = None, False
        words = _words(text)
        if self.send_mode == LIVE:
            # the final, more careful pass may reword things; send only what
            # comes after the words already screeched
            rest = words[self._committed:]
            if rest:
                self.on_send(" ".join(rest), True)
        elif words:
            self.on_send(" ".join(words), True)
        self.on_partial("")
        self._prev, self._committed, self._last_hyp = [], 0, ""
