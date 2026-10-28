"""Plain terminal front end (``binharic listen``): a colourful running log."""

from __future__ import annotations

import sys
import threading
import time

from .core import Translator
from .stt import LIVE

try:
    from rich.console import Console
    from rich.markup import escape as _esc
    from rich.text import Text
except ImportError:  # rich is optional; fall back to plain prints
    Console = None

    def _esc(text: str) -> str:
        return text

BANNER = r"""
  ┌─┐┬┌┐┌┬ ┬┌─┐┬─┐┬┌─┐
  ├┴┐││││├─┤├─┤├┬┘││     01010000 01110010 01100001 01101001 01110011 01100101
  └─┘┴┘└┘┴ ┴┴ ┴┴└─┴└─┘   the flesh is weak · the machine speaks
"""


class Printer:
    def __init__(self, show: str = "text", test: bool = False):
        self.show = show
        self.test = test
        self.console = Console(highlight=False) if Console else None
        self._partial = ""
        self._lock = threading.Lock()
        self._meter_at = 0.0
        self._speaking = False
        self._tty = sys.stdout.isatty()

    def _out(self, markup: str, plain: str):
        with self._lock:
            self._clear_partial()
            if self.console:
                self.console.print(markup)
            else:
                print(plain, flush=True)

    def _clear_partial(self):
        if self._partial:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self._partial = ""

    def _meter(self, level: float):
        """In-place mic meter, so you can see whether you're being heard."""
        now = time.monotonic()
        if now - self._meter_at < 0.1:
            return
        self._meter_at = now
        with self._lock:
            if self._partial and self._partial != "\0meter":
                return  # a live transcript is showing; leave it
            n = int(min(1.0, (level * 12) ** 0.6) * 24)
            state = "\033[33mhearing you…\033[0m" if self._speaking else "\033[2msay something\033[0m"
            sys.stdout.write(f"\r\033[K  mic \033[32m{'█' * n}\033[2m{'░' * (24 - n)}\033[0m {state}")
            sys.stdout.flush()
            self._partial = "\0meter"

    def _timing(self, ev: dict):
        if "latency_ms" not in ev:
            self._out(f"           [dim]synth {ev['synth_ms']:.0f} ms[/]", f"  synth {ev['synth_ms']:.0f} ms")
            return
        lat = ev["latency_ms"]
        colour = "green" if lat < 900 else "yellow" if lat < 1800 else "red"
        spec = " (started early)" if ev.get("speculative") else ""
        self._out(f"           [{colour}]⏱ screech started {lat:.0f} ms after you stopped talking[/] "
                  f"[dim]· recognition {ev['stt_ms']:.0f} ms{spec} · synth {ev['synth_ms']:.0f} ms[/]",
                  f"  latency {lat:.0f} ms (recognition {ev['stt_ms']:.0f} ms, synth {ev['synth_ms']:.0f} ms)")

    def banner(self):
        if self.console:
            self.console.print(Text(BANNER, style="bold red"))
        else:
            print(BANNER)

    def __call__(self, kind: str, ev: dict):
        t = time.strftime("%H:%M:%S")
        if kind == "log":
            style = {"warn": "yellow", "error": "bold red"}.get(ev.get("level"), "cyan")
            self._out(f"[dim]{t}[/] [{style}]⚙ {_esc(ev['text'])}[/]", f"{t} * {ev['text']}")
        elif kind == "error":
            self._out(f"[dim]{t}[/] [bold red]✖ {_esc(ev['text'])}[/]", f"{t} ! {ev['text']}")
        elif kind == "speech":
            self._speaking = ev["speaking"]
        elif kind == "level" and self.test and self._tty:
            self._meter(ev["level"])
        elif kind == "partial":
            if not self._tty:
                return  # an in-place line only makes sense on a terminal, not in a log/journal
            with self._lock:
                text = ev["text"]
                if not text:
                    self._clear_partial()
                    return
                sys.stdout.write("\r\033[K\033[2m  … " + text[-100:] + "\033[0m")
                sys.stdout.flush()
                self._partial = text
        elif kind == "sent":
            shown = {"hex": ev["hex"], "binary": ev["binary"]}.get(self.show, ev["text"])
            priv = {"plain": "[yellow]unencrypted[/]", "encrypted": "[green]encrypted[/]",
                    "decoys": f"[green]encrypted +{ev.get('decoys', 0)} decoys[/]"}.get(ev.get("privacy", "plain"), "")
            self._out(f"[dim]{t}[/] [bold red]▲ {ev['mode']}[/] {_esc(shown)} [dim]({ev['seconds']:.1f}s ·[/] {priv}[dim])[/]",
                      f"{t} >> {shown}  ({ev.get('privacy', 'plain')})")
            if ev.get("test") or self.test:
                self._timing(ev)
        elif kind == "verify":
            via = "over the air" if ev["via"] == "air" else "round trip"
            if ev["ok"]:
                extra = f" in {ev['decode_ms']:.0f} ms" if ev["decode_ms"] else ""
                self._out(f"           [bold green]✔ {via} OK[/][dim]{extra}:[/] {_esc(ev['decoded'])}",
                          f"  OK {via}: {ev['decoded']}")
            else:
                self._out(f"           [bold red]✖ {via} FAILED[/] [dim]got:[/] {_esc(str(ev['decoded']))}",
                          f"  FAILED {via}: {ev['decoded']}")
        elif kind == "received":
            style = "yellow" if ev["locked"] else "bold green"
            self._out(f"[dim]{t}[/] [{style}]▼ {_esc(ev['text'])}[/]", f"{t} << {ev['text']}")


def describe(tr: Translator) -> str:
    s = tr.s
    parts = [
        "ON" if s.enabled else "OFF",
        "send: as you talk" if s.send_mode == LIVE else "send: after you finish",
        f"mode: {s.mode}",
        {"plain": "UNENCRYPTED", "encrypted": "encrypted", "decoys": f"encrypted + {s.decoys} decoys"}[s.privacy],
        f"out: {s.sink or 'default output'}",
    ]
    if s.passthrough:
        parts.append("passthrough when OFF")
    if s.hear:
        parts.append(f"hearing: {s.hear}")
    return " · ".join(parts)


def run(tr: Translator, show: str = "text", test: bool = False) -> None:
    p = Printer(show, test)
    p.banner()
    tr.subscribe(p)
    tr.start()
    if test:
        p("log", {"text": "TEST MODE · nothing goes to the Binharic Mic · the screech plays on your "
                          "speakers and is decoded back" + (" (and through the mic)" if tr.s.test_air else ""),
                  "level": "warn"})
        p("log", {"text": f"engine: {tr.s.engine} {tr.s.model or ''} · pause {tr.s.end_ms} ms · "
                          "wait for 'ready', then say something · Ctrl+C quits", "level": "info"})
    else:
        p("log", {"text": describe(tr), "level": "info"})
        p("log", {"text": "hotkeys: `binharic toggle` · `binharic send toggle` · Ctrl+C quits", "level": "info"})
    try:
        tr.wait()
    except KeyboardInterrupt:
        pass
    finally:
        tr.stop()
