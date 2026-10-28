"""Windows / iSH code paths and the `binharic fix` helpers, tested on Linux with fakes."""

import hashlib
import http.server
import sys
import threading
import time
import types

import numpy as np
import pytest

from binharic import cli, core, fix, modem, osinfo, pa


# -- Windows: control socket over localhost TCP ----------------------------------

def test_control_works_without_unix_sockets(monkeypatch, tmp_path):
    from tests.test_pipeline import FakeIn, FakeOut, ScriptedEngine
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(core, "UNIX_SOCKETS", False)  # what Windows Python gives us
    monkeypatch.setattr(core, "Input", FakeIn(np.zeros(48000, np.float32)))
    monkeypatch.setattr(core, "Output", FakeOut())
    monkeypatch.setattr(core, "make_engine", lambda *a, **k: ScriptedEngine([], ""))
    tr = core.Translator(core.Settings())
    tr.start()
    try:
        assert (tmp_path / "binharic.port").exists()
        assert '"enabled": false' in core.send_command("off")
        with pytest.raises(RuntimeError, match="already running"):
            core.Translator(core.Settings()).start()
    finally:
        tr.stop()
    assert not (tmp_path / "binharic.port").exists()
    with pytest.raises(RuntimeError, match="not running"):
        core.send_command("status")


# -- Windows: VB-Audio Virtual Cable ------------------------------------------------

def fake_sounddevice(monkeypatch, names):
    devs = [{"name": n, "hostapi": 0, "max_input_channels": i, "max_output_channels": o} for n, i, o in names]
    sd = types.SimpleNamespace(query_devices=lambda: devs, query_hostapis=lambda: [{"name": "MME"}])
    monkeypatch.setitem(sys.modules, "sounddevice", sd)


WIN_DEVICES = [("Microphone (Realtek Audio)", 2, 0), ("Speakers (Realtek Audio)", 0, 2),
               ("CABLE Output (VB-Audio Virtual Cable)", 2, 0), ("CABLE Input (VB-Audio Virtual Cable)", 0, 2)]


def test_find_the_virtual_cable(monkeypatch):
    fake_sounddevice(monkeypatch, WIN_DEVICES)
    assert pa.find_cable() == 3
    assert [d["name"] for d in pa.real_inputs()] == ["Microphone (Realtek Audio)"]
    assert pa.resolve("Microphone (Realtek Audio)", "input") == 0
    with pytest.raises(RuntimeError, match="binharic fix"):
        pa.resolve("No Such Mic", "input")


def test_windows_routes_the_screech_into_the_cable(monkeypatch, tmp_path):
    from binharic import pulse
    fake_sounddevice(monkeypatch, WIN_DEVICES)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(pulse, "available", lambda: False)
    tr = core.Translator(core.Settings(control_socket=False, input_device="Microphone (Realtek Audio)", test=True))
    tr._resolve_devices()
    assert tr.virtual and tr.s.output_device == 3 and tr.s.input_device == 0
    assert tr._speaker_device is None  # test mode plays on the default speakers, not the cable
    assert tr.full_duplex is False      # (test mode always half duplex)


# -- iSH -------------------------------------------------------------------------

def test_ish_writes_one_file_per_message(monkeypatch, tmp_path):
    from binharic import ish
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = [ish.to_wav(f"message {i}") for i in range(3)]  # same second
    assert len(set(paths)) == 3
    from binharic.audio import read_wav
    x, sr = read_wav(str(tmp_path / "binharic" / "latest.wav"))
    assert [m.text for m in modem.decode_audio(x, sr)] == ["message 2"]


def test_plain_output_drops_style_tags(monkeypatch, capsys):
    monkeypatch.setattr(cli, "console", None)  # no rich, as on iSH
    cli.say_line("[green]wrote[/] file.wav [dim](1.0s)[/] [bold red]x[/]")
    assert capsys.readouterr().out.strip() == "wrote file.wav (1.0s) x"


# -- fix: resumable downloads ---------------------------------------------------------

class FlakyHandler(http.server.BaseHTTPRequestHandler):
    """Serves a file with Range support, but drops every connection after 64 KB."""
    data = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
        body = self.data[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body[:64 * 1024])  # ...then the "connection reset"


def test_wheel_download_resumes_after_drops(monkeypatch, tmp_path):
    data = np.random.default_rng(0).bytes(300 * 1024)
    FlakyHandler.data = data
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(fix, "wheel_dir", lambda: tmp_path)
    monkeypatch.setattr(fix, "_pick_wheel", lambda pkg, prefix: {
        "filename": "nvidia_x-1-py3-none-manylinux_x86_64.whl", "size": len(data),
        "url": f"http://127.0.0.1:{srv.server_address[1]}/w.whl",
        "digests": {"sha256": hashlib.sha256(data).hexdigest()}})
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = fix.download_wheel("nvidia-x", "1.", say=lambda m: None)
    srv.shutdown()
    assert out.read_bytes() == data


# -- fix: Bluetooth headset mode ---------------------------------------------------------

def test_bluetooth_in_music_mode_is_offered_headset_mode(monkeypatch):
    f = fix.Fixer(yes=True, out=lambda m: None)
    monkeypatch.setattr(f, "_cards", lambda: [
        {"name": "bluez_card.AA_BB", "active_profile": "a2dp-sink",
         "profiles": {"a2dp-sink": {}, "headset-head-unit": {}, "headset-head-unit-msbc": {}, "off": {}}},
        {"name": "alsa_card.pci", "active_profile": "output:analog-stereo", "profiles": {}},
        {"name": "bluez_card.CC", "active_profile": "headset-head-unit", "profiles": {"headset-head-unit": {}}},
    ])
    assert f._bluetooth_headset_profiles() == [("bluez_card.AA_BB", "a2dp-sink", "headset-head-unit-msbc")]


def test_saved_mic_is_used_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    osinfo.save_config(source="alsa_input.usb-Mic")
    st = cli._settings(cli.build_parser().parse_args(["listen"]))
    assert st.source == "alsa_input.usb-Mic"
    st = cli._settings(cli.build_parser().parse_args(["listen", "--source", "other"]))
    assert st.source == "other"
