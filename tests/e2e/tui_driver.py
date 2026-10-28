"""TUI end-to-end test: `binharic tui` in a real terminal, every key pressed.

    python -m tests.e2e.tui_driver [layer options]

Linux: a pseudo-terminal. Windows: ConPTY (pywinpty). The screen is rebuilt
with pyte so the test reads what a person would see. Prints ALL OK and exits
0 on success.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time

import pyte

from .rig import make_rig, speech
from .run import BH, KEY, PHRASES, Failed, check, similar, texts

ROWS, COLS = 40, 140


class Terminal:
    def __init__(self, argv: list[str]):
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()
        env = dict(os.environ, TERM="xterm-256color", LANG=os.environ.get("LANG", "C.UTF-8"))
        if sys.platform == "win32":
            from winpty import PtyProcess
            self.p = PtyProcess.spawn(subprocess.list2cmdline(argv), dimensions=(ROWS, COLS), env=env)
            self._read = lambda: self.p.read(4096).encode("utf-8", "replace")
            self._write = self.p.write
        else:
            import fcntl
            import pty
            import struct
            import termios
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
            self.p = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
            os.close(slave)
            self.master = master
            self._read = lambda: os.read(master, 4096)
            self._write = lambda s: os.write(master, s.encode())
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                data = self._read()
            except (OSError, EOFError):
                return
            if not data:
                return
            with self.lock:
                self.stream.feed(data)

    def text(self) -> str:
        with self.lock:
            return "\n".join(self.screen.display)

    def line(self, n: int) -> str:
        with self.lock:
            return self.screen.display[n]

    def key(self, s: str):
        self._write(s)
        time.sleep(0.4)

    def wait(self, what: str, timeout: float = 20, row: int | None = None, absent: bool = False):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            shown = self.line(row) if row is not None else self.text()
            if (what in shown) != absent:
                return
            if not self.alive():
                break
            time.sleep(0.1)
        raise Failed(f"{'still' if absent else 'never'} showed {what!r}" + (f" on row {row}" if row is not None else "")
                     + f":\n{self.text()}")

    def alive(self) -> bool:
        return self.p.isalive() if sys.platform == "win32" else self.p.poll() is None

    def exit_code(self, timeout: float = 15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.alive():
                return self.p.exitstatus if sys.platform == "win32" else self.p.returncode
            time.sleep(0.1)
        return None

    def kill(self):
        if self.alive():
            self.p.terminate() if sys.platform == "win32" else self.p.kill()


def dump(term: Terminal, name: str):
    out = os.environ.get("BINHARIC_E2E_ARTIFACTS")
    if out:
        with open(os.path.join(out, f"{name}.txt"), "w", encoding="utf-8") as f:
            f.write(term.text())


def record_while(rig, fn, term: Terminal, settle: float = 6.0):
    rec = rig.record("vmic")
    fn()
    time.sleep(settle)
    term.wait("SCREECHING", 60, absent=True)
    time.sleep(1.0)
    return rec.stop()


def main():
    rig = make_rig()
    rig.attach()
    argv = BH + ["tui", *sys.argv[1:]]
    print("running", " ".join(shlex.quote(a) for a in argv), flush=True)
    term = Terminal(argv)
    try:
        term.wait("BINHARIC", 30, row=0)
        term.wait("loading model", 300, row=0, absent=True)
        term.wait("whisper", 5, row=0)
        dump(term, "tui-ready")
        check("UNENCRYPTED" in term.line(2), f"header: {term.line(2)}")

        term.key(" ")
        term.wait(" OFF ", 5, row=0)
        term.key(" ")
        term.wait(" ON ", 5, row=0)
        term.key("l")
        term.wait("AS I TALK", 5, row=2)
        term.key("l")
        term.wait("AFTER I FINISH", 5, row=2)
        seen = []
        for _ in range(3):
            term.key("m")
            seen.append(term.line(2).split("mode: ")[1].split()[0])
        check(sorted(seen) == ["binary", "binharic", "hex"] and seen[-1] == "binharic", f"m cycles modes: {seen}")

        term.key("c")
        term.wait("set a key first", 5)
        check("UNENCRYPTED" in term.line(2), "no encryption without a key")
        term.key("k")
        term.wait("key (empty clears):", 5)
        term.key(KEY + "\r")
        term.wait("privacy: encrypted", 5, row=2)
        check(KEY not in term.text(), "the key isn't echoed on screen")
        term.key("c")
        term.wait("encrypted+3 decoys", 5, row=2)
        term.key("c")
        term.wait("UNENCRYPTED", 5, row=2)
        term.key("c")
        term.wait("privacy: encrypted", 5, row=2)

        term.key("+")
        term.wait("speed: 1.2x", 5, row=2)
        term.key("-")
        term.wait("speed: 1.0x", 5, row=2)
        term.key("p")
        term.wait("passthrough: on", 5, row=2)
        term.key("p")
        term.wait("passthrough: off", 5, row=2)
        term.key("x")
        term.wait("TEST MODE", 5, row=4)
        term.key("x")
        term.wait("TEST MODE", 5, row=4, absent=True)

        def type_it():
            term.key("t")
            term.wait("screech:", 5)
            term.key("typed in the terminal\r")
        audio = record_while(rig, type_it, term)
        check(texts(audio, KEY) == ["typed in the terminal"] and texts(audio) == [], "typed + encrypted")
        term.wait("▲ typed in the terminal", 5)

        term.key("c")
        term.key("c")
        term.wait("UNENCRYPTED", 5, row=2)
        audio = record_while(rig, lambda: (time.sleep(0.3), rig.play(speech("hello"))), term)
        got = " ".join(texts(audio))
        check(similar(got, PHRASES["hello"]), f"speech through the TUI: {got!r}\n{term.text()}")
        dump(term, "tui-sent")

        term.key("q")
        code = term.exit_code()
        check(code == 0, f"q quits cleanly (exit code {code})")
    except BaseException:
        dump(term, "tui-failure")
        print(term.text())
        term.kill()
        raise
    print("ALL OK", flush=True)


if __name__ == "__main__":
    main()
