import numpy as np
import pytest

from binharic import codec, modem

TEXT = "Praise the Omnissiah! 01 ünïcode ⚙"


@pytest.mark.parametrize("mode", list(modem.MODES))
@pytest.mark.parametrize("sr", [16000, 48000])
def test_roundtrip_clean(mode, sr):
    audio = modem.encode_text(TEXT, mode, sr=sr)
    [msg] = modem.decode_audio(audio, sr, mode)
    assert msg.text == TEXT and not msg.encrypted


@pytest.mark.parametrize("mode", list(modem.MODES))
def test_auto_mode_with_noise_and_offset(mode):
    sr = 48000
    rng = np.random.default_rng(0)
    audio = modem.encode_text(TEXT, mode, sr=sr)
    sig = np.concatenate([rng.standard_normal(7777) * 0.05, audio * 0.5, np.zeros(3000)])
    sig = sig + 0.1 * rng.standard_normal(len(sig))  # ~SNR 8 dB
    [msg] = modem.decode_audio(sig, sr, "auto")
    assert msg.text == TEXT
    assert modem.MODES_BY_ID[msg.mode_id].name == mode


def test_keyed_messages():
    sr = 16000
    audio = modem.encode_text("the cog turns", "binharic", key="noosphere", sr=sr)
    [ok] = modem.decode_audio(audio, sr, key="noosphere")
    assert ok.text == "the cog turns" and ok.encrypted
    [locked] = modem.decode_audio(audio, sr)
    assert locked.locked and locked.text is None
    [wrong] = modem.decode_audio(audio, sr, key="heresy")
    assert wrong.locked


def test_speed_must_match():
    sr = 16000
    audio = modem.encode_text("fast", "hex", sr=sr, speed=2.0)
    assert [m.text for m in modem.decode_audio(audio, sr, speed=2.0)] == ["fast"]
    assert modem.decode_audio(audio, sr, speed=1.0) == []


def test_stream_decoder_multiple_messages():
    sr = 16000
    rng = np.random.default_rng(1)
    parts = [np.zeros(4000)]
    for i, mode in enumerate(["binharic", "binary", "hex"]):
        parts += [modem.encode_text(f"msg {i}", mode, sr=sr), rng.standard_normal(sr // 2) * 0.05]
    sig = np.concatenate(parts) + 0.05 * rng.standard_normal(sum(map(len, parts)))
    dec = modem.StreamDecoder(sr)
    got = []
    for i in range(0, len(sig), 800):
        got += [m.text for m in dec.feed(sig[i:i + 800])]
    assert got == ["msg 0", "msg 1", "msg 2"]


def test_noise_yields_nothing():
    sr = 16000
    rng = np.random.default_rng(2)
    assert modem.decode_audio(rng.standard_normal(sr * 5) * 0.3, sr) == []


def test_resampled_playback_drift():
    # a soundcard running 0.05% off-rate should still decode
    sr = 16000
    audio = modem.encode_text(TEXT, "binharic", sr=sr)
    x = np.arange(0, len(audio) - 1, 1.0005)
    drifted = np.interp(x, np.arange(len(audio)), audio)
    [msg] = modem.decode_audio(drifted, sr)
    assert msg.text == TEXT


def test_frame_corruption_detected():
    frame = bytearray(codec.build_frame("hello", 2))
    frame[-3] ^= 0x01
    with pytest.raises(codec.BadFrame):
        codec.parse_frame(bytes(frame))


def test_text_helpers():
    assert codec.to_binary("Hi") == "01001000 01101001"
    assert codec.to_hex("Hi") == "48 69"
    assert codec.from_binary("01001000 01101001") == "Hi"
    assert codec.from_hex("48 69") == "Hi"


def test_stream_decoder_flush_catches_a_short_tail():
    sr = 16000
    sig = modem.encode_text("tail end", "binharic", sr=sr)
    dec = modem.StreamDecoder(sr)
    got = []
    # one big block that ends just after the frame, then nothing more arrives
    cut = len(sig) - int(0.05 * sr)
    got += dec.feed(sig[: cut - sr // 8])
    got += dec.feed(sig[cut - sr // 8:])
    got += dec.flush()
    assert [m.text for m in got] == ["tail end"]


def test_live_decoder_never_drops_unsearched_audio():
    # regression: while nothing was being decoded, the stream decoder trimmed its
    # buffer past audio it hadn't searched for sync chirps yet, so a message
    # starting in that gap was lost (live `decode`, --hear and --air missed
    # messages depending on timing and block size)
    from binharic import modem
    sr = 48000
    msg = modem.encode_text("never lost", "hex", sr=sr, speed=2.0)
    for lead in (0.05, 0.12, 0.19, 0.27, 0.33, 0.41):
        for block in (480, 960, 2400, 4800, 9600):
            sig = np.concatenate([np.zeros(int(lead * sr), np.float32), msg, np.zeros(sr // 2, np.float32)])
            dec = modem.StreamDecoder(sr, speed=2.0)
            got = [m.text for i in range(0, len(sig), block) for m in dec.feed(sig[i:i + block])]
            got += [m.text for m in dec.flush()]
            assert got == ["never lost"], (lead, block, got)
