"""Command-line entry point: ``binharic <command>``."""

from __future__ import annotations

import argparse
import os
import signal
import sys

import numpy as np

from . import codec, modem

SR = modem.DEFAULT_SR


def _key(args) -> str | None:
    return args.key or os.environ.get("BINHARIC_KEY") or None


def _add_common(p, auto: bool = False):
    choices = (["auto"] if auto else []) + list(modem.MODES)
    p.add_argument("-m", "--mode", choices=choices, default="auto" if auto else "binharic",
                   help="binary = 0/1 beeps, hex = 16 tones, binharic = Mechanicus screech")
    p.add_argument("-k", "--key", help="shared cant-key; only holders can read it (or set BINHARIC_KEY)")
    p.add_argument("--speed", type=float, default=1.0, help="tempo multiplier (both sides must match)")


def cmd_listen(args):
    from . import listen
    args.key = _key(args)
    listen.run(args)


def cmd_say(args):
    text = " ".join(args.text) if args.text else sys.stdin.read().strip()
    audio = modem.encode_text(text, args.mode, key=_key(args), sr=SR, speed=args.speed, volume=args.volume)
    if args.wav:
        from .audio import write_wav
        write_wav(args.wav, audio, SR)
        print(f"wrote {args.wav} ({len(audio) / SR:.1f}s)")
        return
    from .audio import Output
    out = Output(SR, sink=args.sink, device=args.output_device)
    try:
        out.write(audio)
        out.write(np.zeros(SR // 4, dtype=np.float32))
    finally:
        out.close()


def cmd_decode(args):
    key = _key(args)
    if args.wav:
        from .audio import read_wav
        sig, sr = read_wav(args.wav)
        msgs = modem.decode_audio(sig, sr, args.mode, key=key, speed=args.speed, threshold=args.threshold)
        if not msgs:
            print("no transmissions found", file=sys.stderr)
            sys.exit(1)
        for m in msgs:
            print(m.display())
        return

    from .audio import Input
    mic = Input(SR, source=args.source, device=args.input_device)
    dec = modem.StreamDecoder(SR, args.mode, key=key, speed=args.speed, threshold=args.threshold)
    print("listening for binharic... (Ctrl+C to stop)", file=sys.stderr)
    try:
        while True:
            for m in dec.feed(mic.read()):
                tag = modem.MODES_BY_ID[m.mode_id].name if m.mode_id in modem.MODES_BY_ID else "?"
                print(f"<< [{tag}] {m.display()}", flush=True)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        mic.close()


def cmd_text(args):
    text = " ".join(args.text) if args.text else sys.stdin.read().strip()
    if args.reverse:
        t = text.strip()
        print(codec.from_binary(t) if set(t) <= set("01 \n\t") else codec.from_hex(t))
        return
    print("binary:", codec.to_binary(text))
    print("hex:   ", codec.to_hex(text))


def cmd_toggle(args):
    from . import listen
    listen.send_signal(signal.SIGUSR1)


def cmd_off(args):
    from . import listen
    listen.send_signal(signal.SIGUSR2)


def cmd_download(args):
    from . import listen
    listen.download_model(args.name)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="binharic", description="Speak in binary / hex / binharic screech.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("listen", help="live layer: your speech -> binharic on the output")
    _add_common(s)
    s.add_argument("--model", help="path to a Vosk model directory")
    s.add_argument("--sink", help="PulseAudio/PipeWire sink to play into (e.g. binharic_out)")
    s.add_argument("--source", help="PulseAudio/PipeWire source to record from (default: system mic)")
    s.add_argument("--output-device", help="PortAudio output device (when --sink is not used)")
    s.add_argument("--input-device", help="PortAudio input device (when --source is not used)")
    s.add_argument("--passthrough", action="store_true", help="when OFF, pass your normal voice through")
    s.add_argument("--start-off", action="store_true", help="start in the OFF state")
    s.add_argument("--full-duplex", action="store_true",
                   help="keep recognising while screeching (only safe when output isn't audible to the mic)")
    s.add_argument("--show", choices=["text", "hex", "binary"], default="text", help="what to print per phrase")
    s.add_argument("--volume", type=float, default=0.6)
    s.add_argument("-v", "--verbose", action="store_true", help="show partial recognition")
    s.set_defaults(func=cmd_listen)

    s = sub.add_parser("say", help="encode text and play it (or write a WAV)")
    _add_common(s)
    s.add_argument("text", nargs="*")
    s.add_argument("--wav", help="write to this WAV file instead of playing")
    s.add_argument("--sink")
    s.add_argument("--output-device")
    s.add_argument("--volume", type=float, default=0.6)
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("decode", help="decode binharic from the mic, a source, or a WAV")
    _add_common(s, auto=True)
    s.add_argument("--wav")
    s.add_argument("--source", help="PulseAudio/PipeWire source, e.g. a sink monitor like alsa_output...monitor")
    s.add_argument("--input-device")
    s.add_argument("--threshold", type=float, default=0.45, help="sync-chirp detection threshold (0-1)")
    s.set_defaults(func=cmd_decode)

    s = sub.add_parser("text", help="show text as binary/hex (or --reverse to read it back)")
    s.add_argument("text", nargs="*")
    s.add_argument("-r", "--reverse", action="store_true")
    s.set_defaults(func=cmd_text)

    sub.add_parser("toggle", help="toggle a running `listen` ON/OFF").set_defaults(func=cmd_toggle)
    sub.add_parser("off", help="force a running `listen` OFF").set_defaults(func=cmd_off)

    s = sub.add_parser("download-model", help="fetch an offline Vosk speech model")
    s.add_argument("name", nargs="?", default="vosk-model-small-en-us-0.15")
    s.set_defaults(func=cmd_download)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
