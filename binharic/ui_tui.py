"""Full-screen terminal dashboard (``binharic tui``), curses only."""

from __future__ import annotations

import curses
import locale
import queue
import time
from collections import deque

from . import modem
from .core import Translator
from .stt import LIVE

KEYS = [("space", "on/off"), ("l", "finish ⇄ live"), ("x", "test"), ("m", "mode"), ("k", "key"),
        ("c", "privacy"), ("t", "type"), ("p", "pass"), ("+/-", "speed"), ("q", "quit")]


class Dashboard:
    def __init__(self, tr: Translator):
        self.tr = tr
        self.events: queue.Queue = queue.Queue()
        self.log: deque[tuple[str, str, str]] = deque(maxlen=300)  # (time, kind, text)
        self.partial = ""
        self.level = 0.0
        self.peak = 0.0
        tr.subscribe(lambda k, e: self.events.put((k, e)))

    # -- event pump --------------------------------------------------------
    def pump(self):
        while True:
            try:
                kind, ev = self.events.get_nowait()
            except queue.Empty:
                return
            t = time.strftime("%H:%M:%S")
            if kind == "level":
                self.level = ev["level"]
            elif kind == "partial":
                self.partial = ev["text"]
            elif kind == "sent":
                self.log.append((t, "sent", f"{ev['text']}   ·  {ev['hex']}"))
                if ev.get("test") and "latency_ms" in ev:
                    self.log.append((t, "info", f"⏱ {ev['latency_ms']:.0f} ms after you stopped · recognition "
                                                f"{ev['stt_ms']:.0f} ms · synth {ev['synth_ms']:.0f} ms"))
            elif kind == "verify":
                via = "over the air" if ev["via"] == "air" else "round trip"
                self.log.append((t, "recv" if ev["ok"] else "error",
                                 f"{'✔' if ev['ok'] else '✖'} {via}: {ev['decoded']}"))
            elif kind == "received":
                frm = f"   ({ev['source']})" if ev.get("source") else ""
                self.log.append((t, "locked" if ev["locked"] else "recv", ev["text"] + frm))
            elif kind in ("log", "error"):
                self.log.append((t, ev.get("level", "error") if kind == "log" else "error", ev["text"]))

    # -- drawing -----------------------------------------------------------
    def draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 10 or w < 40:
            scr.addstr(0, 0, "window too small"[:w - 1])
            return
        s = self.tr.s
        C = curses.color_pair

        def put(y, x, text, attr=0):
            if 0 <= y < h and 0 <= x < w - 1:
                try:
                    scr.addnstr(y, x, text, w - 1 - x, attr)
                except curses.error:  # e.g. a wide character at the edge: skip, don't crash
                    pass

        put(0, 0, " ⚙ BINHARIC ".ljust(w - 1), C(1) | curses.A_BOLD)
        on = s.enabled
        put(0, 13, " ON " if on else " OFF ", (C(2) if on else C(3)) | curses.A_BOLD | curses.A_REVERSE)
        status = ("loading model…" if not self.tr.ready else self.tr.engine_name)[: max(0, w - 22)]
        put(0, max(19, w - len(status) - 2), status, C(1))

        send = "AS I TALK" if s.send_mode == LIVE else "AFTER I FINISH"
        x = 1
        for label, val, hi in [("send", send, True), ("mode", s.mode, False), ("speed", f"{s.speed:.1f}x", False),
                               ("privacy", {"plain": "UNENCRYPTED", "encrypted": "encrypted",
                                            "decoys": f"encrypted+{s.decoys} decoys"}[s.privacy], s.privacy != "plain"),
                               ("out", s.sink or self.tr.speakers_label, False),
                               ("passthrough", "on" if s.passthrough else "off", False)]:
            put(2, x, f"{label}: ", curses.A_DIM)
            x += len(label) + 2
            put(2, x, val, (C(4) | curses.A_BOLD) if hi else curses.A_BOLD)
            x += len(val) + 3

        # mic meter
        self.peak = max(self.level, self.peak * 0.92)
        width = max(10, w - 34)
        lvl = min(1.0, (self.level * 12) ** 0.6)
        filled = int(lvl * width)
        put(4, 1, "mic ", curses.A_DIM)
        put(4, 5, "█" * filled, C(2) if self.tr.speaking else C(5))
        put(4, 5 + filled, "░" * (width - filled), curses.A_DIM)
        if s.test and not self.tr.screeching.is_set():
            state, attr = "TEST MODE · say something", C(4) | curses.A_BOLD
        elif not on:
            state, attr = ("voice passing through" if s.passthrough else "idle"), curses.A_DIM
        elif self.tr.screeching.is_set():
            state, attr = "▲ SCREECHING", C(1) | curses.A_BOLD
        elif self.tr.speaking:
            state, attr = "● voice muted · listening", C(4) | curses.A_BOLD
        else:
            state, attr = "voice muted", curses.A_DIM
        put(4, 7 + width, state, attr)

        put(6, 1, "hearing: ", curses.A_DIM)
        italic = getattr(curses, "A_ITALIC", curses.A_NORMAL)
        put(6, 10, (self.partial[-max(1, w - 12):] if self.partial else "…"), italic if self.partial else curses.A_DIM)

        put(8, 0, "─" * (w - 1), curses.A_DIM)
        rows = h - 11
        styles = {"sent": ("▲", C(1)), "recv": ("▼", C(2) | curses.A_BOLD), "locked": ("▼", C(4)),
                  "warn": ("!", C(4)), "error": ("✖", C(3) | curses.A_BOLD), "info": ("·", curses.A_DIM)}
        for i, (t, kind, text) in enumerate(list(self.log)[-rows:]):
            mark, attr = styles.get(kind, ("·", 0))
            put(9 + i, 1, t, curses.A_DIM)
            put(9 + i, 10, f"{mark} {text}", attr)

        x = 0
        for k, label in KEYS:
            put(h - 1, x, f" {k} ", C(6) | curses.A_BOLD)
            x += len(k) + 2
            put(h - 1, x, f" {label}  ", curses.A_DIM)
            x += len(label) + 3

    def prompt(self, scr, label: str, secret: bool = False) -> str | None:
        h, w = scr.getmaxyx()
        curses.echo(not secret)
        curses.curs_set(1)
        scr.nodelay(False)
        scr.move(h - 1, 0)
        scr.clrtoeol()
        scr.addnstr(h - 1, 0, label, w - 1, curses.A_BOLD)
        try:
            raw = scr.getstr(h - 1, len(label), 400)
            return raw.decode("utf-8", "replace")
        except (KeyboardInterrupt, curses.error):
            return None
        finally:
            curses.noecho()
            curses.curs_set(0)
            scr.nodelay(True)

    def handle_key(self, scr, ch) -> bool:
        tr = self.tr
        if ch in (ord("q"), ord("Q")):  # not Esc: arrow keys start with Esc in many terminals
            return False
        if ch == ord(" "):
            tr.toggle()
        elif ch == ord("l"):
            tr.set_send_mode("toggle")
        elif ch == ord("m"):
            names = list(modem.MODES)
            tr.set_mode(names[(names.index(tr.s.mode) + 1) % len(names)])
        elif ch == ord("x"):
            tr.set_test(not tr.s.test)
        elif ch == ord("p"):
            tr.set_passthrough(not tr.s.passthrough)
        elif ch in (ord("+"), ord("=")):
            tr.set_speed(tr.s.speed + 0.25)
        elif ch == ord("-"):
            tr.set_speed(tr.s.speed - 0.25)
        elif ch == ord("c"):  # unencrypted -> encrypted -> encrypted + gibberish -> ...
            order = ["plain", "encrypted", "decoys"]
            tr.set_privacy(order[(order.index(tr.s.privacy) + 1) % 3])
        elif ch == ord("k"):
            key = self.prompt(scr, "key (empty clears): ", secret=True)
            if key is not None:
                tr.set_key(key)
                if key and tr.s.privacy == "plain":
                    tr.set_privacy("encrypted")
        elif ch == ord("t"):
            text = self.prompt(scr, "screech: ")
            if text:
                tr.say(text)
        return True

    def loop(self, scr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        scr.keypad(True)  # arrow/function keys arrive as single codes, not Esc sequences
        background = curses.COLOR_BLACK
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:  # terminals without default-colour support
                pass
            for i, fg in enumerate([curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_RED,
                                    curses.COLOR_YELLOW, curses.COLOR_CYAN, curses.COLOR_BLACK], start=1):
                curses.init_pair(i, fg, curses.COLOR_RED if i == 6 else background)
        scr.nodelay(True)
        while not self.tr.stopped:
            self.pump()
            self.draw(scr)
            scr.refresh()
            ch = scr.getch()
            if ch != -1 and not self.handle_key(scr, ch):
                break
            time.sleep(0.04)


def run(tr: Translator) -> None:
    locale.setlocale(locale.LC_ALL, "")
    tr.s.notify = False
    dash = Dashboard(tr)
    tr.start()
    try:
        curses.wrapper(dash.loop)
    except KeyboardInterrupt:
        pass
    finally:
        tr.stop()
