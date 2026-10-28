"""PortAudio helpers: device lists, the Windows virtual cable, and mic probing.

On Windows there is no PipeWire, so the "Binharic Mic" is VB-Audio's free
Virtual Cable: we play into "CABLE Input", and Discord records from
"CABLE Output" (https://vb-audio.com/Cable/).
"""

from __future__ import annotations

import numpy as np

CABLE_URL = "https://vb-audio.com/Cable/"


def _sd():
    import sounddevice as sd
    return sd


def available() -> bool:
    try:
        _sd().query_devices()
        return True
    except Exception:
        return False


def devices(kind: str) -> list[dict]:
    """[{index, name, hostapi}] of input or output devices."""
    sd = _sd()
    apis = sd.query_hostapis()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0:
            out.append({"index": i, "name": d["name"], "hostapi": apis[d["hostapi"]]["name"]})
    return out


def _is_cable(name: str) -> bool:
    n = name.lower()
    return "cable" in n and ("vb-audio" in n or "virtual" in n or n.startswith("cable"))


def find_cable() -> int | None:
    """Output index of 'CABLE Input (VB-Audio Virtual Cable)', if installed."""
    try:
        outs = devices("output")
    except Exception:
        return None
    # prefer the MME/WASAPI entry; any will do
    for d in outs:
        if _is_cable(d["name"]) and "input" in d["name"].lower():
            return d["index"]
    return None


def real_inputs() -> list[dict]:
    """Microphones, without the virtual cable's own end."""
    return [d for d in devices("input") if not _is_cable(d["name"])]


def resolve(device, kind: str):
    """A saved device (name or index) -> an index sounddevice accepts, or None."""
    if device is None or device == "":
        return None
    if isinstance(device, int) or str(device).isdigit():
        return int(device)
    for d in devices(kind):
        if d["name"] == device:
            return d["index"]
    for d in devices(kind):
        if str(device).lower() in d["name"].lower():
            return d["index"]
    raise RuntimeError(f"there is no {kind} device called '{device}' - run `binharic fix` to pick one")


def probe_input(device, seconds: float = 2.5, sr: int = 16000) -> float | None:
    """Record briefly; return the peak level (0..1), or None if it can't record."""
    sd = _sd()
    try:
        x = sd.rec(int(seconds * sr), samplerate=sr, channels=1, dtype="float32", device=device)
        sd.wait()
    except Exception:
        return None
    return float(np.abs(x).max())
