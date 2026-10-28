"""iSH (iOS) mode.

iSH emulates an x86 Linux and can't touch the iPhone's microphone or speaker,
and it's far too slow for Whisper. So the phone does those parts:

* speech in: the iOS keyboard's dictation (the mic key) types what you say
* sound out: each message becomes a WAV in ~/binharic, which you play from the
  Files app (iSH -> root -> binharic), or share/AirDrop
* decoding: record a screech with Voice Memos, save it to Files -> iSH, then
  `binharic decode --wav file.m4a` (m4a needs `apk add ffmpeg`)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import codec, modem

SR = 22050  # plenty for the tones, and quicker to synthesise in the emulator


def outbox() -> Path:
    d = Path.home() / "binharic"
    d.mkdir(parents=True, exist_ok=True)
    return d


def to_wav(text: str, mode: str = "binharic", key: str | None = None, speed: float = 1.0,
           decoys: int = 0) -> Path:
    from .audio import write_wav
    audio = modem.encode_text(text, mode, key=key, sr=SR, speed=speed, volume=0.8, decoys=decoys)
    # the Files app sorts by name: a counter that always sorts after the last
    # file (never "...-2.wav", which sorts before "....wav") and never overwrites
    stamp = time.strftime("%Y%m%d-%H%M%S")
    taken = sorted(p.name for p in outbox().glob(f"{stamp}-*.wav"))
    n = int(taken[-1][len(stamp) + 1:-4]) + 1 if taken and taken[-1][len(stamp) + 1:-4].isdigit() else 1
    path = outbox() / f"{stamp}-{n:02d}.wav"
    while path.exists():
        n += 1
        path = outbox() / f"{stamp}-{n:02d}.wav"
    write_wav(str(path), audio, SR)
    latest = outbox() / "latest.wav"
    shutil.copyfile(path, latest)
    return path


def readable(path: str) -> str:
    """WAV as is; anything else (Voice Memos m4a, mp3...) through ffmpeg."""
    if path.lower().endswith(".wav"):
        return path
    if not shutil.which("ffmpeg"):
        raise RuntimeError("that isn't a WAV: install ffmpeg to read it (apk add ffmpeg), "
                           "or export the recording as WAV")
    out = Path(tempfile.mkdtemp()) / "in.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-ac", "1", "-ar", str(SR),
                    "-sample_fmt", "s16", str(out)], check=True)
    return str(out)


def run(mode: str = "binharic", key: str | None = None, speed: float = 1.0, show: str = "hex",
        decoys: int = 0) -> None:
    print("binharic for iSH. Type, or tap the keyboard's mic key and talk; Enter sends.\n"
          f"Each line becomes a sound file in {outbox()} (Files app -> iSH -> root -> binharic;\n"
          "latest.wav is always the newest). Empty line or Ctrl+D quits.\n"
          + ("Privacy: encrypted + gibberish chirps.\n" if decoys else "Privacy: encrypted.\n" if key
             else "Privacy: UNENCRYPTED (add -k \"passphrase\" to encrypt).\n"))
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            return
        t = time.monotonic()
        path = to_wav(line, mode, key, speed, decoys)
        shown = codec.to_binary(line) if show == "binary" else codec.to_hex(line)
        print(f"  {shown}\n  -> {path.name} ({time.monotonic() - t:.1f}s)", file=sys.stdout)
