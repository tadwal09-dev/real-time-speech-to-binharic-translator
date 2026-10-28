import time

import numpy as np
import pytest

from binharic import codec, incoming, modem

SR = 48000


def garbled_screech(text: str) -> np.ndarray:
    """A screech whose payload changed on the way (one byte), so its checksum fails."""
    spec = modem.get_mode("binharic")
    frame = bytearray(codec.build_frame(text, spec.id))
    frame[5] ^= 0x10
    return modem.modulate(bytes(frame), spec, SR, 0.6)


def pad(x, before=1.0, after=1.0):
    return np.concatenate([np.zeros(int(before * SR), np.float32), x, np.zeros(int(after * SR), np.float32)])


def loud_sound(n: int, seed: int = 1) -> np.ndarray:
    """Noise in the screech's own band (like voices and music), RMS 0.5: more than
    twice as loud as a screech at volume 0.3."""
    f = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    hz = np.fft.rfftfreq(n, 1 / SR)
    f[(hz < 700) | (hz > 3200)] = 0
    x = np.fft.irfft(f, n)
    return (x / np.sqrt(np.mean(x * x)) * 0.5).astype(np.float32)


def feed(dec, x):
    out = []
    for i in range(0, len(x), 4800):
        out += dec.feed(x[i:i + 4800])
    return out + dec.flush()


def test_a_screech_garbled_on_the_way_is_noticed_and_noise_is_not():
    dec = modem.StreamDecoder(SR)
    assert feed(dec, pad(garbled_screech("meet at the forge"))) == [] and dec.garbled == 1
    dec = modem.StreamDecoder(SR)
    rng = np.random.default_rng(0)
    assert feed(dec, (rng.standard_normal(SR * 20) * 0.2).astype(np.float32)) == [] and dec.garbled == 0


class FakeInput:
    """Delivers a fixed recording per source name (or app stream index), then ends."""
    feeds: dict = {}

    def __init__(self, sr, source=None, device=None, blocksize=4800, notify=None, app_stream=None):
        # an app's stream plays once: when it has ended, it's gone from the list
        self.audio = FakeInput.feeds[source] if app_stream is None else FakeInput.feeds.pop(app_stream)
        self.pos, self.bs, self.closed = 0, blocksize, False

    def read(self):
        if self.closed or self.pos >= len(self.audio):
            raise EOFError
        b = self.audio[self.pos:self.pos + self.bs]
        self.pos += self.bs
        time.sleep(0.002)
        return b

    def close(self):
        self.closed = True


def hear(monkeypatch, feeds, sources, apps=None, seconds=2.5):
    FakeInput.feeds = feeds
    monkeypatch.setattr(incoming, "Input", FakeInput)
    monkeypatch.setattr(incoming, "playing_apps",
                        lambda ignore=(): {i: label for i, label in (apps or {}).items() if i in FakeInput.feeds})
    monkeypatch.setattr(incoming.Incoming, "SAME", 0.5)
    monkeypatch.setattr(incoming.Incoming, "RESCAN", 0.05)
    inc = incoming.Incoming(SR, sources, apps=apps is not None)
    inc.start()
    got = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        got += [(h.source, h.message.text if h.message else None) for h in inc.get(0.05)]
    inc.close()
    return got


def test_music_over_a_friends_screech_is_read_from_discords_own_stream(monkeypatch):
    # in the speakers' mix, loud sound over the screech garbles it (measured on the
    # user's desktop); Discord's own playback stream has nothing else in it
    screech = modem.encode_text("meet at the forge", volume=0.3)
    discord = pad(screech, 1.0, 1.0)
    mix = discord + loud_sound(len(discord))
    assert modem.decode_audio(mix, SR) == []  # the mix alone: lost
    got = hear(monkeypatch, {"speakers.monitor": mix, 7: discord}, [("your speakers", "speakers.monitor")],
               apps={7: "Firefox · Discord | @friend"})
    assert got == [("Firefox · Discord | @friend", "meet at the forge")]


def test_a_message_heard_two_ways_is_shown_once(monkeypatch):
    discord = pad(modem.encode_text("only once", volume=0.5))
    got = hear(monkeypatch, {"speakers.monitor": discord, 7: discord}, [("your speakers", "speakers.monitor")],
               apps={7: "Discord"})
    assert [text for _, text in got] == ["only once"]


def test_an_unreadable_screech_is_reported_once(monkeypatch):
    got = hear(monkeypatch, {"mic": pad(garbled_screech("lost on the way"))}, [("your mic", "mic")])
    assert got == [("your mic", None)]
    # ...but not when another input read it
    bad, good = pad(garbled_screech("lost on the way")), pad(modem.encode_text("lost on the way", volume=0.6))
    got = hear(monkeypatch, {"speakers.monitor": bad, 7: good}, [("your speakers", "speakers.monitor")],
               apps={7: "Discord"})
    assert got == [("Discord", "lost on the way")]


def test_decode_listens_to_the_mic_and_every_app_by_default(monkeypatch):
    # regression: plain `binharic decode` listened only to the mic, and a friend's
    # screech in Discord plays inside the user's earbuds, where the mic can't hear it
    from binharic import cli, pulse
    made = []

    class FakeIncoming:
        def __init__(self, sr, sources, apps=False, **kw):
            made.append((sources, apps))

        def start(self):
            pass

        def wait_flowing(self, timeout):
            return True

        def get(self, timeout):
            self.calls = getattr(self, "calls", 0) + 1
            if self.calls == 1:
                raise KeyboardInterrupt  # Ctrl+C while listening
            return []

        def close(self):
            pass
    monkeypatch.setattr(incoming, "Incoming", FakeIncoming)
    monkeypatch.setattr(pulse, "available", lambda: True)
    monkeypatch.setattr(pulse, "real_source", lambda: "bluez_input.earbuds")
    monkeypatch.setattr(pulse, "speakers_monitor", lambda: "bluez_output.earbuds.monitor")
    monkeypatch.setattr(pulse, "describe", lambda name, kind: "CMF Buds Pro 2")
    monkeypatch.setattr(pulse, "check_device", lambda name, kind: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)
    for argv, want in [(["decode"], ([("your mic, CMF Buds Pro 2", "bluez_input.earbuds"),
                                      ("your speakers", "bluez_output.earbuds.monitor")], True)),
                       (["decode", "--source", "speakers"], ([("your speakers", "bluez_output.earbuds.monitor")], True)),
                       (["decode", "--source", "binharic_mic"], ([("CMF Buds Pro 2", "binharic_mic")], False))]:
        made.clear()
        cli.cmd_decode(cli.build_parser().parse_args(argv))
        assert made == [want], argv


@pytest.mark.parametrize("name", ["binharic", "e2e-noise"])
def test_our_own_streams_are_not_tapped(monkeypatch, name):
    import json
    streams = [{"index": 3, "properties": {"application.name": name}},
               {"index": 5, "properties": {"application.name": "Firefox", "media.name": "Discord | @friend"}}]
    monkeypatch.setattr(incoming.pulse, "_pactl", lambda *a: json.dumps(streams))
    apps = incoming.playing_apps(ignore=("binharic",))
    assert apps[5] == "Firefox · Discord | @friend"
    assert (3 in apps) == (name != "binharic")


def test_apps_are_found_with_an_old_pactl_too(monkeypatch):
    import subprocess
    text = """Sink Input #42
\tDriver: protocol-native.c
\tProperties:
\t\tmedia.name = "Discord | @friend"
\t\tapplication.name = "Firefox"
Sink Input #43
\tProperties:
\t\tapplication.name = "binharic"
"""

    def pactl(*args):
        if args[0] == "-f":
            raise subprocess.CalledProcessError(1, "pactl")  # no JSON output
        return text
    monkeypatch.setattr(incoming.pulse, "_pactl", pactl)
    assert incoming.playing_apps() == {42: "Firefox · Discord | @friend"}
