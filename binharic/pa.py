"""PortAudio helpers: device lists, the Windows virtual cable, and mic probing.

On Windows there is no PipeWire, so the "Binharic Mic" is VB-Audio's free
Virtual Cable: we play into "CABLE Input", and Discord records from
"CABLE Output" (https://vb-audio.com/Cable/).
"""

from __future__ import annotations

import re

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
    """Any virtual cable (VB-Audio's Virtual Cable, Hi-Fi Cable, ...): never a real mic."""
    n = name.lower()
    return "cable" in n and ("vb-audio" in n or "virtual" in n or n.startswith("cable"))


# VB-Audio Virtual Cable itself ("CABLE Input" / "CABLE Output", or the A/B/C/D
# extra cables), not their other products: Hi-Fi Cable, Voicemeeter, "CABLE In 16ch"
_VB_CABLE = re.compile(r"^cable(-[a-d])? (input|output)\b", re.I)


def _vb_cable(kind: str) -> list[dict]:
    """VB-Cable's ends: "output" devices are where we play (CABLE Input), "input"
    devices are what Discord records (CABLE Output). MME first: its shared mode
    lets Discord and binharic use the cable at the same time."""
    end = "input" if kind == "output" else "output"
    found = [d for d in devices(kind) if (m := _VB_CABLE.match(d["name"])) and m.group(2).lower() == end]
    found.sort(key=lambda d: d["hostapi"] != "MME")
    return found


def find_cable() -> int | None:
    """Output index of 'CABLE Input (VB-Audio Virtual Cable)', if installed."""
    try:
        outs = _vb_cable("output")
    except Exception:
        return None
    return outs[0]["index"] if outs else None


def find_cable_output() -> int | None:
    """Input index of 'CABLE Output': what Discord records the screech from."""
    try:
        ins = _vb_cable("input")
    except Exception:
        return None
    return ins[0]["index"] if ins else None


def real_inputs() -> list[dict]:
    """Microphones, without the virtual cable's own end."""
    return [d for d in devices("input") if not _is_cable(d["name"])]


def resolve(device, kind: str):
    """A saved device (name or index) -> an index sounddevice accepts, or None.
    Windows lists every device once per audio API (and MME cuts names to 31
    characters), so a name matches the MME entry first, then any other."""
    if device is None or device == "":
        return None
    if isinstance(device, int) or str(device).isdigit():
        return int(device)
    want = str(device).lower()
    found = devices(kind)
    found.sort(key=lambda d: d["hostapi"] != "MME")
    for test in (lambda n: n == want, lambda n: n.startswith(want) or (len(n) >= 30 and want.startswith(n)),  # MME cut it short
                 lambda n: want in n):
        for d in found:
            if test(d["name"].lower()):
                return d["index"]
    raise RuntimeError(f"there is no {kind} device called '{device}' - run `binharic devices` to list them, "
                       "or `binharic fix` to pick one")


def probe_input(device, seconds: float = 2.5, sr: int = 16000) -> float | None:
    """Record briefly; return the peak level (0..1), or None if it can't record."""
    sd = _sd()
    try:
        x = sd.rec(int(seconds * sr), samplerate=sr, channels=1, dtype="float32", device=device)
        sd.wait()
    except Exception:
        return None
    return float(np.abs(x).max())
