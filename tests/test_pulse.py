import json
import os
import stat
import sys

import numpy as np
import pytest

from binharic import core, pulse

SOURCES = [
    {"name": "binharic_out.monitor", "description": "Monitor of Binharic_Output"},
    {"name": "binharic_mic", "description": "Binharic_Mic"},
    {"name": "alsa_output.pci.analog-stereo.monitor", "description": "Monitor of Built-in Audio"},
    {"name": "alsa_input.usb-Blue_Yeti.analog-stereo", "description": "Yeti Stereo Microphone"},
]
SINKS = [
    {"name": "binharic_out", "description": "Binharic_Output"},
    {"name": "alsa_output.pci.analog-stereo", "description": "Built-in Audio Analog Stereo"},
]


def fake_pactl(tmp_path, monkeypatch, default_source, default_sink):
    if sys.platform == "win32":
        pytest.skip("fakes pactl with a shell script; PipeWire is Linux only")
    script = tmp_path / "pactl"
    script.write_text(f"""#!/bin/sh
case "$*" in
  info) echo ok ;;
  "get-default-source") echo {default_source} ;;
  "get-default-sink") echo {default_sink} ;;
  "-f json list sources") echo '{json.dumps(SOURCES)}' ;;
  "-f json list sinks") echo '{json.dumps(SINKS)}' ;;
  "list short sources") printf '1\\tbinharic_mic\\n' ;;
  "list short sinks") printf '1\\tbinharic_out\\n' ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")


def test_never_listens_to_our_own_virtual_mic(tmp_path, monkeypatch):
    # PipeWire made the freshly created virtual devices the defaults
    fake_pactl(tmp_path, monkeypatch, "binharic_mic", "binharic_out")
    assert pulse.real_source() == "alsa_input.usb-Blue_Yeti.analog-stereo"
    assert pulse.real_sink() == "alsa_output.pci.analog-stereo"
    assert pulse.describe(pulse.real_source(), "sources") == "Yeti Stereo Microphone"


def test_keeps_a_normal_default(tmp_path, monkeypatch):
    fake_pactl(tmp_path, monkeypatch, "alsa_input.usb-Blue_Yeti.analog-stereo", "alsa_output.pci.analog-stereo")
    assert pulse.real_source() == "alsa_input.usb-Blue_Yeti.analog-stereo"
    assert pulse.real_sink() == "alsa_output.pci.analog-stereo"


def test_silent_mic_is_reported(monkeypatch, tmp_path):
    from tests.test_pipeline import FakeIn, FakeOut, ScriptedEngine
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(core, "Input", FakeIn(np.zeros(48000 * 30, np.float32)))  # muted mic: pure zeros
    monkeypatch.setattr(core, "Output", FakeOut())
    monkeypatch.setattr(core, "make_engine", lambda *a, **k: ScriptedEngine([], ""))
    import time
    tr = core.Translator(core.Settings(control_socket=False))
    logs = []
    tr.subscribe(lambda kind, ev: kind == "log" and logs.append((ev.get("level"), ev["text"])))
    tr.start()
    deadline = time.time() + 12
    while time.time() < deadline and not any("silent" in t for _, t in logs):
        time.sleep(0.1)
    tr.stop()
    assert any(level == "error" and "silent" in t for level, t in logs)


def test_mic_down_removes_the_source_before_the_sink(monkeypatch):
    # regression: unloading the null sink first made PulseAudio drop the remap
    # source with it, and `binharic mic down` then failed unloading that
    import subprocess
    mods = {"6": "module-null-sink\tsink_name=binharic_out", "7": "module-remap-source\tsource_name=binharic_mic"}

    def pactl(*args):
        if args[:3] == ("list", "short", "modules"):
            return "".join(f"{k}\t{v}\n" for k, v in mods.items())
        if args[0] == "unload-module":
            if args[1] not in mods:
                raise subprocess.CalledProcessError(1, "pactl")
            if args[1] == "6":
                mods.pop("7", None)  # the sink takes its remap source with it
            mods.pop(args[1])
            return ""
        raise AssertionError(args)
    monkeypatch.setattr(pulse, "_pactl", pactl)
    assert pulse.mic_down() == 2 and not mods
