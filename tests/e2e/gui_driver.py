"""GUI end-to-end test: the real window on real audio, every control used.

    python -m tests.e2e.gui_driver [layer options]

Runs `binharic gui` in this process (so it can reach the widgets), clicks
through everything from a script thread, talks into the fake mic and checks
what comes out of the virtual mic. Prints ALL OK and exits 0 on success.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk

from binharic import cli, core, ui_gui

from .rig import make_rig, speech
from .run import KEY, PHRASES, Failed, check, decode, similar, texts

calls: queue.Queue = queue.Queue()


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


class Window:
    """Runs things on the Tk thread (Tk isn't thread-safe) and finds widgets."""

    def __init__(self, root: tk.Tk):
        self.root = root

    def ui(self, fn, timeout: float = 15):
        done, box = threading.Event(), {}

        def call():
            try:
                box["v"] = fn()
            except BaseException as e:  # noqa: BLE001 - handed back to the script thread
                box["e"] = e
            done.set()
        calls.put(call)
        if not done.wait(timeout):
            raise Failed("the window stopped responding")
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def widget(self, cls, text: str | None = None, pred=None):
        def find():
            for w in walk(self.root):
                if isinstance(w, cls) and (text is None or w.cget("text") == text) and (pred is None or pred(w)):
                    return w
            raise Failed(f"no {cls.__name__} {text!r} in the window")
        return self.ui(find)

    def click(self, text: str, cls=(tk.Button, tk.Radiobutton, tk.Checkbutton)):
        w = self.widget(cls, text)
        self.ui(w.invoke)

    def log_text(self) -> str:
        t = self.widget(tk.Text)
        return self.ui(lambda: t.get("1.0", "end"))


def wait(cond, timeout: float, what: str):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.1)
    raise Failed(f"timed out waiting for {what}")


def idle(tr, settle: float = 1.0):
    quiet = None
    end = time.monotonic() + 90
    while time.monotonic() < end:
        busy = tr.screeching.is_set() or tr._pending or tr.speaking
        if busy:
            quiet = None
        elif quiet is None:
            quiet = time.monotonic()
        elif time.monotonic() - quiet > settle:
            return
        time.sleep(0.1)
    raise Failed("never went idle")


def screenshot(name: str):
    out = os.environ.get("BINHARIC_E2E_ARTIFACTS")
    if not out:
        return
    path = os.path.join(out, f"{name}.png")
    try:
        if sys.platform == "win32":
            from PIL import ImageGrab
            ImageGrab.grab().save(path)
        elif shutil.which("import"):
            subprocess.run(["import", "-window", "root", path], timeout=20)
    except Exception as e:  # noqa: BLE001 - a missing screenshot isn't a test failure
        print(f"(no screenshot: {e})")


def script(win: Window, tr: core.Translator, rig):
    s = tr.s
    wait(lambda: tr.ready, 300, "the speech model")
    wait(lambda: win.ui(lambda: any(isinstance(w, tk.Label) and w.cget("text") == tr.engine_name
                                    for w in walk(win.root))), 5, "the engine name in the header")
    screenshot("gui-ready")

    # power button, and the Space key
    power = win.widget(tk.Button, "● ON · speaking binharic")
    win.ui(power.invoke)
    check(not s.enabled, "power off")
    wait(lambda: win.ui(lambda: power.cget("text")) == "○ OFF", 3, "the power button to say OFF")
    win.ui(power.invoke)
    check(s.enabled, "power on")
    win.ui(lambda: (win.root.focus_force(), win.root.event_generate("<space>")))
    wait(lambda: not s.enabled, 3, "Space toggles")
    win.ui(lambda: win.root.event_generate("<space>"))
    wait(lambda: s.enabled, 3, "Space toggles back")

    # send mode, sound mode
    win.click("As I talk")
    check(s.send_mode == "live", "As I talk")
    win.click("After I finish")
    check(s.send_mode == "phrase", "After I finish")
    win.ui(lambda: win.root.event_generate("<Control-l>"))
    wait(lambda: s.send_mode == "live", 3, "Ctrl+L")
    win.ui(lambda: win.root.event_generate("<Control-l>"))
    wait(lambda: s.send_mode == "phrase", 3, "Ctrl+L back")
    for text, mode in (("Hex tones", "hex"), ("Binary 0/1", "binary"), ("Binharic screech", "binharic")):
        win.click(text)
        check(s.mode == mode, text)

    # privacy: refused without a key, then all three
    win.click("Encrypted")
    check(s.privacy == "plain", "encryption needs a key")
    key_entry = win.widget(tk.Entry, pred=lambda w: w.cget("show") == "•")
    win.ui(lambda: key_entry.insert(0, KEY))
    win.click("Set")
    check(s.key == KEY and s.privacy == "encrypted", "setting a key switches to encrypted")
    win.click("Encrypted + gibberish")
    check(s.privacy == "decoys", "gibberish")
    win.click("Unencrypted")
    check(s.privacy == "plain" and s.key == KEY, "unencrypted keeps the key")
    win.click("Encrypted")
    check(s.privacy == "encrypted", "encrypted")

    # sliders, checkboxes
    scales = win.ui(lambda: [w for w in walk(win.root) if isinstance(w, ttk.Scale)])
    win.ui(lambda: scales[0].set(2.0))
    check(abs(s.speed - 2.0) < 1e-6, f"speed slider: {s.speed}")
    win.ui(lambda: scales[0].set(1.0))
    win.ui(lambda: scales[1].set(900))
    check(s.end_ms == 900, f"pause slider: {s.end_ms}")
    win.ui(lambda: scales[1].set(700))
    win.click("Pass my normal voice through while OFF")
    check(s.passthrough, "passthrough on")
    win.click("Pass my normal voice through while OFF")
    check(not s.passthrough, "passthrough off")
    test_box = "Test mode: play the screech on my speakers and decode it back (timed)"
    win.click(test_box)
    check(s.test, "test mode on")
    win.click(test_box)
    check(not s.test, "test mode off")
    win.ui(lambda: win.root.event_generate("<Control-t>"))
    wait(lambda: s.test, 3, "Ctrl+T")
    win.ui(lambda: win.root.event_generate("<Control-t>"))
    wait(lambda: not s.test, 3, "Ctrl+T back")
    screenshot("gui-controls")

    # type to screech (encrypted)
    say_entry = win.widget(tk.Entry, pred=lambda w: w.cget("show") != "•")
    rec = rig.record("vmic")
    win.ui(lambda: say_entry.insert(0, "typed in the window"))
    win.click("Screech it")
    time.sleep(1)
    idle(tr)
    audio = rec.stop()
    check(texts(audio, KEY) == ["typed in the window"] and texts(audio) == [],
          f"typed + encrypted: {decode(audio)}")
    check("typed in the window   [encrypted]" in win.log_text(), "the log shows it")

    # speech, unencrypted
    win.click("Unencrypted")
    rec = rig.record("vmic")
    time.sleep(0.3)
    rig.play(speech("hello"))
    time.sleep(1)
    idle(tr)
    got = " ".join(texts(rec.stop()))
    check(similar(got, PHRASES["hello"]), f"speech through the GUI: {got!r}")
    check("[unencrypted]" in win.log_text(), "logged as unencrypted")
    screenshot("gui-sent")

    # close the window like a user
    win.ui(lambda: win.root.tk.call(win.root.protocol("WM_DELETE_WINDOW")))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = cli.build_parser().parse_args(["gui", *sys.argv[1:]])
    tr = core.Translator(cli._settings(args))
    rig = make_rig()
    rig.attach()
    result = {"ok": False}
    original = tk.Tk.mainloop

    def mainloop(root, *a):
        win = Window(root)

        def pump():
            while True:
                try:
                    calls.get_nowait()()
                except queue.Empty:
                    break
            root.after(20, pump)
        root.after(20, pump)

        def run():
            try:
                script(win, tr, rig)
                result["ok"] = True
            except BaseException:  # noqa: BLE001
                traceback.print_exc()
                screenshot("gui-failure")
                try:
                    win.ui(lambda: root.destroy(), 5)
                except Failed:
                    os._exit(1)
        threading.Thread(target=run, daemon=True).start()
        original(root, *a)

    tk.Tk.mainloop = mainloop
    fatal = cli._run_ui(ui_gui.run, tr)
    if fatal:
        print("fatal:", fatal)
    print("ALL OK" if result["ok"] and not fatal else "FAILED", flush=True)
    os._exit(0 if result["ok"] and not fatal else 1)


if __name__ == "__main__":
    main()
