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
import os
import shutil
import sys

import numpy as np

from . import codec, modem

SR = modem.DEFAULT_SR

try:
    from rich.console import Console
    from rich.table import Table
    console = Console(highlight=False)
except ImportError:
    console = Table = None


def say_line(markup: str, plain: str | None = None) -> None:
    if console:
        console.print(markup)
    else:
        print(plain if plain is not None else markup)


def _key(value: str | None) -> str | None:
    return value or os.environ.get("BINHARIC_KEY") or None


# --------------------------------------------------------------------------
# translator front ends

def _settings(args):
    from . import pulse
    from .core import Settings
    from .stt import LIVE, PHRASE

    sink = pulse.resolve_sink(args.sink)
    model, live_model, beam, pause, partials = args.model, args.live_model, 5, args.pause_ms, True
    if args.fast:
        # one quick model, greedy decoding, shorter pause, no interim transcripts
        model, live_model, beam, partials = model or "base.en", None, 1, False
        pause = min(pause, 450)
    return Settings(
        enabled=not args.start_off,
        send_mode=LIVE if args.live else PHRASE,
        mode=args.mode, key=_key(args.key), speed=args.speed, volume=args.volume,
        passthrough=args.passthrough, engine=args.engine, model=model, live_model=live_model,
        device=args.device, beam=beam, partials=partials, test=getattr(args, "test_mode", False), test_air=getattr(args, "air", False),
        sink=sink, source=pulse.resolve_source(args.source),
        output_device=args.output_device, input_device=args.input_device,
        duplex=args.duplex, end_ms=pause,
        hear=pulse.resolve_source(args.hear), hear_key=_key(args.hear_key) if args.hear_key else None,
    )


def _translator(args):
    from .core import Translator
    return Translator(_settings(args))


def _run_ui(run, tr, **kw):
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


def cmd_listen(args):
    from . import ui_cli
    return _run_ui(ui_cli.run, _translator(args), show=args.show)


def cmd_test(args):
    from . import ui_cli
    from .core import send_command
    try:
        reply = json.loads(send_command("test"))
    except RuntimeError:
        pass  # nothing running: start our own test session below
    else:
        state = "ON" if reply["test"] else "OFF"
        say_line(f"[yellow]⚙ test mode {state} in the binharic that's already running[/] "
                 "[dim](results show in its window, or: journalctl --user -u binharic -f)[/]")
        return
    args.test_mode = True
    return _run_ui(ui_cli.run, _translator(args), show="text", test=True)


def cmd_tui(args):
    from . import ui_tui
    return _run_ui(ui_tui.run, _translator(args))


def cmd_gui(args):
    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.exit("Tk is missing: sudo pacman -S tk   (or use `binharic tui`)")
    from . import ui_gui
    return _run_ui(ui_gui.run, _translator(args))


def cmd_pick(args):
    """No subcommand: start the GUI on a desktop, the TUI in a terminal."""
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
    say_line(f"{on} · send {send} · mode {st['mode']} · key {'set' if st['key'] else 'none'}",
             f"{'ON' if st['enabled'] else 'OFF'} · send {send} · mode {st['mode']}")


def cmd_ctl(args):
    _control(args.command)


# --------------------------------------------------------------------------
# one-offs

def cmd_say(args):
    from . import pulse
    text = " ".join(args.text) if args.text else sys.stdin.read().strip()
    audio = modem.encode_text(text, args.mode, key=_key(args.key), sr=SR, speed=args.speed, volume=args.volume)
    if args.wav:
        from .audio import write_wav
        write_wav(args.wav, audio, SR)
        say_line(f"[green]wrote[/] {args.wav} [dim]({len(audio) / SR:.1f}s)[/]")
        return
    from .audio import Output
    out = Output(SR, sink=pulse.resolve_sink(args.sink), device=args.output_device)
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
        sig, sr = read_wav(args.wav)
        msgs = modem.decode_audio(sig, sr, args.mode, key=key, speed=args.speed, threshold=args.threshold)
        if not msgs:
            say_line("[yellow]no transmissions found[/]", "no transmissions found")
            sys.exit(1)
        for m in msgs:
            print(m.display())
        return

    from .audio import Input
    source = pulse.resolve_source(args.source)
    mic = Input(SR, source=source, device=args.input_device)
    dec = modem.StreamDecoder(SR, args.mode, key=key, speed=args.speed, threshold=args.threshold)
    say_line(f"[cyan]⚙ listening for binharic on {source or 'the default mic'}… (Ctrl+C stops)[/]")
    try:
        while True:
            for m in dec.feed(mic.read()):
                tag = modem.MODES_BY_ID[m.mode_id].name if m.mode_id in modem.MODES_BY_ID else "?"
                style = "yellow" if m.locked else "bold green"
                say_line(f"[{style}]▼ [{tag}] {m.display()}[/]", f"<< [{tag}] {m.display()}")
    except (KeyboardInterrupt, EOFError):
        for m in dec.flush():
            print(m.display())
    finally:
        mic.close()


def cmd_text(args):
    text = " ".join(args.text) if args.text else sys.stdin.read().strip()
    if args.reverse:
        t = text.strip()
        print(codec.from_binary(t) if set(t) <= set("01 \n\t") else codec.from_hex(t))
        return
    say_line(f"[dim]binary[/] {codec.to_binary(text)}", f"binary: {codec.to_binary(text)}")
    say_line(f"[dim]hex   [/] {codec.to_hex(text)}", f"hex:    {codec.to_hex(text)}")


def cmd_mic(args):
    from . import pulse
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


def cmd_devices(args):
    from . import pulse
    if pulse.available():
        say_line("[bold]PipeWire/Pulse sinks[/] [dim](--sink)[/]")
        for n in pulse.sinks():
            say_line(f"  {n}")
        say_line("[bold]PipeWire/Pulse sources[/] [dim](--source, --hear; 'speakers' = what you hear)[/]")
        for n in pulse.sources():
            say_line(f"  {n}")
    try:
        import sounddevice as sd
        say_line("[bold]PortAudio devices[/] [dim](--input-device / --output-device)[/]")
        for i, d in enumerate(sd.query_devices()):
            say_line(f"  {i:>2} {d['name']}  [dim]in {d['max_input_channels']} / out {d['max_output_channels']}[/]")
    except Exception as e:
        say_line(f"[yellow]PortAudio unavailable: {e}[/]")


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


def cmd_doctor(args):
    from . import pulse, stt

    def mod(name):
        try:
            __import__(name)
            return True
        except Exception:
            return False

    checks = [
        ("numpy", mod("numpy"), "rerun ./install.sh"),
        ("sounddevice / PortAudio", mod("sounddevice"), "sudo pacman -S portaudio"),
        ("pactl / pacat (PipeWire)", bool(shutil.which("pactl") and shutil.which("pacat")),
         "sudo pacman -S libpulse pipewire-pulse"),
        ("PipeWire/Pulse server", pulse.available(), "systemctl --user start pipewire-pulse"),
        ("Binharic Mic", pulse.virtual_mic_exists(), "binharic mic up"),
        ("Whisper (faster-whisper)", stt.have_whisper(), "rerun ./install.sh"),
        ("Vosk", mod("vosk"), "rerun ./install.sh"),
        *_gpu_checks(),
        ("Tk (for the GUI)", mod("tkinter"), "sudo pacman -S tk"),
        ("rich (pretty CLI)", mod("rich"), "rerun ./install.sh"),
        ("notify-send", bool(shutil.which("notify-send")), "sudo pacman -S libnotify"),
    ]
    for name, ok, fix in checks:
        say_line(f"  {'[green]✔[/]' if ok else '[red]✖[/]'} {name}" + ("" if ok else f"  [dim]→ {fix}[/]"),
                 f"  {'ok ' if ok else 'MISSING'} {name}" + ("" if ok else f"  -> {fix}"))


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
    g.add_argument("--speed", type=float, default=1.0, help="tempo multiplier, listeners must match")
    g.add_argument("--volume", type=float, default=0.6)

    g = p.add_argument_group("sending")
    g.add_argument("--live", action="store_true", help="screech words as you say them (default: after you finish)")
    g.add_argument("--pause-ms", type=int, default=700, help="silence that counts as 'finished' (ms)")
    g.add_argument("--start-off", action="store_true", help="start in the OFF state")
    g.add_argument("--passthrough", action="store_true", help="while OFF, send your normal voice")

    g = p.add_argument_group("speech recognition")
    g.add_argument("--engine", choices=["auto", "whisper", "vosk"], default="auto",
                   help="whisper = accurate (default when installed), vosk = light")
    g.add_argument("--model", help="whisper size (tiny.en base.en small.en medium.en large-v3) or vosk model dir")
    g.add_argument("--live-model", default="base.en",
                   help="faster whisper model for live words / interim text (default base.en)")
    g.add_argument("--device", default="auto", help="whisper compute device: auto, cpu, cuda")
    g.add_argument("--fast", action="store_true",
                   help="lowest latency: base.en only, greedy decoding, 450 ms pause, no live transcript")

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
    s = sub.add_parser("send", help="choose when to screech: toggle, live (as I talk), phrase (after I finish)")
    s.add_argument("when", nargs="?", choices=["toggle", "live", "phrase"], default="toggle")
    s.set_defaults(func=lambda a: _control(f"send {a.when}"))

    s = sub.add_parser("say", help="screech typed text (or write a WAV)")
    s.add_argument("text", nargs="*")
    s.add_argument("-m", "--mode", choices=list(modem.MODES), default="binharic")
    s.add_argument("-k", "--key")
    s.add_argument("--speed", type=float, default=1.0)
    s.add_argument("--volume", type=float, default=0.6)
    s.add_argument("--wav", help="write to this WAV file instead of playing")
    s.add_argument("--sink", default="auto")
    s.add_argument("--output-device")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("decode", help="translate binharic back to text (mic, source or WAV)")
    s.add_argument("-m", "--mode", choices=["auto"] + list(modem.MODES), default="auto")
    s.add_argument("-k", "--key")
    s.add_argument("--speed", type=float, default=1.0)
    s.add_argument("--wav")
    s.add_argument("--source", help="PipeWire source; 'speakers' decodes what your PC is playing")
    s.add_argument("--input-device")
    s.add_argument("--threshold", type=float, default=0.45, help="sync-chirp detection threshold (0-1)")
    s.set_defaults(func=cmd_decode)

    s = sub.add_parser("text", help="show text as binary/hex, or --reverse to read it back")
    s.add_argument("text", nargs="*")
    s.add_argument("-r", "--reverse", action="store_true")
    s.set_defaults(func=cmd_text)

    s = sub.add_parser("mic", help="create/remove the 'Binharic Mic' virtual microphone")
    s.add_argument("action", nargs="?", choices=["up", "down", "status"], default="status")
    s.set_defaults(func=cmd_mic)

    sub.add_parser("devices", help="list audio sinks, sources and devices").set_defaults(func=cmd_devices)
    sub.add_parser("doctor", help="check that everything is installed").set_defaults(func=cmd_doctor)

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
            say_line(f"[bold red]✖ {result}[/]", f"error: {result}")
            code = 1
    except RuntimeError as e:
        say_line(f"[bold red]✖[/] {e}", f"error: {e}")
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
