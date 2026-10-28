"""Command-line entry point: ``binharic <command>``.

    binharic                      # pick a front end (gui / tui / listen)
    binharic gui | tui | listen   # start the translator layer
    binharic toggle | on | off    # control a running layer (bind to hotkeys)
    binharic send toggle|live|phrase
    binharic mic up|down|status   # the "Binharic Mic" virtual device
    binharic say / decode / text  # one-off encoding and decoding
"""

from __future__ import annotations

import argparse
import json
import re
import os
import shutil
import signal
import sys

import numpy as np

from . import codec, modem
from .osinfo import ISH

SR = modem.DEFAULT_SR

try:
    from rich.console import Console
    from rich.markup import escape as _rich_escape
    console = Console(highlight=False)
except ImportError:
    console = None

    def _rich_escape(text: str) -> str:
        return text


def esc(text: object) -> str:
    """Make arbitrary text (decoded messages, device names, paths) safe inside rich markup."""
    return _rich_escape(str(text))


_STYLE_TAG = re.compile(r"\[/?(?:bold|dim|green|red|yellow|cyan)(?: (?:green|red|yellow|cyan))?\]|\[/\]")


def say_line(markup: str, plain: str | None = None) -> None:
    if console:
        console.print(markup)
    else:  # no rich (e.g. iSH): drop our style tags rather than print them
        print(plain if plain is not None else _STYLE_TAG.sub("", markup))


# argument checks: a bad value gets a one-line error, not a traceback
def _ranged(kind, lo: float, hi: float, what: str):
    def check(value: str):
        try:
            v = kind(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{value!r} isn't a number") from None
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"{what} must be between {lo:g} and {hi:g}")
        return v
    return check


SPEED = _ranged(float, 0.25, 4.0, "speed")
VOLUME = _ranged(float, 0.01, 1.0, "volume")
PAUSE = _ranged(int, 100, 5000, "pause (ms)")
THRESHOLD = _ranged(float, 0.05, 0.95, "threshold")


def _key(value: str | None) -> str | None:
    return value or os.environ.get("BINHARIC_KEY") or None


# --------------------------------------------------------------------------
# translator front ends

def _settings(args):
    from . import pulse
    from .core import Settings
    from .stt import LIVE, PHRASE, resolve_engine

    from .osinfo import load_config

    cfg = load_config()  # what `binharic fix` found to work
    sink = pulse.resolve_sink(args.sink)
    model, live_model, beam, pause, partials = args.model, args.live_model, 5, args.pause_ms, True
    fast = args.fast
    if not fast and not args.accurate and not args.model and resolve_engine(args.engine) == "whisper":
        # no usable GPU: the quick single-model setup is the default (`--accurate` for the careful one)
        from . import gpu
        fast = args.device == "cpu" or not (gpu.cuda_devices() > 0 and gpu.libs_available())
    if fast:
        # one quick model, greedy decoding, shorter pause, no interim transcripts
        beam, partials, pause = 1, False, min(pause, 450)
        if resolve_engine(args.engine) == "whisper":
            model, live_model = model or ("base.en" if not args.language else "base"), None
    return Settings(
        enabled=not args.start_off,
        send_mode=LIVE if args.live else PHRASE,
        mode=args.mode, key=_key(args.key), speed=args.speed, volume=args.volume,
        passthrough=args.passthrough, engine=args.engine, model=model, live_model=live_model,
        device=args.device, beam=beam, privacy=_privacy(args), decoys=args.decoys or 3, partials=partials, language=args.language, test=getattr(args, "test_mode", False), test_air=getattr(args, "air", False),
        sink=sink, source=pulse.resolve_source(args.source) or (cfg.get("source") if not args.input_device else None),
        output_device=args.output_device, input_device=args.input_device or cfg.get("input_device"),
        duplex=args.duplex, end_ms=pause,
        hear=pulse.resolve_source(args.hear) if pulse.available() else args.hear, hear_key=_key(args.hear_key) if args.hear_key else None,
    )


def _privacy(args) -> str:
    """--privacy, or what the other options imply: a key -> encrypted, --decoys -> decoys."""
    key = _key(args.key)
    privacy = args.privacy or ("decoys" if args.decoys else "encrypted" if key else "plain")
    if privacy != "plain" and not key:
        sys.exit(f"--privacy {privacy} needs a key: add -k \"your passphrase\" (or set BINHARIC_KEY)")
    return privacy


def _translator(args):
    from .core import Translator
    return Translator(_settings(args))


def _run_ui(run, tr, **kw):
    # `systemctl --user stop`, closing the terminal, `kill`: shut down cleanly
    # (close the audio, remove the control socket) instead of just dying
    # Windows: Ctrl+Break, or closing the console window, arrives as SIGBREAK
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: tr.request_stop())
    try:
        run(tr, **kw)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            tr.stop()
        except KeyboardInterrupt:
            pass
    return tr.fatal


def _no_live_audio_on_ish():
    if ISH:
        sys.exit("iSH can't use the iPhone's mic or speaker, so live modes don't work here.\n"
                 "Use `binharic ish` (dictate or type -> sound files), `binharic say`, `binharic decode --wav`.")


def cmd_ish(args):
    from . import ish
    key = _key(getattr(args, "key", None))
    privacy = getattr(args, "privacy", None) or ("decoys" if getattr(args, "decoys", 0) else
                                                  "encrypted" if key else "plain")
    if privacy != "plain" and not key:
        sys.exit(f"--privacy {privacy} needs a key: add -k \"your passphrase\"")
    ish.run(mode=getattr(args, "mode", "binharic"), key=key if privacy != "plain" else None,
            speed=getattr(args, "speed", 1.0),
            decoys=(getattr(args, "decoys", 0) or 3) if privacy == "decoys" else 0)


def cmd_fix(args):
    from . import fix
    sys.exit(fix.run(yes=args.yes, gpu=not args.no_gpu))


def cmd_listen(args):
    _no_live_audio_on_ish()
    from . import ui_cli
    return _run_ui(ui_cli.run, _translator(args), show=getattr(args, "show", "text"))


def cmd_test(args):
    _no_live_audio_on_ish()
    from . import ui_cli
    from .core import send_command
    try:
        reply = json.loads(send_command("test"))
    except RuntimeError:
        pass  # nothing running: start our own test session below
    else:
        if reply["test"]:
            say_line("[yellow]⚙ binharic is already running, so test mode is now ON in that one:[/] its screech "
                     "plays on your speakers instead of the Binharic Mic. Results show in its window "
                     "(or: journalctl --user -u binharic -f).\n  [bold]Run `binharic test` again to switch it "
                     "back[/] to the Binharic Mic.")
        else:
            say_line("[green]⚙ test mode OFF[/] in the running binharic: the screech goes to the Binharic Mic again.")
        return
    args.test_mode = True
    return _run_ui(ui_cli.run, _translator(args), show="text", test=True)


def cmd_tui(args):
    _no_live_audio_on_ish()
    from . import ui_tui
    return _run_ui(ui_tui.run, _translator(args))


def cmd_gui(args):
    _no_live_audio_on_ish()
    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.exit("Tk is missing: sudo pacman -S tk   (or use `binharic tui`)")
    from . import ui_gui
    return _run_ui(ui_gui.run, _translator(args))


def cmd_pick(args):
    """No subcommand: start the GUI on a desktop, the TUI in a terminal."""
    if ISH:
        return cmd_ish(args)
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        try:
            import tkinter  # noqa: F401
        except ImportError:
            pass
        else:
            return cmd_gui(args)
    if sys.stdout.isatty():
        return cmd_tui(args)
    return cmd_listen(args)


# --------------------------------------------------------------------------
# control of a running layer

def _control(cmd: str):
    from .core import send_command
    try:
        reply = send_command(cmd)
    except RuntimeError as e:
        sys.exit(str(e))
    if reply.startswith("error"):
        sys.exit(reply)
    st = json.loads(reply)
    on = "[bold green]ON[/]" if st["enabled"] else "[bold red]OFF[/]"
    send = "as you talk" if st["send_mode"] == "live" else "after you finish"
    priv = {"plain": "[bold red]UNENCRYPTED[/]", "encrypted": "[green]encrypted[/]",
            "decoys": "[green]encrypted + gibberish[/]"}.get(st.get("privacy"), "?")
    say_line(f"{on} · send {send} · mode {st['mode']} · {priv}",
             f"{'ON' if st['enabled'] else 'OFF'} · send {send} · mode {st['mode']}")


def cmd_ctl(args):
    _control(args.command)


# --------------------------------------------------------------------------
# one-offs

def cmd_say(args):
    from . import pulse
    text = " ".join(args.text) if args.text else ("" if sys.stdin.isatty() else sys.stdin.read())
    text = " ".join(text.split())
    if not text:
        sys.exit("nothing to say: binharic say \"your message\"  (or pipe text in)")
    if ISH and not args.wav:
        from . import ish
        privacy = _privacy(args)
        path = ish.to_wav(text, args.mode, _key(args.key) if privacy != "plain" else None, args.speed,
                          decoys=(args.decoys or 3) if privacy == "decoys" else 0)
        say_line(f"[green]wrote[/] {esc(path)} [dim](play it from the Files app: iSH -> root -> binharic)[/]")
        return
    if not args.wav and not args.direct:
        # a running layer owns the Binharic Mic: queue it there rather than
        # playing a second stream over (and garbling) whatever it's sending
        from .core import send_command
        try:
            send_command("say " + text)
        except RuntimeError:
            pass
        else:
            say_line("[green]⚙ queued in the running binharic[/] [dim](its mode, key and speed apply; "
                     "--direct plays it yourself)[/]")
            return
    privacy = _privacy(args)
    key = _key(args.key) if privacy != "plain" else None
    decoys = (args.decoys or 3) if privacy == "decoys" else 0
    audio = modem.encode_text(text, args.mode, key=key, sr=SR, speed=args.speed, volume=args.volume,
                              decoys=decoys)
    if args.wav:
        from .audio import write_wav
        write_wav(args.wav, audio, SR)
        say_line(f"[green]wrote[/] {esc(args.wav)} [dim]({len(audio) / SR:.1f}s)[/]")
        return
    from .audio import Output
    sink = pulse.resolve_sink(args.sink)
    device = args.output_device
    if device and not sink:
        from . import pa
        device = pa.resolve(device, "output")  # a name: Windows lists each device several times
    if sink is None and not device and pulse.available():
        sink = pulse.real_sink()  # PipeWire directly: PortAudio over ALSA is unreliable there
    elif sink is None and not device and args.sink == "auto":
        from . import pa
        device = pa.find_cable() if pa.available() else None  # Windows: VB-Cable is the virtual mic
    out = Output(SR, sink=sink, device=device)
    try:
        out.write(audio)
        out.write(np.zeros(SR // 4, dtype=np.float32))
    finally:
        out.close()


def cmd_decode(args):
    from . import pulse
    key = _key(args.key)
    if args.wav:
        from .audio import read_wav
        from .ish import readable
        sig, sr = read_wav(readable(args.wav))
        msgs = modem.decode_audio(sig, sr, args.mode, key=key, speed=args.speed, threshold=args.threshold)
        if key:
            skipped = sum(m.locked for m in msgs)
            msgs = [m for m in msgs if not m.locked]
            if skipped:
                say_line(f"[dim]({skipped} frame(s) don't match your key: decoys, or a different key)[/]",
                         f"({skipped} frame(s) don't match your key: decoys, or a different key)")
        if not msgs:
            say_line("[yellow]no transmissions found[/]", "no transmissions found")
            sys.exit(1)
        for m in msgs:
            print(m.display())
        return

    from .audio import Input
    source = pulse.resolve_source(args.source)
    if source is None and not args.input_device and pulse.available():
        source = pulse.real_source()
    elif source:
        pulse.check_device(source, "sources")
    device = None
    if not source and args.input_device:
        from . import pa
        device = pa.resolve(args.input_device, "input")
    mic = Input(SR, source=source, device=device)
    dec = modem.StreamDecoder(SR, args.mode, key=key, speed=args.speed, threshold=args.threshold)
    where = pulse.describe(source, "sources") if source else args.input_device or "the default mic"
    say_line(f"[cyan]⚙ listening for binharic on {esc(where)}… (Ctrl+C stops)[/]")

    def show(m):
        if m.locked and key:
            return  # a decoy (or a different key)
        tag = modem.MODES_BY_ID[m.mode_id].name if m.mode_id in modem.MODES_BY_ID else "?"
        style = "yellow" if m.locked else "bold green"
        # decoded text is whatever someone sent: never let it be read as markup
        say_line(f"[{style}]▼ {esc('[' + tag + ']')} {esc(m.display())}[/]", f"<< [{tag}] {m.display()}")

    try:
        while True:
            for m in dec.feed(mic.read()):
                show(m)
    except (KeyboardInterrupt, EOFError):
        for m in dec.flush():
            show(m)
    finally:
        mic.close()


def cmd_text(args):
    text = " ".join(args.text) if args.text else ("" if sys.stdin.isatty() else sys.stdin.read().strip())
    if not text:
        sys.exit("nothing to convert: binharic text \"your message\"  (or -r \"01001000 ...\")")
    if args.reverse:
        t = text.strip()
        try:
            print(codec.from_binary(t) if set(t) <= set("01 \n\t") else codec.from_hex(t))
        except ValueError as e:
            sys.exit(f"can't read that: {e}")
        return
    say_line(f"[dim]binary[/] {codec.to_binary(text)}", f"binary: {codec.to_binary(text)}")
    say_line(f"[dim]hex   [/] {codec.to_hex(text)}", f"hex:    {codec.to_hex(text)}")


def cmd_mic(args):
    from . import pulse
    from .osinfo import WINDOWS
    if WINDOWS or (ISH and not shutil.which("pactl")):
        return _mic_windows(args)
    try:
        if args.action == "up":
            pulse.mic_up()
            say_line("[green]✔[/] [bold]Binharic Mic[/] is ready. Pick it as the input in Discord/OBS/etc.\n"
                     "  `binharic` now sends into it automatically.")
        elif args.action == "down":
            say_line(f"removed {pulse.mic_down()} module(s)")
        else:
            ok = pulse.virtual_mic_exists()
            say_line("[green]✔ Binharic Mic present[/]" if ok else "[yellow]✖ not set up: binharic mic up[/]")
    except Exception as e:
        sys.exit(f"pactl failed: {e}")


def _mic_windows(args):
    """Windows has no PipeWire to make a virtual mic: VB-Audio's free Virtual
    Cable is one. binharic plays into "CABLE Input"; Discord records "CABLE Output"."""
    from . import pa
    if ISH:
        sys.exit("iSH has no virtual microphone: binharic writes sound files there (see `binharic ish`)")
    cable = pa.find_cable() if pa.available() else None
    if cable is not None:
        say_line("[green]✔ VB-Audio Virtual Cable is installed[/]: binharic sends into 'CABLE Input'. "
                 "In Discord/OBS pick [bold]CABLE Output[/] as your microphone.")
        if args.action == "down":
            say_line("  (to remove it, uninstall 'VB-Audio Virtual Cable' in Settings -> Apps)")
        return
    say_line(f"[yellow]✖ VB-Audio Virtual Cable isn't installed.[/] On Windows it is the Binharic Mic: download "
             f"it from {pa.CABLE_URL}, run VBCABLE_Setup_x64.exe as administrator, reboot, then in Discord "
             "pick 'CABLE Output' as your mic.")
    if args.action == "up":
        sys.exit(1)


def cmd_devices(args):
    from . import pulse
    if pulse.available():
        mic, spk = pulse.real_source(), pulse.real_sink()
        for kind, title, mine in (("sinks", "Outputs (--sink)", spk),
                                  ("sources", "Inputs (--source, --hear; 'speakers' = what you hear)", mic)):
            say_line(f"[bold]{title}[/]")
            for d in pulse._info(kind):
                star = " [green]← binharic uses this[/]" if d["name"] == mine else ""
                say_line(f"  {esc(d['name'])}  [dim]{esc(d['description'])}[/]{star}",
                         f"  {d['name']}  ({d['description']})")
    try:
        import sounddevice as sd
        say_line("[bold]PortAudio devices[/] [dim](--input-device / --output-device)[/]")
        for i, d in enumerate(sd.query_devices()):
            say_line(f"  {i:>2} {d['name']}  [dim]in {d['max_input_channels']} / out {d['max_output_channels']}[/]")
    except Exception as e:
        if pulse.available():
            say_line("[dim]PortAudio isn't installed - not needed, binharic talks to PipeWire directly[/]")
        else:
            say_line(f"[yellow]PortAudio unavailable: {esc(e)}[/]")


def cmd_download(args):
    from . import stt
    engine = args.engine
    if engine == "auto":
        engine = "whisper" if stt.have_whisper() else "vosk"
    say_line(f"[cyan]⚙ downloading {engine} model {args.name or ''}…[/]")
    path = stt.download(engine, args.name)
    say_line(f"[green]✔[/] {path}")


def _gpu_checks():
    from . import gpu
    if not gpu.has_nvidia_driver():
        return []  # no NVIDIA card: CPU it is, nothing to report
    return [("NVIDIA GPU visible to Whisper", gpu.cuda_devices() > 0, "check the nvidia driver (nvidia-smi)"),
            ("CUDA 12 libraries (cuBLAS, cuDNN)", gpu.libs_available(),
             "rerun ./install.sh  (or run on the CPU: --device cpu)")]


def _audio_checks():
    from . import pulse
    if not pulse.available():
        return []
    src, sink = pulse.real_source(), pulse.real_sink()
    out = [(f"speakers: {pulse.describe(sink, 'sinks')}", bool(sink), "no output device found (pavucontrol)")]
    if not src:
        return out + [("microphone", False, "no input device found: plug one in / check pavucontrol")]
    say_line(f"  [dim]… listening to {pulse.describe(src, 'sources')} for a second[/]")
    peak = pulse.probe_source(src, 1.0)
    label = f"microphone: {pulse.describe(src, 'sources')}"
    if peak is None:
        return out + [(label, False, "couldn't record from it")]
    ok = peak >= 3e-4
    return out + [(label + f"  (level {peak:.3f})", ok,
                   "silent: unmute it / pick the right input in pavucontrol, or --source NAME")]


def cmd_doctor(args):
    from . import pulse, stt

    def mod(name):
        try:
            __import__(name)
            return True
        except Exception:
            return False

    have_pulse = pulse.available()
    checks = [
        ("numpy", mod("numpy"), "rerun ./install.sh"),
        # only a fallback when PipeWire/Pulse is running
        ("sounddevice / PortAudio" + (" (optional with PipeWire)" if have_pulse else ""),
         mod("sounddevice") or None if have_pulse else mod("sounddevice"), "sudo pacman -S portaudio"),
        ("pactl / pacat (PipeWire)", bool(shutil.which("pactl") and shutil.which("pacat")),
         "sudo pacman -S libpulse pipewire-pulse"),
        ("PipeWire/Pulse server", have_pulse, "systemctl --user start pipewire-pulse"),
        ("Binharic Mic", pulse.virtual_mic_exists(), "binharic mic up"),
        ("Whisper (faster-whisper)", stt.have_whisper(), "rerun ./install.sh"),
        ("Vosk", mod("vosk"), "rerun ./install.sh"),
        *_gpu_checks(),
        ("Tk (for the GUI)", mod("tkinter"), "sudo pacman -S tk"),
        ("rich (pretty CLI)", mod("rich"), "rerun ./install.sh"),
        ("notify-send", bool(shutil.which("notify-send")), "sudo pacman -S libnotify"),
    ]
    checks += _audio_checks()
    for name, ok, fix in checks:
        if ok is None:  # optional and missing: not a problem
            say_line(f"  [dim]–[/] {esc(name)}  [dim](not installed; fine)[/]", f"  --  {name} (not installed; fine)")
            continue
        say_line(f"  {'[green]✔[/]' if ok else '[red]✖[/]'} {esc(name)}" + ("" if ok else f"  [dim]→ {esc(fix)}[/]"),
                 f"  {'ok ' if ok else 'MISSING'} {name}" + ("" if ok else f"  -> {fix}"))
    if any(ok is False for _, ok, _ in checks):
        say_line("\n  [bold]Run `binharic fix` to repair these[/] (it tests your mic for real and fixes what it can).",
                 "\n  Run `binharic fix` to repair these.")


# --------------------------------------------------------------------------
# parser

def _run_options(p, inherit: bool = False):
    """Translator options. On subcommands (``inherit``) they default to
    whatever was given before the subcommand, so `binharic -k x gui` works."""
    if inherit:
        real = p.add_argument_group

        def group(title):
            g = real(title)
            add = g.add_argument

            def add_suppressed(*a, **kw):
                kw["default"] = argparse.SUPPRESS
                return add(*a, **kw)
            g.add_argument = add_suppressed
            return g
        p.add_argument_group = group
    g = p.add_argument_group("sound")
    g.add_argument("-m", "--mode", choices=list(modem.MODES), default="binharic",
                   help="binharic = Mechanicus screech, hex = 16 tones, binary = 0/1 beeps")
    g.add_argument("-k", "--key", help="shared cant-key; only holders can read you (or set BINHARIC_KEY)")
    g.add_argument("--privacy", choices=["plain", "encrypted", "decoys"],
                   help="plain = unencrypted, encrypted = only key holders can read it, decoys = encrypted and "
                        "hidden among gibberish chirps (default: encrypted if you give -k, else plain)")
    g.add_argument("--decoys", type=_ranged(int, 0, 8, "decoys"), default=0,
                   help="how many gibberish chirps per message in decoys privacy (1-8, default 3; implies "
                        "--privacy decoys)")
    g.add_argument("--speed", type=SPEED, default=1.0, help="tempo multiplier 0.25-4, listeners must match")
    g.add_argument("--volume", type=VOLUME, default=0.6, help="0.01-1")

    g = p.add_argument_group("sending")
    g.add_argument("--live", action="store_true", help="screech words as you say them (default: after you finish)")
    g.add_argument("--pause-ms", type=PAUSE, default=700, help="silence that counts as 'finished' (ms)")
    g.add_argument("--start-off", action="store_true", help="start in the OFF state")
    g.add_argument("--passthrough", action="store_true", help="while OFF, send your normal voice")

    g = p.add_argument_group("speech recognition")
    g.add_argument("--engine", choices=["auto", "whisper", "vosk"], default="auto",
                   help="whisper = accurate (default when installed), vosk = light")
    g.add_argument("--model", help="whisper size (tiny.en base.en small.en medium.en large-v3) or vosk model dir")
    g.add_argument("--live-model", default="base.en",
                   help="faster whisper model for live words / interim text (default base.en)")
    g.add_argument("--language", help="language for multilingual whisper models, e.g. de, fr, ja "
                                      "(default: detect; .en models are English)")
    g.add_argument("--device", default="auto", help="whisper compute device: auto, cpu, cuda")
    g.add_argument("--fast", action="store_true",
                   help="lowest latency: base.en only, greedy decoding, 450 ms pause, no live transcript "
                        "(the default without a working GPU)")
    g.add_argument("--accurate", action="store_true",
                   help="small.en for the final pass even on the CPU: better words, about 3x slower")

    g = p.add_argument_group("audio routing")
    g.add_argument("--sink", default="auto",
                   help="where the screech goes: auto (Binharic Mic if set up), default, or a sink name")
    g.add_argument("--source", help="mic to listen to (PipeWire source name; default system mic)")
    g.add_argument("--hear", help="also decode incoming binharic from this source ('speakers' = what you hear)")
    g.add_argument("--hear-key", help="key for incoming messages (defaults to --key)")
    g.add_argument("--duplex", choices=["auto", "full", "half"], default="auto",
                   help="half = ignore the mic while screeching (auto: half unless using the virtual mic)")
    g.add_argument("--output-device", help="PortAudio output device when not using a sink")
    g.add_argument("--input-device", help="PortAudio input device when not using --source")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="binharic", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Speak, and the machine screeches it: binary, hex or binharic, live.",
        epilog="run `binharic <command> -h` for details · `binharic doctor` checks your setup")
    _run_options(p)
    p.set_defaults(func=cmd_pick)
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    for name, func, text in [("gui", cmd_gui, "desktop window"),
                             ("tui", cmd_tui, "full-screen terminal dashboard"),
                             ("listen", cmd_listen, "plain terminal log")]:
        s = sub.add_parser(name, help=f"start the translator: {text}")
        _run_options(s, inherit=True)
        if name == "listen":
            s.add_argument("--show", choices=["text", "hex", "binary"], default="text",
                           help="what to print for each sent message")
        s.set_defaults(func=func)

    s = sub.add_parser("test", help="speak and hear the screech back on your speakers, decoded and timed")
    _run_options(s, inherit=True)
    s.add_argument("--air", action="store_true",
                   help="also decode what the mic hears from your speakers (needs speakers, not headphones)")
    s.set_defaults(func=cmd_test)

    for name, cmd, text in [("toggle", "toggle", "flip the running layer ON/OFF"),
                            ("on", "on", "turn it ON"), ("off", "off", "turn it OFF"),
                            ("status", "status", "show the running layer's state")]:
        sub.add_parser(name, help=text).set_defaults(func=cmd_ctl, command=cmd)
    s = sub.add_parser("privacy", help="running layer: plain (unencrypted), encrypted, or decoys "
                                       "(encrypted + gibberish)")
    s.add_argument("level", choices=["plain", "encrypted", "decoys"])
    s.set_defaults(func=lambda a: _control(f"privacy {a.level}"))
    s = sub.add_parser("send", help="choose when to screech: toggle, live (as I talk), phrase (after I finish)")
    s.add_argument("when", nargs="?", choices=["toggle", "live", "phrase"], default="toggle")
    s.set_defaults(func=lambda a: _control(f"send {a.when}"))

    s = sub.add_parser("say", help="screech typed text: through the running layer if there is one, "
                                   "else into the Binharic Mic (or your speakers), or to a WAV")
    s.add_argument("text", nargs="*")
    s.add_argument("-m", "--mode", choices=list(modem.MODES), default="binharic")
    s.add_argument("-k", "--key")
    s.add_argument("--speed", type=SPEED, default=1.0)
    s.add_argument("--volume", type=VOLUME, default=0.6)
    s.add_argument("--wav", help="write to this WAV file instead of playing")
    s.add_argument("--privacy", choices=["plain", "encrypted", "decoys"],
                   help="plain, encrypted, or encrypted + gibberish chirps (default: encrypted if -k, else plain)")
    s.add_argument("--decoys", type=_ranged(int, 0, 8, "decoys"), default=0,
                   help="gibberish chirps per message (implies --privacy decoys; default 3)")
    s.add_argument("--direct", action="store_true", help="play it yourself even if a layer is running")
    s.add_argument("--sink", default="auto", help="auto (Binharic Mic if set up), default (your speakers) or a name")
    s.add_argument("--output-device")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("decode", help="translate binharic back to text (mic, source or WAV)")
    s.add_argument("-m", "--mode", choices=["auto"] + list(modem.MODES), default="auto")
    s.add_argument("-k", "--key")
    s.add_argument("--speed", type=SPEED, default=1.0)
    s.add_argument("--wav")
    s.add_argument("--source", help="PipeWire source; 'speakers' decodes what your PC is playing")
    s.add_argument("--input-device")
    s.add_argument("--threshold", type=THRESHOLD, default=0.45, help="sync-chirp detection threshold (0.05-0.95)")
    s.set_defaults(func=cmd_decode)

    s = sub.add_parser("text", help="show text as binary/hex, or --reverse to read it back")
    s.add_argument("text", nargs="*")
    s.add_argument("-r", "--reverse", action="store_true")
    s.set_defaults(func=cmd_text)

    s = sub.add_parser("mic", help="create/remove the 'Binharic Mic' virtual microphone")
    s.add_argument("action", nargs="?", choices=["up", "down", "status"], default="status")
    s.set_defaults(func=cmd_mic)

    sub.add_parser("devices", help="list audio sinks, sources and devices").set_defaults(func=cmd_devices)
    sub.add_parser("doctor", help="quick check (no changes; `binharic fix` repairs)").set_defaults(func=cmd_doctor)
    s = sub.add_parser("fix", help="find problems and FIX them: mic, Bluetooth, virtual mic, models, GPU")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask, just fix")
    s.add_argument("--no-gpu", action="store_true", help="skip the (1.5 GB) GPU library download")
    s.set_defaults(func=cmd_fix)
    s = sub.add_parser("ish", help="iSH on iOS: dictate or type, get sound files")
    s.add_argument("-m", "--mode", choices=list(modem.MODES), default="binharic")
    s.add_argument("-k", "--key")
    s.add_argument("--privacy", choices=["plain", "encrypted", "decoys"])
    s.add_argument("--decoys", type=_ranged(int, 0, 8, "decoys"), default=0)
    s.add_argument("--speed", type=SPEED, default=1.0)
    s.set_defaults(func=cmd_ish)

    s = sub.add_parser("download-model", help="fetch an offline speech model")
    s.add_argument("name", nargs="?", help="e.g. small.en, medium.en, large-v3, or a vosk model name")
    s.add_argument("--engine", choices=["auto", "whisper", "vosk"], default="auto")
    s.set_defaults(func=cmd_download)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    code = 0
    try:
        result = args.func(args)
        if isinstance(result, str):  # a translator front end that stopped on a fatal error
            say_line(f"[bold red]✖ {esc(result)}[/]", f"error: {result}")
            code = 1
    except (RuntimeError, OSError) as e:
        say_line(f"[bold red]✖[/] {esc(e)}", f"error: {e}")
        code = 1
    except KeyboardInterrupt:
        code = 130
    if args.func in (cmd_listen, cmd_tui, cmd_gui, cmd_pick, cmd_test):
        # audio and Whisper threads live in C libraries; skipping interpreter
        # teardown avoids crashes on exit after everything was already closed
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    sys.exit(code)


if __name__ == "__main__":
    main()
