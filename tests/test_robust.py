"""What a voice call does to a screech, and that the message still arrives.

A call loses packets (the receiver papers over each with a guess), and its jitter
buffer cuts out or repeats a few milliseconds several times a second to keep up.
Before error correction and slip-tolerant timing, one wrong tone lost the message:
sentence-length messages mostly failed on an ordinary call.
"""

import random

import numpy as np
import pytest

from binharic import codec, modem

SR = 48000
TEXT = "the flesh is weak but the machine endures"
CALL_MODES = ["hex", "binharic"]  # binary's 31 ms beeps are no match for 15 ms cuts; see the README


def pad(x, before=0.6, after=0.6):
    return np.concatenate([np.zeros(int(before * SR), np.float32), x, np.zeros(int(after * SR), np.float32)])


def live(x, block=4800, **kw):
    dec = modem.StreamDecoder(SR, **kw)
    out = [m for i in range(0, len(x), block) for m in dec.feed(x[i:i + block])]
    return out + dec.flush()


def both_ways(x, **kw):
    return [m.text for m in modem.decode_audio(x, SR, **kw)], [m.text for m in live(x, **kw)]


def ms(n):
    return int(n * SR / 1000)


# -- Reed-Solomon --------------------------------------------------------------------

def test_reed_solomon_repairs_errors_and_erasures_up_to_its_limit():
    rnd = random.Random(1)
    for _ in range(400):
        nsym = rnd.choice([4, 6, 10, 20, 54])
        data = bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 255 - nsym)))
        block = bytearray(data + codec.rs_parity(data, nsym))
        erased = rnd.randrange(0, nsym + 1)
        wrong = rnd.randrange(0, (nsym - erased) // 2 + 1)
        where = rnd.sample(range(len(block)), min(len(block), erased + wrong))
        for i in where[erased:]:
            block[i] ^= rnd.randrange(1, 256)
        for i in where[:erased]:
            block[i] = rnd.randrange(256)
        fixed, changed = codec.rs_correct(bytes(block), nsym, where[:erased])
        assert fixed == data and changed <= len(where)


def test_reed_solomon_refuses_what_it_cannot_repair():
    rnd = random.Random(2)
    refused = 0
    for _ in range(300):
        data = bytes(rnd.randrange(256) for _ in range(40))
        block = bytearray(data + codec.rs_parity(data, 6))
        for i in rnd.sample(range(len(block)), 5):
            block[i] ^= rnd.randrange(1, 256)
        try:
            codec.rs_correct(bytes(block), 6)
        except codec.Uncorrectable:
            refused += 1
    assert refused > 280  # the rare "repair" into another valid block is caught by the frame's checksum


# -- frames --------------------------------------------------------------------------

@pytest.mark.parametrize("key", [None, "cant"])
def test_a_frame_survives_wrong_bytes(key):
    rnd = random.Random(3)
    frame = codec.build_frame(TEXT, 2, key)
    nsym = next(n for n in range(4, 60, 2) if codec.parity_len(len(frame) - codec.HEADER_LEN - n) == n)
    for _ in range(50):
        f = bytearray(frame)
        for i in rnd.sample(range(codec.HEADER_LEN), 2):  # the header: 2 of its 6 bytes
            f[i] ^= rnd.randrange(1, 256)
        for i in rnd.sample(range(codec.HEADER_LEN, len(f)), nsym // 2):
            f[i] ^= rnd.randrange(1, 256)
        m, used = codec.parse_frame(bytes(f), key, 2)
        assert m.text == TEXT and used == len(frame) and m.repaired == 2 + nsym // 2


def test_doubtful_bytes_count_half():
    # more wrong bytes than plain repair can take, but the receiver knew which ones it was unsure of
    frame = codec.build_frame(TEXT, 2)
    nsym = next(n for n in range(4, 60, 2) if codec.parity_len(len(frame) - codec.HEADER_LEN - n) == n)
    f = bytearray(frame)
    wrong = list(range(codec.HEADER_LEN + 1, codec.HEADER_LEN + 1 + nsym - 2))
    for i in wrong:
        f[i] ^= 0x5A
    with pytest.raises(codec.BadFrame):
        codec.parse_frame(bytes(f), None, 2)
    doubt = [0.9 if i in wrong else 0.1 for i in range(len(f))]
    assert codec.parse_frame(bytes(f), None, 2, doubt)[0].text == TEXT


def test_every_frame_fits_one_block_and_long_text_is_split():
    text = "praise the omnissiah " * 40
    chunks = codec.split_text(text)
    assert " ".join(chunks) == text.strip() and all(len(c.encode()) <= codec.CHUNK_BYTES for c in chunks)
    for key in (None, "cant"):
        for n in (0, 1, codec.CHUNK_BYTES, codec.MAX_PAYLOAD, 400):
            f = codec.build_frame("x" * n, 2, key)
            assert len(f) - codec.HEADER_LEN <= 255
            assert codec.parse_frame(f, key, 2)[0].text == "x" * min(n, codec.MAX_PAYLOAD)
        assert codec.parse_frame(codec.build_decoy(2, 400), "cant", 2)[0].locked


# -- friends who haven't updated: frames without error correction ---------------------

@pytest.mark.parametrize("mode", list(modem.MODES))
@pytest.mark.parametrize("key", [None, "cant"])
def test_screeches_from_older_versions_are_still_read(mode, key):
    spec = modem.get_mode(mode)
    old = codec.build_frame(TEXT, spec.id, key, version=1)
    assert old[0] & 0xF0 == codec.MAGIC_V1 and len(old) < len(codec.build_frame(TEXT, spec.id, key))
    x = pad(np.concatenate([modem.modulate(old, spec, SR, 0.6), modem.encode_text("and new ones", mode, key)]))
    assert both_ways(x, key=key) == ([TEXT, "and new ones"], [TEXT, "and new ones"])
    if key:
        assert all(m.locked for m in modem.decode_audio(x, SR, key="wrong"))


# -- what a call does ----------------------------------------------------------------

@pytest.mark.parametrize("mode", CALL_MODES)
@pytest.mark.parametrize("what", ["cut", "repeat", "silence"])
def test_sound_cut_out_or_repeated_mid_message(mode, what):
    # a jitter buffer catching up (cut), falling behind (repeat), or a sound server
    # missing a cycle (silence put in): every 0.6 s, 12 ms each
    x = pad(modem.encode_text(TEXT, mode, volume=0.6))
    n = ms(12)
    pieces, last = [], 0
    for at in range(int(0.9 * SR), len(x) - SR // 2, int(0.6 * SR)):
        pieces.append(x[last:at])
        if what == "cut":
            last = at + n
        else:
            pieces.append(x[at - n:at] if what == "repeat" else np.zeros(n, np.float32))
            last = at
    pieces.append(x[last:])
    assert len(pieces) > 8
    assert both_ways(np.concatenate(pieces)) == ([TEXT], [TEXT])


@pytest.mark.parametrize("mode", CALL_MODES)
def test_lost_packets_papered_over(mode):
    # every 0.35 s, 20 ms is replaced by the 20 ms before it (what a receiver's
    # concealment does with a tone): about 6% of the packets
    x = pad(modem.encode_text(TEXT, mode, volume=0.6)).copy()
    n = ms(20)
    hits = 0
    for at in range(int(0.8 * SR), len(x) - SR // 2, int(0.35 * SR)):
        x[at:at + n] = x[at - n:at]
        hits += 1
    assert hits > 10
    offline = modem.decode_audio(x, SR)
    assert [m.text for m in offline] == [TEXT] == [m.text for m in live(x)]


@pytest.mark.parametrize("mode", CALL_MODES)
def test_a_noisy_stretched_lossy_call(mode):
    rng = np.random.default_rng(5)
    x = pad(modem.encode_text(TEXT, mode, key="cant", volume=0.5)).copy()
    for at in range(int(0.75 * SR), len(x) - SR // 2, int(0.9 * SR)):
        x[at:at + ms(20)] = x[at - ms(20):at]
    cut, rep = int(1.7 * SR), int(3.1 * SR)
    x = np.concatenate([x[:cut], x[cut + ms(9):rep], x[rep - ms(14):rep], x[rep:]])
    x = (x + 0.05 * rng.standard_normal(len(x))).astype(np.float32)
    assert both_ways(x, key="cant") == ([TEXT], [TEXT])


def test_a_message_stretched_past_its_end_is_waited_for():
    # the live decoder must not give up on a frame whose last tones arrive late
    x = pad(modem.encode_text("late tail", "binharic", volume=0.6), 0.3, 1.0)
    at = int(0.3 * SR + 0.5 * SR)
    stretched = np.concatenate([x[:at], x[at - ms(15):at], x[at:at + ms(200)], x[at + ms(185):at + ms(200)], x[at + ms(200):]])
    for block in (480, 1024, 4800):
        assert [m.text for m in live(stretched, block)] == ["late tail"], block


def test_a_whole_tone_early_or_late_is_not_mistaken_for_the_start():
    # regression: with only the header read, a path one whole beep late scored as well as
    # the right one, and binary frames from a sender with a fast clock were all lost
    for ppm in (-3000, 3000):
        x = pad(modem.encode_text("hi there", "binary", volume=0.6))
        n = int(round(len(x) * (1 + ppm * 1e-6)))
        y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
        assert both_ways(y) == (["hi there"], ["hi there"]), ppm


def test_repairs_are_counted():
    x = pad(modem.encode_text(TEXT, "hex", volume=0.6)).copy()
    assert modem.decode_audio(x, SR)[0].repaired == 0
    at = int(2.0 * SR)
    x[at:at + ms(150)] = 0  # a dropout: two or three tones gone
    [m] = modem.decode_audio(x, SR)
    assert m.text == TEXT and m.repaired >= 1
