"""The live layer: mic -> speech recognition -> binharic -> output.

While ON, your voice never reaches the output; only the encoded screech
does. While OFF, nothing is sent, or (with --passthrough) your normal voice
is passed straight through, so the virtual mic can stay selected in
Discord/Mumble/etc. Toggle with ``binharic toggle`` (bind it to a hotkey).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

from . import codec, modem
from .audio import Input, Output

SR = 48000
DEFAULT_MODEL = "vosk-model-small-en-us-0.15"
MODEL_URL = "https://alphacephei.com/vosk/models/{name}.zip"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "binharic"


def pidfile() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/binharic-{os.getuid()}"
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base) / "binharic.pid"


def download_model(name: str = DEFAULT_MODEL) -> Path:
    dest = data_dir() / "models"
    target = dest / name
    if target.exists():
        print(f"model already present: {target}")
        return target
    dest.mkdir(parents=True, exist_ok=True)
    url = MODEL_URL.format(name=name)
    zpath = dest / f"{name}.zip"
    print(f"downloading {url} ...")
    urllib.request.urlretrieve(url, zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest)
    zpath.unlink()
    print(f"model installed: {target}")
    return target


def resolve_model(path: str | None) -> Path:
    if path:
        return Path(path).expanduser()
    models = data_dir() / "models"
    found = sorted(models.glob("vosk-model*")) if models.exists() else []
    if not found:
        raise SystemExit("no speech model found - run `binharic download-model` first (or pass --model)")
    return found[0]


def notify(msg: str) -> None:
    print(f"[binharic] {msg}", flush=True)
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "binharic", "-t", "1500", "Binharic", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def send_signal(sig: int) -> None:
    pf = pidfile()
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, sig)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        raise SystemExit("binharic listen is not running")


class Layer:
    def __init__(self, args):
        self.args = args
        self.enabled = threading.Event()
        if not args.start_off:
            self.enabled.set()
        self.messages: queue.Queue[np.ndarray] = queue.Queue()
        self.passthrough: queue.Queue[np.ndarray] = queue.Queue(maxsize=20)
        self.speaking = threading.Event()
        self.stop = threading.Event()

    # -- control ----------------------------------------------------------
    def toggle(self, *_):
        self.set_enabled(not self.enabled.is_set())

    def set_enabled(self, on: bool):
        if on:
            self.enabled.set()
        else:
            self.enabled.clear()
        notify("ON - speaking binharic" if on else "OFF")

    # -- output thread ------------------------------------------------------
    def _output_loop(self, out: Output):
        silence = np.zeros(out.blocksize, dtype=np.float32)
        while not self.stop.is_set():
            try:
                audio = self.messages.get_nowait()
            except queue.Empty:
                audio = None
            if audio is not None:
                self.speaking.set()
                for i in range(0, len(audio), out.blocksize):
                    out.write(audio[i:i + out.blocksize])
                    if self.stop.is_set():
                        break
                self.speaking.clear()
                continue
            try:
                out.write(self.passthrough.get_nowait())
            except queue.Empty:
                out.write(silence)

    # -- main -------------------------------------------------------------
    def run(self):
        import vosk

        args = self.args
        vosk.SetLogLevel(-1)
        model = vosk.Model(str(resolve_model(args.model)))
        rec = vosk.KaldiRecognizer(model, SR)

        out = Output(SR, sink=args.sink, device=args.output_device)
        mic = Input(SR, source=args.source, device=args.input_device, blocksize=SR // 10)
        threading.Thread(target=self._output_loop, args=(out,), daemon=True).start()

        signal.signal(signal.SIGUSR1, self.toggle)
        signal.signal(signal.SIGUSR2, lambda *_: self.set_enabled(False))
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        pidfile().write_text(str(os.getpid()))

        where = f"sink {args.sink}" if args.sink else "default output"
        notify(f"listening ({args.mode}{', keyed' if args.key else ''}) -> {where}; "
               f"{'ON' if self.enabled.is_set() else 'OFF'}")
        last_partial = ""
        try:
            while not self.stop.is_set():
                block = mic.read()
                if self.speaking.is_set() and not args.full_duplex:
                    continue  # don't let the mic hear our own screeching
                if not self.enabled.is_set():
                    if args.passthrough:
                        try:
                            self.passthrough.put_nowait(block)
                        except queue.Full:
                            pass
                    continue
                pcm = (np.clip(block, -1, 1) * 32767).astype("<i2").tobytes()
                if rec.AcceptWaveform(pcm):
                    text = json.loads(rec.Result()).get("text", "").strip()
                    if last_partial:
                        sys.stdout.write("\r\033[K")
                        last_partial = ""
                    if text:
                        self.speak(text)
                elif args.verbose:
                    partial = json.loads(rec.PartialResult()).get("partial", "")
                    if partial and partial != last_partial:
                        sys.stdout.write(f"\r\033[K  ... {partial}")
                        sys.stdout.flush()
                        last_partial = partial
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            time.sleep(0.1)
            mic.close()
            out.close()
            try:
                pidfile().unlink()
            except FileNotFoundError:
                pass

    def speak(self, text: str):
        a = self.args
        if a.show == "hex":
            shown = codec.to_hex(text)
        elif a.show == "binary":
            shown = codec.to_binary(text)
        else:
            shown = text
        print(f"++ {shown}", flush=True)
        audio = modem.encode_text(text, a.mode, key=a.key, sr=SR, speed=a.speed, volume=a.volume)
        self.messages.put(audio)


def run(args) -> None:
    Layer(args).run()
