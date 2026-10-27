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
    assert kinds == ["start", "end"]
    speech = sum(len(e.audio) for e in events)
    assert 2.0 * STT_SR <= speech <= 3.0 * STT_SR  # pre-roll + speech + hangover


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
    # committed only once two hypotheses agree; "omni" is never sent
    assert [t for t, _ in sent] == ["praise", "the", "omnissiah", "now"]
    assert sent[-1][1] is True
    assert " ".join(t for t, _ in sent) == "praise the omnissiah now"


def test_agreement_ignores_case_and_punctuation():
    assert agreed_prefix("Hello, there".split(), "hello there friend".split()) == 2
    assert agreed_prefix("a b".split(), "a c".split()) == 1


class FakeIn:
    def __init__(self, sig):
        self.sig, self.i = sig, 0

    def __call__(self, sr, source=None, device=None, blocksize=4800):
        self.bs = blocksize
        return self

    def read(self):
        time.sleep(0.001)
        b = self.sig[self.i:self.i + self.bs]
        self.i += self.bs
        return b if len(b) == self.bs else np.zeros(self.bs, np.float32)

    def close(self):
        pass


class FakeOut:
    def __init__(self):
        self.blocks = []

    def __call__(self, sr, sink=None, device=None, blocksize=960):
        return self

    def write(self, s):
        self.blocks.append(np.asarray(s, np.float32).copy())

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
