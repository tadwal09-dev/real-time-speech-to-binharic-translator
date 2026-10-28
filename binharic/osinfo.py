"""Where are we running: Linux (PipeWire), Windows, or iSH on iOS."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"


def is_ish() -> bool:
    """iSH (iOS) is an emulated Alpine Linux with no access to the phone's
    microphone or speakers: only text in, WAV files out."""
    if WINDOWS:
        return False
    if os.environ.get("BINHARIC_ISH"):
        return True
    try:
        return "ish" in os.uname().release.lower() or Path("/proc/ish").exists()
    except Exception:
        return False


ISH = is_ish()


def runtime_dir() -> Path:
    if WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "binharic"
    else:
        base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/binharic-{os.getuid()}")
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_dir() -> Path:
    if WINDOWS:
        return Path(os.environ.get("APPDATA", Path.home())) / "binharic"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "binharic"


def load_config() -> dict:
    try:
        return json.loads((config_dir() / "config.json").read_text())
    except (OSError, ValueError):
        return {}


def save_config(**changes) -> Path:
    cfg = load_config()
    for k, v in changes.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    path = config_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    return path
