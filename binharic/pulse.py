"""PipeWire / PulseAudio helpers (through ``pactl``)."""

from __future__ import annotations

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
    if SINK not in sinks():
        _pactl("load-module", "module-null-sink", f"sink_name={SINK}",
               "sink_properties=device.description=Binharic_Output")
    if MIC not in sources():
        _pactl("load-module", "module-remap-source", f"master={SINK}.monitor", f"source_name={MIC}",
               "source_properties=device.description=Binharic_Mic")


def mic_down() -> int:
    n = 0
    for line in _pactl("list", "short", "modules").splitlines():
        if f"sink_name={SINK}" in line or f"source_name={MIC}" in line:
            _pactl("unload-module", line.split("\t")[0])
            n += 1
    return n


def default_sink_monitor() -> str | None:
    """Monitor of the speakers: decode what Discord etc. is playing to you."""
    try:
        name = _pactl("get-default-sink").strip()
    except (RuntimeError, subprocess.CalledProcessError, OSError):
        return None
    return f"{name}.monitor" if name else None


def resolve_sink(value: str | None) -> str | None:
    """``auto`` -> the Binharic virtual mic when it exists, else default output."""
    if value in (None, "", "auto"):
        return SINK if virtual_mic_exists() else None
    if value in ("default", "none"):
        return None
    return value


def resolve_source(value: str | None) -> str | None:
    if value in ("speakers", "monitor"):
        return default_sink_monitor()
    return value or None
