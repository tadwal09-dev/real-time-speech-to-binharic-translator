"""Regression tests for bugs found in the full test pass."""

import argparse
import sys
import types
import wave

import numpy as np
import pytest

from binharic import audio, cli, codec, core, modem, pulse, stt
from binharic.vad import EnergyVAD

MSG = ("praise the omnissiah " * 5).strip()  # ~100 bytes


# -- modem -------------------------------------------------------------------

@pytest.mark.parametrize("mode", list(modem.MODES))
@pytest.mark.parametrize("ppm", [-1000, 1000])
def test_decoder_tracks_clock_drift(mode, ppm):
    # two sound cards never agree exactly; 1000 ppm is ~10x typical hardware
    a = np.concatenate([np.zeros(24000), modem.encode_text(MSG, mode)])
    b = np.interp(np.arange(0, len(a) - 1, 1 + ppm * 1e-6), np.arange(len(a)), a)
    assert [m.text for m in modem.decode_audio(b, 48000, mode)] == [MSG]


@pytest.mark.parametrize("mode", list(modem.MODES))
@pytest.mark.parametrize("rate", [16000, 44100])
def test_cross_sample_rate_at_odd_speed(mode, rate):
    # the old int() timing grid drifted at speeds like 1.5x between sample rates
    a = modem.encode_text(MSG, mode, speed=1.5)
    b = np.interp(np.arange(int(len(a) * rate / 48000)) * 48000 / rate, np.arange(len(a)), a)
    assert [m.text for m in modem.decode_audio(b, rate, mode, speed=1.5)] == [MSG]


@pytest.mark.parametrize("mode", list(modem.MODES))
def test_cut_off_transmission_does_not_block_the_next(mode):
    rng = np.random.default_rng(0)
    cut = modem.encode_text("x" * 200, mode)[:48000]  # someone switched off mid-screech
    real = modem.encode_text("the real message", mode)
    sig = np.concatenate([cut, np.zeros(12000), real, np.zeros(24000)])
    sig = sig + 0.01 * rng.standard_normal(len(sig))
    dec = modem.StreamDecoder(48000)
    got = []
    for i in range(0, len(sig), 480):  # 10 ms blocks: frames arrive a little at a time
        got += [m.text for m in dec.feed(sig[i:i + 480])]
    assert got == ["the real message"]


def test_long_text_is_split_not_truncated():
    text = " ".join(f"servitor{i:03d}" for i in range(150))  # ~1800 bytes
    msgs = modem.decode_audio(modem.encode_text(text, "hex"), 48000)
    assert len(msgs) > 1 and " ".join(m.text for m in msgs) == text


def test_split_text_limits_and_boundaries():
    for text in ["", "a", "x" * 700, "ab " + "é" * 200 + " cd", " ".join(f"w{i}" for i in range(300))]:
        chunks = codec.split_text(text, 60)
        assert all(len(c.encode("utf-8")) <= 60 for c in chunks)
        assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_reverse_text_errors_are_clear():
    with pytest.raises(ValueError, match="hex digits"):
        codec.from_hex("486")
    with pytest.raises(ValueError, match="bits"):
        codec.from_binary("0100100")
    assert codec.from_hex("48 69") == "Hi"


# -- audio files ---------------------------------------------------------------

def _write(path, width, channels, data):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(48000)
        w.writeframes(data)


def test_read_wav_24_bit_stereo(tmp_path):
    sig = modem.encode_text("twenty four bit", "binharic") * 0.5
    v = np.round(sig * (2 ** 23 - 1)).astype(np.int32)
    raw = np.stack([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF], axis=1).astype(np.uint8)
    stereo = np.repeat(raw, 2, axis=0).tobytes()  # same sample on both channels
    _write(tmp_path / "a.wav", 3, 2, stereo)
    x, sr = audio.read_wav(str(tmp_path / "a.wav"))
    assert sr == 48000 and [m.text for m in modem.decode_audio(x, sr)] == ["twenty four bit"]


def test_read_wav_bad_file_gives_a_hint(tmp_path):
    (tmp_path / "bad.wav").write_bytes(b"not a wav file at all")
    with pytest.raises(RuntimeError, match="ffmpeg"):
        audio.read_wav(str(tmp_path / "bad.wav"))


# -- speech --------------------------------------------------------------------

def test_vad_hears_a_quiet_mic():
    sr = 16000
    t = np.arange(sr * 2) / sr
    speech = (np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)) * 0.004).astype(np.float32)
    rng = np.random.default_rng(1)
    sig = np.concatenate([np.zeros(sr), speech, np.zeros(sr * 2)]) + 0.0003 * rng.standard_normal(sr * 5)
    vad = EnergyVAD(sr)
    kinds = []
    for i in range(0, len(sig), 1600):
        kinds += [e.kind for e in vad.feed(sig[i:i + 1600]) if e.kind in ("start", "end")]
    assert kinds == ["start", "end"]


@pytest.mark.parametrize("room", [0.003, 0.01, 0.02])
def test_vad_in_a_noisy_room(room):
    # steady fan/PC noise used to count as speech that never ended
    sr = 16000
    t = np.arange(sr * 2) / sr
    speech = (np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)) * 0.1).astype(np.float32)
    rng = np.random.default_rng(2)
    sig = np.concatenate([np.zeros(sr * 3), speech, np.zeros(sr * 3)]) + room * rng.standard_normal(sr * 8)
    vad = EnergyVAD(sr)
    events = []
    for i in range(0, len(sig), 640):
        events += [(e.kind, i / sr) for e in vad.feed(sig[i:i + 640]) if e.kind in ("start", "end")]
    assert [k for k, _ in events] == ["start", "end"]
    assert 2.9 < events[0][1] < 3.2 and 5.0 < events[1][1] < 6.0


def test_whisper_noise_hallucinations_are_dropped():
    assert stt._is_hallucination("Thank you.", 1.0)
    assert stt._is_hallucination(" you", 0.6)
    assert not stt._is_hallucination("Thank you.", 4.0)  # a real, longer "thank you" stays
    assert not stt._is_hallucination("thank you brother", 1.0)


def _fake_whisper(monkeypatch):
    seen = []

    class Model:
        def __init__(self, name, **kw):
            seen.append(name)

        def transcribe(self, audio, **kw):
            seen.append(kw.get("language"))
            return iter([]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    from binharic import gpu
    monkeypatch.setattr(gpu, "cuda_devices", lambda: 0)
    return seen


def test_multilingual_models_detect_the_language(monkeypatch):
    _fake_whisper(monkeypatch)
    assert stt.WhisperEngine("small", live_model="base.en").language is None
    assert stt.WhisperEngine("small.en").language == "en"
    assert stt.WhisperEngine("small", language="de").language == "de"


def test_multilingual_model_does_not_use_an_english_only_live_model(monkeypatch):
    seen = _fake_whisper(monkeypatch)
    stt.WhisperEngine("small", live_model="base.en")
    assert "base.en" not in seen


def test_fast_preset_with_vosk_does_not_pass_a_whisper_model(monkeypatch):
    monkeypatch.setattr(stt, "have_whisper", lambda: False)
    args = cli.build_parser().parse_args(["listen", "--fast"])
    st = cli._settings(args)
    assert st.model is None and st.beam == 1 and st.end_ms == 450


# -- command line ----------------------------------------------------------------

@pytest.mark.parametrize("argv", [["say", "--speed", "0", "x"], ["say", "--volume", "-1", "x"],
                                  ["listen", "--pause-ms", "5"], ["decode", "--threshold", "2"]])
def test_bad_numbers_are_rejected_cleanly(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(argv)
    assert e.value.code == 2 and "must be between" in capsys.readouterr().err


def test_say_with_nothing_to_say(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False, read=lambda: "  \n"))
    with pytest.raises(SystemExit, match="nothing to say"):
        cli.cmd_say(cli.build_parser().parse_args(["say", "--wav", str(tmp_path / "x.wav")]))


def test_bare_binharic_without_a_terminal_starts_the_log_ui(monkeypatch):
    # used to crash with AttributeError: 'Namespace' object has no attribute 'show'
    calls = {}
    monkeypatch.setattr(cli, "_translator", lambda a: "tr")
    monkeypatch.setattr(cli, "_run_ui", lambda run, tr, **kw: calls.update(kw))
    cli.cmd_listen(argparse.Namespace())
    assert calls == {"show": "text"}


def test_markup_in_text_is_escaped():
    Console = pytest.importorskip("rich.console").Console
    assert cli.esc("[/] [red]x\\") != "[/] [red]x\\"
    c = Console(record=True, width=200)
    c.print(f"[green]▼ {cli.esc('[binharic]')} {cli.esc('[/] [red]x' + chr(92))}[/]")
    assert "[binharic] [/] [red]x\\" in c.export_text()


# -- devices ---------------------------------------------------------------------

def test_speakers_means_the_real_speakers_not_our_sink(tmp_path, monkeypatch):
    from tests.test_pulse import fake_pactl
    fake_pactl(tmp_path, monkeypatch, "binharic_mic", "binharic_out")  # PipeWire made ours the defaults
    assert pulse.resolve_source("speakers") == "alsa_output.pci.analog-stereo.monitor"


def test_unknown_device_names_are_explained(tmp_path, monkeypatch):
    from tests.test_pulse import fake_pactl
    fake_pactl(tmp_path, monkeypatch, "binharic_mic", "binharic_out")
    with pytest.raises(RuntimeError, match="no input .* 'nosuchmic'.*binharic devices"):
        pulse.check_device("nosuchmic", "sources")


# -- runtime ---------------------------------------------------------------------

def test_off_means_off_even_for_already_queued_speech(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    tr = core.Translator(core.Settings(control_socket=False))
    played = []

    class Out:
        latency = 0.0
        needs_feeding = False

        def write(self, s):
            played.append(len(s))

    tr._queue_text("spoken words", True)
    tr.s.enabled = False  # switched OFF after it was queued, before it played
    tr._play_text(Out(), *tr._take_pending())
    assert played == []
    tr.say("typed while off")  # typing still works while OFF
    tr._play_text(Out(), *tr._take_pending())
    assert played


# -- decoy chirps --------------------------------------------------------------------

@pytest.mark.parametrize("mode", list(modem.MODES))
def test_decoys_hide_the_real_message(mode):
    a = modem.encode_text("meet at the forge", mode, key="cog", decoys=3)
    blind = modem.decode_audio(a, 48000)
    assert len(blind) == 4 and all(m.locked for m in blind)  # 4 identical-looking encrypted frames
    keyed = [m.text for m in modem.decode_audio(a, 48000, key="cog") if not m.locked]
    assert keyed == ["meet at the forge"]


def test_decoys_land_at_random_places():
    positions = {[real for real, _ in modem.frames_with_decoys("x", 2, "k", 3)].index(True) for _ in range(60)}
    assert len(positions) > 1


def test_decoys_need_a_key():
    assert len(modem.frames_with_decoys("x", 2, None, 5)) == 1
    with pytest.raises(SystemExit, match="needs a key"):
        cli.cmd_say(cli.build_parser().parse_args(["say", "--decoys", "2", "--wav", "/dev/null", "x"]))


def test_layer_hides_decoys_from_key_holders(monkeypatch, tmp_path):
    tr = core.Translator(core.Settings(control_socket=False, key="cog", decoys=2))
    played = []

    class Out:
        latency = 0.0
        needs_feeding = False

        def write(self, s):
            played.append(np.asarray(s))

    events = []
    tr.subscribe(lambda k, e: k == "sent" and events.append(e))
    tr.say("hidden words")
    tr._play_text(Out(), *tr._take_pending())
    audio = np.concatenate(played)
    assert events[0]["decoys"] == 2
    assert len(modem.decode_audio(audio, 48000)) == 3
    assert [m.text for m in modem.decode_audio(audio, 48000, key="cog") if not m.locked] == ["hidden words"]
