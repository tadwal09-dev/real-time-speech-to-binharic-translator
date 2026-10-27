"""PipeWire / PulseAudio helpers (through ``pactl``)."""

from __future__ import annotations

import json
import shutil
import subprocess

SINK = "binharic_out"
MIC = "binharic_mic"


def _pactl(*args: str) -> str:
    if not shutil.which("pactl"):
        raise RuntimeError("pactl not found - sudo pacman -S libpulse pipewire-pulse")
    return subprocess.run(["pactl", *args], check=True, capture_output=True, text=True).stdout


def _names(kind: str) -> list[str]:
    return [line.split("\t")[1] for line in _pactl("list", "short", kind).splitlines() if "\t" in line]


def sinks() -> list[str]:
    return _names("sinks")


def sources() -> list[str]:
    return _names("sources")


def available() -> bool:
    try:
        _pactl("info")
        return True
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        return False


def virtual_mic_exists() -> bool:
    try:
        return SINK in sinks() and MIC in sources()
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        return False


def mic_up() -> None:
    before_sink, before_source = _default("sink"), _default("source")
    if SINK not in sinks():
        _pactl("load-module", "module-null-sink", f"sink_name={SINK}",
               "sink_properties=device.description=Binharic_Output")
    if MIC not in sources():
        _pactl("load-module", "module-remap-source", f"master={SINK}.monitor", f"source_name={MIC}",
               "source_properties=device.description=Binharic_Mic")
    # PipeWire can make a brand-new device the default. Then all your other
    # sound would vanish into the virtual sink (or apps would record from the
    # Binharic Mic): put the previous defaults back.
    try:
        if before_sink and before_sink != SINK and _default("sink") == SINK:
            _pactl("set-default-sink", before_sink)
        if before_source and before_source not in (MIC, f"{SINK}.monitor") and \
                _default("source") in (MIC, f"{SINK}.monitor"):
            _pactl("set-default-source", before_source)
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        pass


def mic_down() -> int:
    n = 0
    for line in _pactl("list", "short", "modules").splitlines():
        if f"sink_name={SINK}" in line or f"source_name={MIC}" in line:
            _pactl("unload-module", line.split("\t")[0])
            n += 1
    return n


def speakers_monitor() -> str | None:
    """Monitor of your real speakers: decode what Discord etc. plays to you.
    (Never the virtual sink's monitor, which is just our own screech.)"""
    sink = real_sink()
    return f"{sink}.monitor" if sink else None


def resolve_sink(value: str | None) -> str | None:
    """``auto`` -> the Binharic virtual mic when it exists, else default output."""
    if value in (None, "", "auto"):
        return SINK if virtual_mic_exists() else None
    if value in ("default", "none"):
        return None
    return value


def resolve_source(value: str | None) -> str | None:
    if value in ("speakers", "monitor"):
        return speakers_monitor()
    return value or None


def check_device(name: str, kind: str) -> None:
    """Raise a helpful error if a sink/source name doesn't exist."""
    try:
        names = sinks() if kind == "sinks" else sources()
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        return  # can't tell; pacat/parec will report it
    if name not in names:
        what = "output (sink)" if kind == "sinks" else "input (source)"
        listing = ", ".join(names) or "none"
        raise RuntimeError(f"there is no {what} called '{name}'. Available: {listing}. "
                           "See `binharic devices`.")


def _info(kind: str) -> list[dict]:
    """[{name, description}] for sinks or sources."""
    try:
        data = json.loads(_pactl("-f", "json", "list", kind))
        return [{"name": d.get("name", ""), "description": d.get("description", d.get("name", ""))} for d in data]
    except Exception:
        try:
            return [{"name": n, "description": n} for n in _names(kind)]
        except Exception:
            return []


def describe(name: str | None, kind: str) -> str:
    for d in _info(kind):
        if d["name"] == name:
            return d["description"]
    return name or "default"


def _default(kind: str) -> str | None:
    try:
        return _pactl(f"get-default-{kind}").strip() or None
    except Exception:
        return None


def real_source() -> str | None:
    """Your actual microphone: the default source, unless that is our own
    virtual mic or a monitor (PipeWire may switch the default to a newly
    created device), in which case the first real input."""
    if not available():
        return None
    ours = lambda n: n == MIC or n.endswith(".monitor") or n.startswith(SINK)  # noqa: E731
    d = _default("source")
    if d and not ours(d):
        return d
    return _best([s["name"] for s in _info("sources") if not ours(s["name"])], ("alsa_input", "bluez_input"))


def _best(names: list[str], hardware: tuple[str, ...]) -> str | None:
    """Prefer real hardware over other virtual devices."""
    for n in names:
        if n.startswith(hardware):
            return n
    return names[0] if names else None


def real_sink() -> str | None:
    """Your speakers/headphones: the default sink unless it is our virtual one."""
    if not available():
        return None
    d = _default("sink")
    if d and d != SINK:
        return d
    return _best([s["name"] for s in _info("sinks") if s["name"] != SINK], ("alsa_output", "bluez_output"))


def probe_source(name: str, seconds: float = 1.0, rate: int = 16000) -> float | None:
    """Record briefly and return the peak level (0..1), or None if recording failed."""
    if not shutil.which("parec"):
        return None
    import time
    try:
        p = subprocess.Popen(["parec", f"--device={name}", "--raw", "--format=s16le", f"--rate={rate}", "--latency-msec=20",
                              "--channels=1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    time.sleep(seconds)
    p.terminate()
    raw, _ = p.communicate(timeout=3)
    if len(raw) < 2:
        return None
    import numpy as np
    x = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2")
    return float(np.abs(x).max()) / 32768.0
