"""Desktop window (``binharic gui``), Tkinter (``sudo pacman -S tk``)."""

from __future__ import annotations

import queue
import time

from .core import Translator
from .stt import LIVE, PHRASE

BG, PANEL, FG, DIM = "#121012", "#1d191c", "#e8dcd0", "#8a7f78"
RED, RED_HI, GREEN, AMBER = "#b3121b", "#e0252f", "#3fbf6f", "#e0a526"
FONT = ("Inter", 10)
MONO = ("JetBrains Mono", 10)
LOG_LINES = 2000


def run(tr: Translator) -> None:
    tr.s.notify = False  # the window already shows every change
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.withdraw()  # stay hidden until we know we can start (else only an error dialog shows)
    root.title("Binharic")
    root.configure(bg=BG)
    root.minsize(560, 560)
    root.geometry("680x760")
    families = set(tkfont.families())
    ui = FONT if "Inter" in families else ("DejaVu Sans", 10)
    mono = MONO if "JetBrains Mono" in families else ("DejaVu Sans Mono", 10)

    events: queue.Queue = queue.Queue()
    tr.subscribe(lambda k, e: events.put((k, e)))
    state = {"level": 0.0}

    def label(parent, text, **kw):
        opts = dict(bg=parent["bg"], fg=DIM, font=ui)
        opts.update(kw)
        return tk.Label(parent, text=text, **opts)

    def segmented(parent, var, options, command):
        frame = tk.Frame(parent, bg=PANEL)
        for value, text in options:
            tk.Radiobutton(frame, text=text, value=value, variable=var, command=command, indicatoron=False,
                           bg=PANEL, fg=FG, selectcolor=RED, activebackground=RED_HI, activeforeground="white",
                           font=ui, bd=0, padx=12, pady=5, highlightthickness=0,
                           cursor="hand2").pack(side="left", padx=(0, 2))
        return frame

    # -- header -------------------------------------------------------------
    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=16, pady=(14, 6))
    tk.Label(head, text="⚙ BINHARIC", bg=BG, fg=RED_HI, font=(ui[0], 18, "bold")).pack(side="left")
    engine_lbl = label(head, "loading speech model…")
    engine_lbl.pack(side="right")

    power = tk.Button(root, text="", font=(ui[0], 20, "bold"), bd=0, relief="flat", cursor="hand2",
                      highlightthickness=0, command=lambda: tr.toggle(), pady=14)
    power.pack(fill="x", padx=16, pady=(4, 10))

    # -- controls -----------------------------------------------------------
    panel = tk.Frame(root, bg=PANEL, padx=14, pady=12)
    panel.pack(fill="x", padx=16)
    panel.columnconfigure(1, weight=1)

    send_var = tk.StringVar(value=tr.s.send_mode)
    label(panel, "Send", bg=PANEL).grid(row=0, column=0, sticky="w", pady=4)
    segmented(panel, send_var, [(PHRASE, "After I finish"), (LIVE, "As I talk")],
              lambda: tr.set_send_mode(send_var.get())).grid(row=0, column=1, sticky="w")

    mode_var = tk.StringVar(value=tr.s.mode)
    label(panel, "Sound", bg=PANEL).grid(row=1, column=0, sticky="w", pady=4)
    segmented(panel, mode_var, [("binharic", "Binharic screech"), ("hex", "Hex tones"), ("binary", "Binary 0/1")],
              lambda: tr.set_mode(mode_var.get())).grid(row=1, column=1, sticky="w")

    label(panel, "Key", bg=PANEL).grid(row=2, column=0, sticky="w", pady=4)
    keyrow = tk.Frame(panel, bg=PANEL)
    keyrow.grid(row=2, column=1, sticky="we")
    key_var = tk.StringVar(value=tr.s.key or "")
    key_entry = tk.Entry(keyrow, textvariable=key_var, show="•", bg=BG, fg=FG, insertbackground=FG,
                         relief="flat", font=mono, width=24, highlightthickness=1,
                         highlightbackground=PANEL, highlightcolor=RED)
    key_entry.pack(side="left", ipady=3)
    key_entry.bind("<Return>", lambda e: (tr.set_key(key_var.get()), root.focus()))
    tk.Button(keyrow, text="Set", command=lambda: tr.set_key(key_var.get()), bg=RED, fg="white", bd=0,
              activebackground=RED_HI, font=ui, padx=10, cursor="hand2").pack(side="left", padx=6)
    label(keyrow, "only key holders can read you", bg=PANEL).pack(side="left")
    decoy_var = tk.BooleanVar(value=tr.s.decoys > 0)
    tk.Checkbutton(panel, text="Hide it among decoy chirps (only key holders can tell which is real)",
                   variable=decoy_var, command=lambda: tr.set_decoys(3 if decoy_var.get() else 0), bg=PANEL,
                   fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG, font=ui,
                   highlightthickness=0).grid(row=3, column=1, sticky="w")

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("B.Horizontal.TScale", background=RED, troughcolor=BG, bordercolor=PANEL,
                    lightcolor=RED, darkcolor=RED, sliderthickness=14)
    style.map("B.Horizontal.TScale", background=[("active", RED_HI)])

    def slider(row, text, frm, to, step, value, fmt, command):
        label(panel, text, bg=PANEL).grid(row=row, column=0, sticky="w", pady=4)
        holder = tk.Frame(panel, bg=PANEL)
        holder.grid(row=row, column=1, sticky="w")
        shown = tk.Label(holder, bg=PANEL, fg=FG, font=ui, width=8, anchor="w")

        building = True  # ttk.Scale calls its command on set(): don't touch the settings while building

        def moved(v):
            v = round(float(v) / step) * step
            shown.configure(text=fmt(v))
            if not building:
                command(v)

        sc = ttk.Scale(holder, from_=frm, to=to, orient="horizontal", length=260,
                       style="B.Horizontal.TScale", command=moved)
        sc.set(value)
        sc.pack(side="left")
        shown.pack(side="left", padx=10)
        shown.configure(text=fmt(value))  # show the real value, unrounded
        building = False

    slider(4, "Speed", 0.5, 3.0, 0.25, tr.s.speed, lambda v: f"{v:.2f}×", tr.set_speed)
    slider(5, "Pause to send", 300, 2000, 50, tr.s.end_ms, lambda v: f"{int(v)} ms",
           lambda v: tr.set_end_ms(int(v)))

    pass_var = tk.BooleanVar(value=tr.s.passthrough)
    tk.Checkbutton(panel, text="Pass my normal voice through while OFF", variable=pass_var,
                   command=lambda: tr.set_passthrough(pass_var.get()), bg=PANEL, fg=FG, selectcolor=BG,
                   activebackground=PANEL, activeforeground=FG, font=ui, highlightthickness=0
                   ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

    test_var = tk.BooleanVar(value=tr.s.test)
    tk.Checkbutton(panel, text="Test mode: play the screech on my speakers and decode it back (timed)",
                   variable=test_var, command=lambda: tr.set_test(test_var.get()), bg=PANEL, fg=AMBER,
                   selectcolor=BG, activebackground=PANEL, activeforeground=AMBER, font=ui,
                   highlightthickness=0).grid(row=7, column=0, columnspan=2, sticky="w")

    # -- meter + live text --------------------------------------------------
    live = tk.Frame(root, bg=BG)
    live.pack(fill="x", padx=16, pady=(12, 4))
    meter = tk.Canvas(live, height=14, bg=PANEL, highlightthickness=0)
    meter.pack(fill="x")
    status_lbl = tk.Label(live, text="", bg=BG, fg=DIM, font=(ui[0], 10, "bold"), anchor="w")
    status_lbl.pack(fill="x", pady=(4, 0))
    partial_lbl = tk.Label(live, text="", bg=BG, fg=FG, font=(ui[0], 11, "italic"), anchor="w",
                           justify="left", wraplength=620)
    partial_lbl.pack(fill="x")

    # -- history ------------------------------------------------------------
    logf = tk.Frame(root, bg=PANEL)
    logf.pack(fill="both", expand=True, padx=16, pady=(6, 6))
    log = tk.Text(logf, bg=PANEL, fg=FG, font=mono, relief="flat", wrap="word", height=10,
                  padx=10, pady=8, highlightthickness=0, state="disabled", cursor="arrow")
    sb = tk.Scrollbar(logf, command=log.yview, bg=PANEL, troughcolor=PANEL, bd=0, highlightthickness=0)
    log.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    log.pack(side="left", fill="both", expand=True)
    for tag, color in [("time", DIM), ("sent", RED_HI), ("hex", DIM), ("recv", GREEN), ("locked", AMBER),
                       ("info", DIM), ("warn", AMBER), ("error", RED_HI)]:
        log.tag_configure(tag, foreground=color)

    def add_log(kind, text, extra=None):
        log.configure(state="normal")
        log.insert("end", time.strftime("%H:%M:%S  "), "time")
        mark = {"sent": "▲ ", "recv": "▼ ", "locked": "▼ "}.get(kind, "· ")
        log.insert("end", mark + text + "\n", kind)
        if extra:
            log.insert("end", "          " + extra + "\n", "hex")
        lines = int(log.index("end-1c").split(".")[0])
        if lines > LOG_LINES:  # a long session must not grow the window's memory forever
            log.delete("1.0", f"{lines - LOG_LINES}.0")
        log.configure(state="disabled")
        log.see("end")

    say = tk.Frame(root, bg=BG)
    say.pack(fill="x", padx=16, pady=(0, 14))
    say_var = tk.StringVar()
    say_entry = tk.Entry(say, textvariable=say_var, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                         font=ui, highlightthickness=1, highlightbackground=PANEL, highlightcolor=RED)
    say_entry.pack(side="left", fill="x", expand=True, ipady=6)

    def do_say(*_):
        if say_var.get().strip():
            tr.say(say_var.get().strip())
            say_var.set("")

    say_entry.bind("<Return>", do_say)
    tk.Button(say, text="Screech it", command=do_say, bg=RED, fg="white", bd=0, activebackground=RED_HI,
              font=(ui[0], 10, "bold"), padx=14, cursor="hand2").pack(side="left", padx=(8, 0), ipady=4)

    hint = label(root, "Space: on/off  ·  Ctrl+L: finish ⇄ live  ·  Ctrl+T: test  ·  "
                       "hotkeys: binharic toggle / binharic send toggle", bg=BG)
    hint.pack(pady=(0, 10))

    # -- keyboard -----------------------------------------------------------
    def typing():
        return root.focus_get() in (key_entry, say_entry)

    def click(event):
        # clicking anywhere but a text box leaves it, so Space toggles again
        if event.widget not in (key_entry, say_entry):
            root.focus_set()

    root.bind_all("<Button-1>", click, add="+")
    root.bind("<space>", lambda e: None if typing() else tr.toggle())
    root.bind("<Control-l>", lambda e: tr.set_send_mode("toggle"))
    root.bind("<Control-t>", lambda e: tr.set_test(not tr.s.test))
    root.bind("<Escape>", lambda e: root.focus())

    # -- refresh ------------------------------------------------------------
    def refresh():
        s = tr.s
        if s.enabled:
            power.configure(text="● ON · speaking binharic", bg=RED, fg="white", activebackground=RED_HI)
        else:
            power.configure(text="○ OFF", bg=PANEL, fg=DIM, activebackground="#2a2428")
        send_var.set(s.send_mode)
        mode_var.set(s.mode)
        pass_var.set(s.passthrough)
        test_var.set(s.test)
        decoy_var.set(s.decoys > 0)
        engine_lbl.configure(text=tr.engine_name if tr.ready else "loading speech model…")

    def poll():
        changed = False
        while True:
            try:
                kind, ev = events.get_nowait()
            except queue.Empty:
                break
            if kind == "level":
                state["level"] = ev["level"]
            elif kind == "partial":
                partial_lbl.configure(text=ev["text"])
            elif kind == "sent":
                extra = ev["hex"]
                if ev.get("test") and "latency_ms" in ev:
                    extra = (f"⏱ {ev['latency_ms']:.0f} ms after you stopped · recognition "
                             f"{ev['stt_ms']:.0f} ms · synth {ev['synth_ms']:.0f} ms")
                add_log("sent", ev["text"], extra)
            elif kind == "verify":
                via = "over the air" if ev["via"] == "air" else "round trip"
                add_log("recv" if ev["ok"] else "error",
                        f"{'✔' if ev['ok'] else '✖'} {via} {'OK' if ev['ok'] else 'FAILED'}: {ev['decoded']}")
            elif kind == "received":
                add_log("locked" if ev["locked"] else "recv", ev["text"])
            elif kind == "log":
                add_log(ev.get("level", "info"), ev["text"])
            elif kind == "error":
                add_log("error", ev["text"])
            elif kind == "status":
                changed = True
        if changed:
            refresh()

        w = meter.winfo_width()
        lvl = min(1.0, (state["level"] * 12) ** 0.6)
        meter.delete("all")
        meter.create_rectangle(0, 0, int(w * lvl), 14, fill=GREEN if tr.speaking else "#4a6b78", width=0)
        if not tr.ready and not tr.stopped:
            status_lbl.configure(text="loading the speech model…", fg=DIM)
        elif tr.s.test and not tr.screeching.is_set():
            status_lbl.configure(text="TEST MODE · say something" + (" · listening…" if tr.speaking else ""),
                                 fg=AMBER)
        elif not tr.s.enabled:
            status_lbl.configure(text="voice passing through" if tr.s.passthrough else "off: nothing is sent",
                                 fg=DIM)
        elif tr.screeching.is_set():
            status_lbl.configure(text="▲ SCREECHING", fg=RED_HI)
        elif tr.speaking:
            status_lbl.configure(text="● voice muted · listening", fg=AMBER)
        else:
            status_lbl.configure(text="voice muted · waiting for you to speak", fg=DIM)
        if tr.stopped:
            if tr.fatal:  # don't just vanish: say what went wrong
                messagebox.showerror("Binharic stopped", tr.fatal, parent=root)
            root.destroy()
            return
        root.after(40, poll)

    def on_close():
        tr.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    try:
        tr.start()
    except Exception as e:  # e.g. already running, a missing device: show it, launched from a menu
        messagebox.showerror("Binharic can't start", str(e), parent=root)
        root.destroy()
        raise
    root.deiconify()
    root.after(40, poll)
    try:
        root.mainloop()
    finally:
        tr.stop()
