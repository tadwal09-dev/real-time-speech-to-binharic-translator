import time

import numpy as np
import pytest

from binharic import core, modem
from binharic.stt import LIVE, PHRASE, STT_SR, Engine, Transcriber, agreed_prefix
from binharic.vad import Decimator, EnergyVAD


def tone_burst(seconds, sr=STT_SR, amp=0.2):
    return (np.sin(2 * np.pi * 300 * np.arange(int(seconds * sr)) / sr) * amp).astype(np.float32)


def quiet(seconds, sr=STT_SR, rng=np.random.default_rng(0)):
    return (rng.standard_normal(int(seconds * sr)) * 0.001).astype(np.float32)


def feed_blocks(fn, sig, block):
    for i in range(0, len(sig), block):
        fn(sig[i:i + block])


def test_vad_detects_start_and_end_and_keeps_all_speech():
    vad = EnergyVAD(STT_SR, end_ms=500)
    sig = np.concatenate([quiet(1), tone_burst(2), quiet(1.5)])
    events = []
    feed_blocks(lambda b: events.extend(vad.feed(b)), sig, 1600)
    kinds = [e.kind for e in events if e.kind != "audio"]
    assert kinds == ["start", "pause", "end"]
    assert vad.since_voice >= 1.0 * STT_SR  # counts the quiet after you stopped
    speech = sum(len(e.audio) for e in events)
    assert 2.0 * STT_SR <= speech <= 3.0 * STT_SR  # pre-roll + speech + hangover


def test_vad_pause_then_resume_keeps_one_utterance():
    vad = EnergyVAD(STT_SR, end_ms=600, pause_ms=200)
    sig = np.concatenate([quiet(0.5), tone_burst(1), quiet(0.35), tone_burst(1), quiet(1)])
    events = []
    feed_blocks(lambda b: events.extend(vad.feed(b)), sig, 1600)
    assert [e.kind for e in events if e.kind != "audio"] == ["start", "pause", "resume", "pause", "end"]


def test_decimator_preserves_tone_and_rejects_alias():
    sr = 48000
    dec = Decimator(3)
    t = np.arange(sr) / sr
    keep = np.concatenate([dec(c) for c in np.array_split(np.sin(2 * np.pi * 1000 * t), 13)])
    alias = np.concatenate([dec(c) for c in np.array_split(np.sin(2 * np.pi * 15000 * t), 13)])
    assert len(keep) == STT_SR
    assert np.abs(np.fft.rfft(keep)).argmax() == 1000
    assert np.abs(alias[100:]).max() < 0.02


class ScriptedEngine(Engine):
    """Pretends to recognise speech: reveals words over time."""
    name = label = "scripted"

    def __init__(self, hyps, final):
        self.hyps, self.final = hyps, final

    def begin(self):
        self.i = 0

    def feed(self, audio, want_hypothesis):
        if self.i < len(self.hyps):
            self.i += 1
            return self.hyps[self.i - 1]
        return None

    def finish(self, fast=False):
        return self.final


def run_transcriber(mode, hyps, final):
    sent, speech = [], []
    tr = Transcriber(ScriptedEngine(hyps, final), mode, on_send=lambda t, f: sent.append((t, f)),
                     on_speech=speech.append, end_ms=300)
    feed_blocks(tr.feed, np.concatenate([quiet(0.5), tone_burst(1.5), quiet(1)]), 1600)
    return sent, speech


def test_phrase_mode_sends_once_after_you_finish():
    sent, speech = run_transcriber(PHRASE, ["hello", "hello there"], "Hello there, servitor.")
    assert sent == [("Hello there, servitor.", True)]
    assert speech == [True, False]


def test_live_mode_sends_words_as_they_stabilise():
    hyps = ["praise", "praise the", "praise the omni", "praise the omnissiah", "praise the omnissiah"]
    sent, _ = run_transcriber(LIVE, hyps, "praise the Omnissiah now")
    # committed once two hypotheses agree, never the newest word; "omni" is never sent
    assert [t for t, _ in sent] == ["praise", "the", "Omnissiah now"]
    assert sent[-1][1] is True
    assert " ".join(t for t, _ in sent).lower() == "praise the omnissiah now"


def test_live_mode_never_sends_a_cut_off_last_word():
    # you're mid-word for two passes in a row: "omni" agrees twice but must not go out
    hyps = ["praise the omni", "praise the omni", "praise the omnissiah"]
    sent, _ = run_transcriber(LIVE, hyps, "praise the omnissiah")
    words = " ".join(t for t, _ in sent).split()
    assert "omni" not in words and words == ["praise", "the", "omnissiah"]


@pytest.mark.parametrize("sent,final,rest", [
    ("the machine spirit is", "The machine spirit's pleased, the reactor", "pleased, the reactor"),
    ("praise the omni", "Praise the Omnissiah now", "now"),
    ("send more service", "Send more servitors to the forge", "to the forge"),
    ("cannot", "can not do it", "do it"),
    ("hello there", "hello there", ""),
])
def test_live_final_pass_continues_where_it_left_off(sent, final, rest):
    from binharic.stt import remaining_words
    assert " ".join(remaining_words(sent.split(), final.split())) == rest


def test_agreement_ignores_case_and_punctuation():
    assert agreed_prefix("Hello, there".split(), "hello there friend".split()) == 2
    assert agreed_prefix("a b".split(), "a c".split()) == 1


class FakeIn:
    def __init__(self, sig):
        self.sig, self.i = sig, 0

    def __call__(self, sr, source=None, device=None, blocksize=4800, **kw):
        self.bs = blocksize
        return self

    def read(self):
        time.sleep(self.bs / core.SR)  # a mic delivers audio in real time (and FakeOut takes it so)
        b = self.sig[self.i:self.i + self.bs]
        self.i += self.bs
        return b if len(b) == self.bs else np.zeros(self.bs, np.float32)

    def close(self):
        pass


class FakeOut:
    needs_feeding = True
    latency = 0.0

    def ensure_alive(self):
        pass

    def __init__(self):
        self.blocks = []

    def __call__(self, sr, sink=None, device=None, blocksize=960, **kw):
        return self

    def write(self, s):
        self.blocks.append(np.asarray(s, np.float32).copy())
        # a sound card takes audio in real time; without this the output thread
        # wrote minutes of silence per second, and decoding that took 5 minutes on Windows
        time.sleep(len(self.blocks[-1]) / core.SR)

    def close(self):
        pass


@pytest.mark.parametrize("enabled,passthrough", [(True, False), (False, True)])
def test_translator_mutes_voice_and_screeches(monkeypatch, tmp_path, enabled, passthrough):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sr = core.SR
    voice = tone_burst(1.5, sr)
    mic = FakeIn(np.concatenate([quiet(0.5, sr), voice, quiet(2.0, sr)]))
    out = FakeOut()
    monkeypatch.setattr(core, "Input", mic)
    monkeypatch.setattr(core, "Output", out)
    monkeypatch.setattr(core, "make_engine", lambda *a, **k: ScriptedEngine(["by the cog"], "by the cog"))

    tr = core.Translator(core.Settings(enabled=enabled, passthrough=passthrough, key="k", end_ms=300,
                                       duplex="full", control_socket=True))
    sent = []
    tr.subscribe(lambda kind, ev: kind == "sent" and sent.append(ev["text"]))
    tr.start()
    deadline = time.time() + 20
    while time.time() < deadline and mic.i < len(mic.sig) + sr:
        time.sleep(0.05)
    while tr.screeching.is_set() or tr._pending:
        time.sleep(0.05)
    assert '"enabled": %s' % str(enabled).lower() in core.send_command("status")
    tr.stop()
    played = np.concatenate(out.blocks)
    voice_level = np.max(np.abs(np.convolve(played, voice[:480][::-1], "valid"))) / np.sum(voice[:480] ** 2)

    if enabled:
        assert sent == ["by the cog"]
        assert [m.text for m in modem.decode_audio(played, sr, key="k")] == ["by the cog"]
        assert not np.any(np.isclose(played[: len(voice)], voice[: len(played)], atol=1e-6).all())
    else:
        assert sent == []
        assert voice_level > 0.9  # your normal voice went through


def test_control_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    tr = core.Translator(core.Settings())
    tr.handle("send toggle")
    assert tr.s.send_mode == LIVE
    tr.handle("send phrase")
    assert tr.s.send_mode == PHRASE
    tr.handle("toggle")
    assert not tr.s.enabled
    tr.handle("mode hex")
    assert tr.s.mode == "hex"
    assert tr.handle("bogus").startswith("error")


def test_second_instance_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(core, "Input", FakeIn(np.zeros(48000, np.float32)))
    monkeypatch.setattr(core, "Output", FakeOut())
    monkeypatch.setattr(core, "make_engine", lambda *a, **k: ScriptedEngine([], ""))
    first = core.Translator(core.Settings())
    first.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            core.Translator(core.Settings()).start()
    finally:
        first.stop()
    # after a clean stop the next one starts fine
    again = core.Translator(core.Settings())
    again.start()
    again.stop()


class PeekingEngine(ScriptedEngine):
    """Supports speculative finals; counts how often the slow final pass runs."""

    def __init__(self, final):
        super().__init__([], final)
        self.peeks = self.finishes = 0

    def peek(self, fast=False):
        self.peeks += 1
        return self.final

    def finish(self, fast=False):
        self.finishes += 1
        return self.final

    def discard(self):
        pass


def test_speculative_final_is_used_when_you_really_stopped():
    eng = PeekingEngine("done talking")
    sent = []
    tr = Transcriber(eng, PHRASE, on_send=lambda t, f: sent.append(t), end_ms=600)
    feed_blocks(tr.feed, np.concatenate([quiet(0.5), tone_burst(1), quiet(1.2)]), 800)
    assert sent == ["done talking"]
    assert (eng.peeks, eng.finishes) == (1, 0) and tr.last_speculative


def test_speculative_final_is_dropped_if_you_carry_on():
    eng = PeekingEngine("whole sentence")
    sent = []
    tr = Transcriber(eng, PHRASE, on_send=lambda t, f: sent.append(t), end_ms=600)
    # a 300 ms breath: long enough to trigger the early pass, short enough to keep talking
    feed_blocks(tr.feed, np.concatenate([quiet(0.5), tone_burst(1), quiet(0.3), tone_burst(1), quiet(1.2)]), 800)
    assert sent == ["whole sentence"]
    assert eng.peeks == 2  # one wasted, one used


def test_test_mode_plays_locally_verifies_and_times(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sr = core.SR
    mic = FakeIn(np.concatenate([quiet(0.5, sr), tone_burst(1.2, sr), quiet(2.5, sr)]))
    outs = []

    class Out(FakeOut):
        def __call__(self, sr, sink=None, device=None, blocksize=960, **kw):
            o = FakeOut()
            o.sink = sink
            outs.append(o)
            return o

    monkeypatch.setattr(core, "Input", mic)
    monkeypatch.setattr(core, "Output", Out())
    monkeypatch.setattr(core, "make_engine", lambda *a, **k: PeekingEngine("testing one two"))
    tr = core.Translator(core.Settings(enabled=False, test=True, sink="binharic_out", end_ms=300,
                                       control_socket=False, key="k"))
    events = []
    tr.subscribe(lambda kind, ev: kind in ("sent", "verify") and events.append((kind, ev)))
    tr.start()
    deadline = time.time() + 20
    while time.time() < deadline and not any(k == "verify" for k, _ in events):
        time.sleep(0.05)
    while tr.screeching.is_set():
        time.sleep(0.05)
    tr.stop()

    sent = [e for k, e in events if k == "sent"]
    verify = [e for k, e in events if k == "verify"]
    assert sent and sent[0]["text"] == "testing one two" and sent[0]["test"]
    assert 0 < sent[0]["latency_ms"] < 5000 and sent[0]["speculative"]
    assert verify[0]["ok"] and verify[0]["decoded"] == "testing one two"
    sink_out, local = outs[0], outs[1]  # the virtual mic first, your speakers opened for test mode
    # the virtual mic got nothing; your speakers got the screech
    assert not sink_out.blocks or np.abs(np.concatenate(sink_out.blocks)).max() == 0
    played = np.concatenate(local.blocks)
    assert [m.text for m in modem.decode_audio(played, sr, key="k")] == ["testing one two"]


def test_whisper_falls_back_to_cpu_when_cuda_libs_are_missing(monkeypatch):
    import sys
    import types

    from binharic import gpu, stt

    made = []

    class FakeWhisperModel:
        def __init__(self, name, device, compute_type, **kw):
            made.append((name, device, compute_type))
            self.device = device

        def transcribe(self, audio, **kw):
            if self.device == "cuda":  # what your machine did: fails on first use
                raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
            return iter([]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisperModel))
    monkeypatch.setattr(gpu, "cuda_devices", lambda: 1)
    monkeypatch.setattr(gpu, "preload_cuda_libs", lambda: False)
    eng = stt.WhisperEngine("base.en", device="auto", live_model=None)
    assert eng.device == "cpu" and "CPU" in eng.label
    assert "libcublas" in eng.notice and "install.sh" in eng.notice
    assert made == [("base.en", "cuda", "float16"), ("base.en", "cpu", "int8")]
    # asking for the GPU explicitly still fails loudly
    with pytest.raises(RuntimeError):
        stt.WhisperEngine("base.en", device="cuda", live_model=None)


def test_fatal_engine_error_stops_the_layer(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(core, "Input", FakeIn(np.zeros(48000 * 5, np.float32)))
    monkeypatch.setattr(core, "Output", FakeOut())

    def boom(*a, **k):
        raise RuntimeError("Library libcublas.so.12 is not found")

    monkeypatch.setattr(core, "make_engine", boom)
    tr = core.Translator(core.Settings())
    tr.start()
    deadline = time.time() + 5
    while not tr.stopped and time.time() < deadline:
        time.sleep(0.02)
    t = time.time()
    tr.stop()
    tr.stop()  # idempotent
    assert tr.stopped and "libcublas" in tr.fatal
    assert time.time() - t < 1.0
