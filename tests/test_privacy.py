"""Encryption and the three privacy options: unencrypted, encrypted, encrypted + gibberish."""

import binascii
import time

import numpy as np
import pytest

from binharic import cli, codec, core, modem

KEY = "the omnissiah protects"


def _recrc(frame: bytearray) -> bytes:
    body = bytes(frame[:-2])
    return body + binascii.crc_hqx(body, 0xFFFF).to_bytes(2, "big")


# -- the cipher ---------------------------------------------------------------------

def test_round_trip_and_wrong_key():
    f = codec.build_frame("praise the machine god", 2, KEY)
    assert codec.parse_frame(f, KEY)[0].text == "praise the machine god"
    for key in (None, "wrong", KEY + " "):
        m = codec.parse_frame(f, key)[0]
        assert m.locked and m.text is None


@pytest.mark.parametrize("where", ["flag", "nonce", "tag", "length", "ciphertext"])
def test_any_tampering_is_rejected(where):
    # an attacker who fixes up the CRC still can't change a single bit
    f = bytearray(codec.build_frame("send 3 servitors", 2, KEY))
    pos = {"nonce": 2, "tag": 1 + codec.NONCE_LEN + 1, "length": 1 + codec.NONCE_LEN + codec.TAG_LEN + 1,
           "ciphertext": 1 + codec.NONCE_LEN + codec.TAG_LEN + 2 + 3}.get(where)
    if where == "flag":
        f[0] ^= 0x02  # claim another sound mode
        m = codec.parse_frame(_recrc(f), KEY)[0]
    elif where == "length":  # chop the last letter off and fix the length field to match
        n = int.from_bytes(f[pos - 1:pos + 1], "big") - 1
        f[pos - 1:pos + 1] = n.to_bytes(2, "big")
        m = codec.parse_frame(_recrc(f[:-3] + f[-2:]), KEY)[0]
    else:
        f[pos] ^= 0x01
        m = codec.parse_frame(_recrc(f), KEY)[0]
    assert m.locked and m.text is None


def test_ciphertext_hides_the_text_and_nonces_differ():
    a, b = codec.build_frame("aaaaaaaaaaaaaaaa", 2, KEY), codec.build_frame("aaaaaaaaaaaaaaaa", 2, KEY)
    assert a != b  # fresh nonce every message: the same words never sound the same
    assert b"aaaa" not in a


def test_key_stretching_is_cached_and_quick():
    codec._keys.cache_clear()
    t = time.monotonic()
    codec.build_frame("x", 2, "a brand new passphrase")
    first = time.monotonic() - t
    t = time.monotonic()
    for _ in range(20):
        codec.build_frame("x", 2, "a brand new passphrase")
    assert first < 2.0 and (time.monotonic() - t) / 20 < 0.01


def test_overhead_stays_small():
    plain, enc = codec.build_frame("hello", 2), codec.build_frame("hello", 2, KEY)
    assert len(enc) - len(plain) == codec.NONCE_LEN + codec.TAG_LEN == 12  # ~0.6 s of binharic


# -- the three privacy options in the layer ----------------------------------------------

class Out:
    latency = 0.0
    needs_feeding = False

    def __init__(self):
        self.blocks = []

    def write(self, s):
        self.blocks.append(np.asarray(s))


def _send(privacy, text="for the emperor"):
    tr = core.Translator(core.Settings(control_socket=False, key=KEY, privacy=privacy, decoys=3))
    out, sent = Out(), []
    tr.subscribe(lambda k, e: k == "sent" and sent.append(e))
    tr.say(text)
    tr._play_text(out, *tr._take_pending())
    return np.concatenate(out.blocks), sent[0]


def test_unencrypted_even_with_a_key_set():
    audio, ev = _send("plain")
    msgs = modem.decode_audio(audio, 48000)  # no key needed
    assert [m.text for m in msgs] == ["for the emperor"] and not msgs[0].encrypted
    assert ev["privacy"] == "plain" and ev["decoys"] == 0


def test_encrypted():
    audio, ev = _send("encrypted")
    blind = modem.decode_audio(audio, 48000)
    assert len(blind) == 1 and blind[0].locked
    assert [m.text for m in modem.decode_audio(audio, 48000, key=KEY)] == ["for the emperor"]
    assert ev["privacy"] == "encrypted" and ev["decoys"] == 0


def test_encrypted_with_gibberish():
    audio, ev = _send("decoys")
    blind = modem.decode_audio(audio, 48000)
    assert len(blind) == 4 and all(m.locked for m in blind)
    assert [m.text for m in modem.decode_audio(audio, 48000, key=KEY) if not m.locked] == ["for the emperor"]
    assert ev["privacy"] == "decoys" and ev["decoys"] == 3


def test_no_encryption_without_a_key():
    tr = core.Translator(core.Settings(control_socket=False))
    logs = []
    tr.subscribe(lambda k, e: k == "log" and logs.append(e["text"]))
    assert tr.set_privacy("encrypted") is False and tr.s.privacy == "plain"
    assert any("set a key first" in t for t in logs)
    assert tr.handle("privacy decoys").startswith("error")


def test_clearing_the_key_falls_back_to_unencrypted_loudly():
    tr = core.Translator(core.Settings(control_socket=False, key=KEY, privacy="decoys"))
    logs = []
    tr.subscribe(lambda k, e: k == "log" and logs.append(e["text"]))
    tr.set_key("")
    assert tr.s.privacy == "plain" and tr.send_key is None
    assert any("UNENCRYPTED" in t for t in logs)


def test_switching_via_the_control_socket():
    tr = core.Translator(core.Settings(control_socket=False, key=KEY))
    for level in ("encrypted", "decoys", "plain"):
        assert f'"privacy": "{level}"' in tr.handle(f"privacy {level}")


# -- command line --------------------------------------------------------------------

@pytest.mark.parametrize("argv,want", [(["listen"], "plain"), (["listen", "-k", "x"], "encrypted"),
                                       (["listen", "-k", "x", "--decoys", "2"], "decoys"),
                                       (["listen", "-k", "x", "--privacy", "plain"], "plain")])
def test_privacy_from_the_options(argv, want, monkeypatch):
    monkeypatch.delenv("BINHARIC_KEY", raising=False)
    assert cli._settings(cli.build_parser().parse_args(argv)).privacy == want


def test_encrypted_without_key_is_an_error(monkeypatch):
    monkeypatch.delenv("BINHARIC_KEY", raising=False)
    with pytest.raises(SystemExit, match="needs a key"):
        cli._settings(cli.build_parser().parse_args(["listen", "--privacy", "encrypted"]))


@pytest.mark.parametrize("privacy,frames,readable_blind", [("plain", 1, True), ("encrypted", 1, False),
                                                          ("decoys", 4, False)])
def test_say_writes_each_privacy_level(privacy, frames, readable_blind, tmp_path, monkeypatch):
    from binharic.audio import read_wav
    monkeypatch.setattr(cli, "ISH", False)
    wav = str(tmp_path / "x.wav")
    cli.cmd_say(cli.build_parser().parse_args(["say", "-k", KEY, "--privacy", privacy, "--wav", wav, "hail"]))
    x, sr = read_wav(wav)
    blind = modem.decode_audio(x, sr)
    assert len(blind) == frames and (blind[0].text == "hail") == readable_blind
    assert [m.text for m in modem.decode_audio(x, sr, key=KEY) if not m.locked] == ["hail"]


def test_privacy_is_fixed_when_you_speak_not_when_it_plays():
    # regression: a message queued as encrypted went out unencrypted after a switch to plain
    tr = core.Translator(core.Settings(control_socket=False, key=KEY, privacy="decoys", decoys=2))
    tr.say("secret one")
    tr.set_privacy("plain")
    tr.say("open two")
    played = []
    for _ in range(2):
        out = Out()
        tr._play_text(out, *tr._take_pending())
        played.append(np.concatenate(out.blocks))
    assert not tr._pending
    eaves = [m.text for m in modem.decode_audio(played[0], 48000)]
    assert "secret one" not in eaves and "open two" not in eaves
    assert [m.text for m in modem.decode_audio(played[0], 48000, key=KEY) if m.text] == ["secret one"]
    assert [m.text for m in modem.decode_audio(played[1], 48000)] == ["open two"]
