"""Hearing binharic that arrives from elsewhere: a friend's screech in Discord.

"What your PC plays" is decoded two ways at once (PipeWire/PulseAudio):

* the mix your speakers play (their monitor), which catches every sound from its
  first moment
* every app's playback on its own (``parec --monitor-stream``). Discord's screech
  then decodes even while music plays over it: in the mix, anything about as loud
  as the screech garbles it. An app's own stream also stays put when Bluetooth
  earbuds switch modes and their speakers are re-created.

A message heard both ways is shown once. A screech that was heard but couldn't be
read is reported, unless another way read it.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import codec, modem, pulse
from .audio import Input


@dataclass
class Heard:
    source: str                         # where: "your mic", "Firefox · Discord | @friend"...
    message: codec.Message | None       # None: a screech that couldn't be read
    at: float = 0.0


def playing_apps(ignore: tuple[str, ...] = ("binharic",)) -> dict[int, str]:
    """{stream index: "app · title"} of everything playing, except ``ignore``d apps."""
    try:
        streams = [(st["index"], st.get("properties", {}))
                   for st in json.loads(pulse._pactl("-f", "json", "list", "sink-inputs"))]
    except Exception:  # a pactl without -f json (older PulseAudio): read its text
        try:
            streams = _sink_inputs_text(pulse._pactl("list", "sink-inputs"))
        except Exception:  # the server is going away
            return {}
    out = {}
    for index, p in streams:
        app = p.get("application.name") or ""
        if app in ignore:
            continue
        out[index] = " · ".join(dict.fromkeys(filter(None, (app, p.get("media.name"))))) or "an app"
    return out


def _sink_inputs_text(text: str) -> list[tuple[int, dict]]:
    streams: list[tuple[int, dict]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Sink Input #"):
            streams.append((int(line.split("#", 1)[1]), {}))
        elif streams and " = " in line:
            k, _, v = line.partition(" = ")
            streams[-1][1][k.strip()] = v.strip().strip('"')
    return streams


class Incoming:
    """Decode several inputs at once, each with its own decoder; ``get`` merges them."""

    RESCAN = 1.0          # how often new apps' streams are looked for
    SAME = 3.0            # the same message within this many seconds is one message

    def __init__(self, sr: int, sources: list[tuple[str, str]], apps: bool = False, mode: str = "auto",
                 key: str | None = None, speed: float = 1.0, threshold: float = 0.45,
                 notify: Callable[[str], None] | None = None, ignore: tuple[str, ...] = ("binharic",)):
        self.sr = sr
        self.sources = sources          # [(label, PipeWire source name)]
        self.apps = apps
        self.decoder = dict(sr=sr, mode=mode, key=key, speed=speed, threshold=threshold)
        self.notify = notify or (lambda m: None)
        self.ignore = ignore
        self._q: queue.Queue[Heard] = queue.Queue()
        self._stop = threading.Event()
        self._inputs: list[Input] = []
        self._tapped: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()
        self._flowing = threading.Event()
        self._recent: list[tuple[float, tuple]] = []   # (when, message key) shown lately
        self._held: list[Heard] = []                    # unreadable screeches, held back a moment
        self._last_garbled = -1e9
        self._subscribe: subprocess.Popen | None = None

    # -- life cycle --------------------------------------------------------
    def start(self) -> None:
        """Open the fixed sources now (a wrong name fails here), then follow the apps."""
        opened = [(label, Input(self.sr, source=name, blocksize=self.sr // 10, notify=self.notify))
                  for label, name in self.sources]
        for label, inp in opened:
            self._inputs.append(inp)
            self._spawn(label, inp)
        if self.apps:
            threading.Thread(target=self._follow_apps, daemon=True, name="binharic-apps").start()

    def wait_flowing(self, timeout: float = 5.0) -> bool:
        return self._flowing.wait(timeout)

    def close(self) -> None:
        self._stop.set()
        if self._subscribe is not None and self._subscribe.poll() is None:
            self._subscribe.kill()
        with self._lock:
            inputs = list(self._inputs)
        for inp in inputs:
            try:
                inp.close()
            except Exception:
                pass

    # -- readers -----------------------------------------------------------
    def _spawn(self, label: str, inp: Input | None, index: int | None = None) -> threading.Thread:
        t = threading.Thread(target=self._read, args=(label, inp, index), daemon=True, name="binharic-incoming")
        t.start()
        return t

    def _read(self, label: str, inp: Input | None, index: int | None) -> None:
        dec = modem.StreamDecoder(**self.decoder)
        garbled = 0
        try:
            if inp is None:  # an app's stream: opened here, so several open at once (each checks 0.4 s)
                inp = Input(self.sr, app_stream=index, blocksize=self.sr // 10, notify=self.notify)
                with self._lock:
                    if self._stop.is_set():
                        inp.close()
                        return
                    self._inputs.append(inp)
            while not self._stop.is_set():
                try:
                    block = inp.read()
                except EOFError:
                    break
                self._flowing.set()
                for m in dec.feed(block):
                    self._q.put(Heard(label, m, time.monotonic()))
                if dec.garbled > garbled:
                    garbled = dec.garbled
                    self._q.put(Heard(label, None, time.monotonic()))
        except Exception as e:  # e.g. the sound server never came back
            if not self._stop.is_set() and index is None:
                self.notify(f"stopped listening to {label}: {e}")
        finally:
            for m in dec.flush():
                self._q.put(Heard(label, m, time.monotonic()))
            if index is not None:
                with self._lock:
                    self._tapped.pop(index, None)
                    if inp in self._inputs:
                        self._inputs.remove(inp)
                if inp is not None:
                    try:
                        inp.close()
                    except Exception:
                        pass

    def _follow_apps(self) -> None:
        """Tap every app's stream; a new one within moments of it appearing (`pactl subscribe`),
        so a screech at its very start isn't missed. A rescan every second backs that up."""
        wake = threading.Event()
        threading.Thread(target=self._new_streams, args=(wake,), daemon=True, name="binharic-apps-events").start()
        while not self._stop.is_set():
            for index, label in playing_apps(self.ignore).items():
                with self._lock:
                    if self._stop.is_set():
                        return
                    if index not in self._tapped:
                        self._tapped[index] = self._spawn(label, None, index)
            wake.wait(self.RESCAN)
            wake.clear()

    def _new_streams(self, wake: threading.Event) -> None:
        try:
            proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, start_new_session=True)
        except OSError:
            return
        self._subscribe = proc
        for line in proc.stdout:
            if self._stop.is_set():
                break
            if "'new' on sink-input" in line:
                wake.set()
        proc.kill()

    # -- results -----------------------------------------------------------
    def get(self, timeout: float = 0.2) -> list[Heard]:
        """What was heard since the last call: each message once (from whichever input
        read it first), and unreadable screeches that no input could read."""
        out: list[Heard] = []
        try:
            items = [self._q.get(timeout=timeout)]
        except queue.Empty:
            items = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        now = time.monotonic()
        self._recent = [(t, k) for t, k in self._recent if now - t < 2 * self.SAME]
        for h in items:
            if h.message is None:
                self._held.append(h)
                continue
            k = (h.message.mode_id, h.message.encrypted, h.message.raw)
            if any(key == k and abs(h.at - t) < self.SAME for t, key in self._recent):
                continue  # the same screech, read from another input
            self._recent.append((h.at, k))
            out.append(h)
        # an unreadable screech: wait to see whether another input read it after all
        keep = []
        for h in self._held:
            if now - h.at < self.SAME:
                keep.append(h)
            elif not any(abs(h.at - t) < self.SAME for t, _ in self._recent) and h.at - self._last_garbled > self.SAME:
                self._last_garbled = h.at
                out.append(h)
        self._held = keep
        return out
