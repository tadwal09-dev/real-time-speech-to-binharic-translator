"""End-to-end tests: every command and feature, on a real sound system.

    python -m tests.e2e.run                  # everything this platform supports
    python -m tests.e2e.run -k listen        # only cases whose name contains "listen"
    python -m tests.e2e.run --repeat 3       # the whole suite 3 times; all must pass
    python -m tests.e2e.run --list

Talks to binharic the way a user does: by running the `binharic` command
(``BINHARIC_E2E_CMD`` overrides how, e.g. an installed copy). Speech comes
from the WAVs in fixtures/, played into a fake microphone; what "Discord"
would receive is recorded from the far end of the virtual mic and decoded.
See rig.py for the fake desktop on each platform.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from binharic import modem
from binharic.audio import read_wav, write_wav

from . import rig as rigmod
from .rig import SR, speech

WINDOWS = sys.platform == "win32"
ISH = bool(os.environ.get("BINHARIC_ISH")) or Path("/proc/ish").exists()
PLATFORM = "ish" if ISH else "windows" if WINDOWS else "linux"
HERE = Path(__file__).resolve().parent
KEY = "praise the omnissiah"

_cmd = os.environ.get("BINHARIC_E2E_CMD", "")
BH = ([_cmd] if os.path.exists(_cmd) or shutil.which(_cmd) else shlex.split(_cmd)) if _cmd else \
    [sys.executable, "-m", "binharic"]


# -- helpers ---------------------------------------------------------------------------

class Failed(AssertionError):
    pass


def check(cond, msg: str):
    if not cond:
        raise Failed(msg)


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def similar(a: str, b: str, ratio: float = 0.75) -> bool:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() >= ratio


def decode(audio: np.ndarray, key: str | None = None, mode: str = "auto") -> list[modem.Message]:
    return modem.decode_audio(np.asarray(audio, np.float32), SR, mode, key=key)


def texts(audio, key=None) -> list[str]:
    return [m.text for m in decode(audio, key) if not m.locked and m.text is not None]


def rms(x) -> float:
    x = np.asarray(x, np.float32)
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


class Result:
    def __init__(self, code: int, out: str):
        self.code, self.out = code, out

    def __repr__(self):
        return f"<exit {self.code}>\n{self.out[-3000:]}"


class Ctx:
    def __init__(self, rig, tmp: Path, artifacts: Path):
        self.rig = rig
        self.tmp = tmp
        self.artifacts = artifacts
        self.layers: list[Layer] = []
        self.recorders: list = []  # what they captured is saved as WAVs when a case fails
        self.env = dict(os.environ)
        # every test starts from a clean config (what `binharic fix` saves)
        self.env["XDG_CONFIG_HOME"] = str(tmp / "config")
        if WINDOWS:
            self.env["APPDATA"] = str(tmp / "config")
        self.env.pop("BINHARIC_KEY", None)

    def bh(self, *args: str, input: str | None = None, timeout: float = 120, env: dict | None = None) -> Result:
        p = subprocess.run(BH + list(args), input=input.encode() if input is not None else None,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                           env=env or self.env, stdin=None if input is not None else subprocess.DEVNULL)
        return Result(p.returncode, p.stdout.decode("utf-8", "replace"))

    def ok(self, *args: str, **kw) -> Result:
        r = self.bh(*args, **kw)
        check(r.code == 0, f"`binharic {' '.join(args)}` failed: {r}")
        check("Traceback" not in r.out, f"`binharic {' '.join(args)}` printed a traceback: {r}")
        return r

    def fails(self, *args: str, want: str | None = None, **kw) -> Result:
        r = self.bh(*args, **kw)
        check(r.code != 0, f"`binharic {' '.join(args)}` should fail: {r}")
        check("Traceback" not in r.out, f"`binharic {' '.join(args)}` printed a traceback instead of an error: {r}")
        if want:
            check(want.lower() in r.out.lower(), f"`binharic {' '.join(args)}` should say {want!r}: {r}")
        return r

    def record(self, where: str = "vmic"):
        rec = self.rig.record(where)
        self.recorders.append(rec)
        return rec

    def layer(self, *args: str, cmd: str = "listen", ready: bool = True, rig_args: bool = True) -> "Layer":
        lay = Layer(self, cmd, (self.rig.layer_args() if rig_args else []) + list(args))
        if ready:
            lay.wait_for(r"ready \(", 300)
            time.sleep(0.5)
        return lay

    def close(self):
        for lay in self.layers:
            lay.kill()
        for rec in self.recorders:
            rec.stop()


class Layer:
    """A running `binharic listen` (or test/tui...) with its output captured."""

    def __init__(self, ctx: Ctx, cmd: str | None, args: list[str], env: dict | None = None):
        self.ctx = ctx
        self.args = ([cmd] if cmd else []) + list(args)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
        self.proc = subprocess.Popen(BH + self.args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, env=env or ctx.env, creationflags=flags)
        ctx.layers.append(self)  # killed by ctx.close() whatever happens
        self.lines: list[str] = []
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self):
        for raw in self.proc.stdout:
            self.lines.append(raw.decode("utf-8", "replace").rstrip())

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def mark(self) -> int:
        return len(self.lines)

    def wait_for(self, pattern: str, timeout: float = 60, since: int = 0) -> str:
        rx = re.compile(pattern)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for line in self.lines[since:]:
                if rx.search(line):
                    return line
            if self.proc.poll() is not None:
                time.sleep(0.3)
                for line in self.lines[since:]:
                    if rx.search(line):
                        return line
                raise Failed(f"binharic {' '.join(self.args)} exited ({self.proc.returncode}) before "
                             f"printing /{pattern}/:\n{self.text[-3000:]}")
            time.sleep(0.1)
        raise Failed(f"binharic {' '.join(self.args)}: no /{pattern}/ within {timeout:.0f}s:\n{self.text[-3000:]}")

    def status(self) -> dict:
        from binharic.core import send_command
        return json.loads(send_command("status"))

    def idle(self, timeout: float = 60, settle: float = 1.0) -> None:
        """Wait until nothing is being said, recognised or screeched."""
        end = time.monotonic() + timeout
        quiet_since = None
        while time.monotonic() < end:
            st = self.status()
            busy = st["screeching"] or st["queued"] or st["speaking"]
            if busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= settle:
                return
            time.sleep(0.1)
        raise Failed(f"the layer never went idle:\n{self.text[-2000:]}")

    def stop(self, timeout: float = 20) -> int:
        """Shut down the way a user/system would: SIGTERM, or Ctrl+Break on Windows."""
        if self.proc.poll() is None:
            if WINDOWS:
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.send_signal(signal.SIGTERM)
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise Failed(f"binharic {' '.join(self.args)} didn't stop within {timeout}s:\n{self.text[-2000:]}") from None
        self._t.join(timeout=3)
        return code

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.proc.poll() is None:
            code = self.stop()
            if exc[0] is None:
                check(code == 0, f"binharic {' '.join(self.args)} exited with {code} on shutdown:\n{self.text[-2000:]}")
        if exc[0] is None:
            check("Traceback" not in self.text, f"binharic printed a traceback:\n{self.text[-4000:]}")


def say_and_record(ctx: Ctx, lay: Layer, phrase: str, where: str = "vmic", settle: float = 1.0) -> np.ndarray:
    """Speak a fixture phrase into the mic, wait for the layer to finish, return what came out."""
    rec = ctx.record(where)
    heard = ctx.record("mic")  # what the mic delivered: saved with the rest if the case fails
    try:
        time.sleep(0.3)
        ctx.rig.play(speech(phrase))
        time.sleep(1.0)
        lay.idle(90, settle)
    finally:
        audio = rec.stop()
        heard.stop()
    return audio


PHRASES = {"hello": "hello world", "machine": "the machine spirit is pleased",
           "gate": "open the gate now", "flesh": "the flesh is weak"}


# -- registry ---------------------------------------------------------------------------

CASES: list[tuple[str, set[str], object]] = []
ALL, AUDIO = {"linux", "windows", "ish"}, {"linux", "windows"}


def case(platforms=AUDIO):
    def deco(fn):
        CASES.append((fn.__name__, set(platforms), fn))
        return fn
    return deco


# == offline: no sound devices needed ==================================================

@case(ALL)
def help_for_every_command(ctx):
    r = ctx.ok("--help")
    subs = re.findall(r"^\s{4}(\w[\w-]*)\s", r.out, re.M)
    check(len(subs) >= 18, f"expected the full command list in --help, got {subs}")
    for s in subs:
        ctx.ok(s, "--help")


@case(ALL)
def text_conversions(ctx):
    r = ctx.ok("text", "Hi")
    check("01001000 01101001" in r.out and "48 69" in r.out, r)
    check(ctx.ok("text", "-r", "01001000 01101001").out.strip() == "Hi", "binary -> text")
    check(ctx.ok("text", "-r", "48 69").out.strip() == "Hi", "hex -> text")
    check("48 69" in ctx.ok("text", input="Hi\n").out, "text from a pipe")
    ctx.fails("text", "-r", "4")
    ctx.fails("text", want="nothing to convert")


@case(ALL)
def say_wav_and_decode_every_mode_and_speed(ctx):
    for mode in modem.MODES:
        for speed in ("0.5", "1", "2"):
            wav = str(ctx.tmp / f"{mode}-{speed}.wav")
            ctx.ok("say", "--wav", wav, "-m", mode, "--speed", speed, f"{mode} at {speed}")
            r = ctx.ok("decode", "--wav", wav, "--speed", speed)
            check(f"{mode} at {speed}" in r.out, f"{mode} x{speed}: {r}")
            r = ctx.ok("decode", "--wav", wav, "--speed", speed, "-m", mode)
            check(f"{mode} at {speed}" in r.out, f"{mode} x{speed} with -m: {r}")


@case(ALL)
def say_wav_three_privacy_levels(ctx):
    plain, enc, dec = (str(ctx.tmp / f"{n}.wav") for n in ("plain", "enc", "dec"))
    ctx.ok("say", "--wav", plain, "-k", KEY, "--privacy", "plain", "in the open")
    check("in the open" in ctx.ok("decode", "--wav", plain).out, "plain: anyone can read it")

    ctx.ok("say", "--wav", enc, "-k", KEY, "a secret")      # a key alone means encrypted
    r = ctx.ok("decode", "--wav", enc)
    check("a secret" not in r.out and "[encrypted" in r.out, f"encrypted without the key: {r}")
    check("a secret" in ctx.ok("decode", "--wav", enc, "-k", KEY).out, "the key reads it")
    r = ctx.fails("decode", "--wav", enc, "-k", "heresy")
    check("a secret" not in r.out and "don't match your key" in r.out, f"wrong key: {r}")
    check("a secret" in ctx.ok("decode", "--wav", enc, input="", env=dict(ctx.env, BINHARIC_KEY=KEY)).out,
          "BINHARIC_KEY")

    ctx.ok("say", "--wav", dec, "-k", KEY, "--privacy", "decoys", "--decoys", "2", "hidden")
    r = ctx.ok("decode", "--wav", dec)
    check(r.out.count("[encrypted") == 3 and "hidden" not in r.out, f"2 decoys + 1 real, all locked: {r}")
    r = ctx.ok("decode", "--wav", dec, "-k", KEY)
    check(r.out.count("hidden") == 1 and "2 frame(s) don't match" in r.out, f"key holder sees one message: {r}")
    x, _ = read_wav(dec)
    y, _ = read_wav(enc)
    check(len(x) > 2 * len(y), "decoys make the screech longer")

    ctx.fails("say", "--wav", plain, "--privacy", "encrypted", "x", want="needs a key")
    ctx.fails("say", "--wav", plain, "--privacy", "decoys", "x", want="needs a key")


@case(ALL)
def say_wav_unicode_long_and_piped(ctx):
    wav = str(ctx.tmp / "u.wav")
    msg = "héllo wörld ✓ 日本語 — praise"
    ctx.ok("say", "--wav", wav, msg)
    check(msg in ctx.ok("decode", "--wav", wav).out, "unicode round trip")
    long = " ".join(f"word{i}" for i in range(80))  # > 240 bytes: several frames
    ctx.ok("say", "--wav", wav, "-m", "hex", "--speed", "3", long)
    out = ctx.ok("decode", "--wav", wav, "--speed", "3").out
    check(norm(out).replace(" ", "") .find(norm(long).replace(" ", "")) >= 0, f"long text in chunks: {out[:300]}")
    ctx.ok("say", "--wav", wav, input="from a pipe\n")
    check("from a pipe" in ctx.ok("decode", "--wav", wav).out, "piped text")
    ctx.fails("say", "--wav", wav, input="", want="nothing to say")


@case(ALL)
def decode_other_inputs(ctx):
    wav = str(ctx.tmp / "x.wav")
    ctx.ok("say", "--wav", wav, "resampled")
    x, sr = read_wav(wav)
    for rate in (16000, 22050, 44100):
        t = np.arange(int(len(x) * rate / sr)) / rate
        y = np.interp(t, np.arange(len(x)) / sr, x)
        p = str(ctx.tmp / f"r{rate}.wav")
        write_wav(p, y, rate)
        check("resampled" in ctx.ok("decode", "--wav", p).out, f"decode at {rate} Hz")
    if shutil.which("ffmpeg"):
        m4a = str(ctx.tmp / "x.m4a")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", wav, "-c:a", "aac", "-b:a", "128k", m4a], check=True)
        check("resampled" in ctx.ok("decode", "--wav", m4a).out, "decode a Voice Memos m4a")
    silence = str(ctx.tmp / "silence.wav")
    write_wav(silence, np.zeros(SR), SR)
    ctx.fails("decode", "--wav", silence, want="no transmissions")
    junk = ctx.tmp / "junk.wav"
    junk.write_bytes(b"RIFF not really a wav")
    ctx.fails("decode", "--wav", str(junk), want="can't read")
    ctx.fails("decode", "--wav", str(ctx.tmp / "missing.wav"))


@case(ALL)
def bad_arguments_give_one_line_errors(ctx):
    for args, want in [(("say", "--speed", "9", "x"), "speed must be"),
                       (("say", "--volume", "0", "x"), "volume must be"),
                       (("say", "--decoys", "99", "x"), "decoys must be"),
                       (("decode", "--threshold", "2"), "threshold must be"),
                       (("listen", "--pause-ms", "5"), "pause"),
                       (("say", "-m", "morse", "x"), "invalid choice"),
                       (("privacy", "secret"), "invalid choice")]:
        ctx.fails(*args, want=want)


@case(ALL)
def control_commands_without_a_running_layer(ctx):
    for cmd in (["status"], ["toggle"], ["on"], ["off"], ["send", "live"], ["privacy", "plain"]):
        ctx.fails(*cmd, want="not running")


# == iSH ==================================================================================

@case({"ish"})
def ish_text_to_sound_files(ctx):
    home = ctx.tmp / "home"
    env = dict(ctx.env, HOME=str(home))
    r = ctx.bh(input="first line\nsecond line\n\n", env=env)  # no subcommand: the iSH prompt
    check(r.code == 0 and "UNENCRYPTED" in r.out, r)
    files = sorted((home / "binharic").glob("2*.wav"))  # by name, as the Files app shows them
    check(len(files) == 2, f"one WAV per line: {files}")
    got = [ctx.ok("decode", "--wav", str(f)).out.strip() for f in files]
    check(got == ["first line", "second line"], got)
    check("second line" in ctx.ok("decode", "--wav", str(home / "binharic" / "latest.wav")).out, "latest.wav")
    r = ctx.bh("ish", "-k", KEY, "--privacy", "decoys", input="secret line\n", env=env)
    check(r.code == 0 and "gibberish" in r.out, r)
    out = ctx.ok("decode", "--wav", str(home / "binharic" / "latest.wav"), "-k", KEY).out
    check("secret line" in out and "don't match your key" in out, out)
    r = ctx.ok("say", "sent from say", env=env)
    check("wrote" in r.out, r)
    check("sent from say" in ctx.ok("decode", "--wav", str(home / "binharic" / "latest.wav")).out, "say on iSH")
    for cmd in ("listen", "tui", "gui", "test"):
        ctx.fails(cmd, want="iSH can't use")
    r = ctx.bh("fix", "--no-gpu", env=env)
    check("encoding and decoding work" in r.out and "output folder works" in r.out, r)


# == devices and setup ==================================================================

@case({"linux"})
def virtual_mic_up_status_down(ctx):
    ctx.ok("mic", "down")
    check("not set up" in ctx.ok("mic", "status").out, "status after down")
    r = ctx.ok("mic", "up")
    check("ready" in r.out, r)
    check("present" in ctx.ok("mic", "status").out, "status after up")
    ctx.ok("mic", "up")  # twice is fine
    sinks = rigmod.LinuxRig.pactl("list", "short", "sinks")
    check(sinks.count("binharic_out") == 1, "no duplicate sink")
    check(rigmod.LinuxRig.pactl("get-default-sink").strip() == ctx.rig.SPEAKERS, "default output untouched")
    check(rigmod.LinuxRig.pactl("get-default-source").strip() == ctx.rig.MIC, "default input untouched")


@case({"windows"})
def virtual_mic_command_on_windows(ctx):
    r = ctx.bh("mic", "status")
    check("Traceback" not in r.out and "VB-Audio" in r.out and "CABLE" in r.out, f"explains VB-Cable: {r}")
    r = ctx.bh("mic", "up")
    check("Traceback" not in r.out and "CABLE" in r.out, r)


@case(AUDIO)
def devices_lists_the_rig(ctx):
    r = ctx.ok("devices")
    if WINDOWS:
        check("CABLE Input" in r.out and "Hi-Fi Cable Output" in r.out, r)
    else:
        check("alsa_input.e2e_mic" in r.out and "binharic_mic" in r.out and "← binharic uses this" in r.out, r)


@case(AUDIO)
def doctor_checks(ctx):
    r = ctx.bh("doctor")
    check("Traceback" not in r.out and "numpy" in r.out and "Whisper" in r.out, r)
    if not WINDOWS:
        check(re.search(r"(✔|ok ).*microphone: E2E_Mic", r.out), f"doctor hears the mic: {r}")


@case(AUDIO)
def download_models(ctx):
    r = ctx.ok("download-model", "base.en", timeout=900)
    check("base.en" in r.out or "whisper" in r.out, r)
    r = ctx.ok("download-model", "--engine", "vosk", timeout=900)
    check("vosk" in r.out, r)


@case(AUDIO)
def fix_repairs_and_checks_the_virtual_mic(ctx):
    if not WINDOWS:
        ctx.ok("mic", "down")
        # what PipeWire sometimes does to a new virtual device: make it the default
        rigmod.LinuxRig.pactl("load-module", "module-null-sink", "sink_name=binharic_out")
        rigmod.LinuxRig.pactl("set-default-sink", "binharic_out")
    talk = threading.Thread(target=lambda: [ctx.rig.play(speech(p)) for p in ("hello", "machine", "gate")])
    talk.start()
    r = ctx.bh("fix", "-y", "--no-gpu", timeout=900)
    talk.join()
    check("Traceback" not in r.out, r)
    if WINDOWS:
        check("VB-Audio Virtual Cable is installed" in r.out, r)
        check("went through the cable and decoded perfectly" in r.out, r)
    else:
        check("decoded perfectly" in r.out, f"loop check: {r}")
        check("set it back to E2E_Speakers" in r.out, f"un-hijacked the default output: {r}")
        check(rigmod.LinuxRig.pactl("get-default-sink").strip() == ctx.rig.SPEAKERS, "default output restored")
        check("hears you" in r.out and "saved" in r.out, f"found the mic: {r}")
        cfg = json.loads((ctx.tmp / "config" / "binharic" / "config.json").read_text())
        check(cfg.get("source") == ctx.rig.MIC, f"saved the mic: {cfg}")
        check("model base.en" in r.out, r)
        ctx.ok("mic", "down")  # the bare sink made above: the next case gets a proper one again


# == one-off sound ======================================================================

@case(AUDIO)
def say_into_the_virtual_mic(ctx):
    rec = ctx.record("vmic")
    ctx.ok("say", "straight to discord")
    got = texts(rec.stop())
    check(got == ["straight to discord"], f"say -> virtual mic: {got}")
    rec = ctx.record("vmic")
    ctx.ok("say", "-k", KEY, "--privacy", "decoys", "--decoys", "1", "-m", "hex", "quietly")
    audio = rec.stop()
    check(texts(audio, KEY) == ["quietly"] and texts(audio) == [], f"encrypted say: {decode(audio, KEY)}")
    check(len(decode(audio)) == 2, f"one decoy and the message: {decode(audio)}")


@case({"linux"})
def say_to_the_speakers(ctx):
    rec = ctx.record("speakers")
    ctx.ok("say", "--sink", "default", "out loud")
    check(texts(rec.stop()) == ["out loud"], "say --sink default plays on the speakers")


@case(AUDIO)
def decode_live_from_a_device(ctx):
    lay = Layer(ctx, "decode", ctx.rig.decode_args("vmic") + ["-k", KEY])
    lay.wait_for("listening for binharic", 30)
    ctx.record("vmic")  # the same audio the decoder gets, saved if this fails
    time.sleep(1)
    ctx.ok("say", "-k", KEY, "--privacy", "decoys", "--decoys", "2", "live decoding")
    lay.wait_for("live decoding", 30)
    time.sleep(2)
    lay.stop()
    check(lay.text.count("live decoding") == 1 and "[encrypted" not in lay.text, f"decoys hidden:\n{lay.text}")


# == the layer ==========================================================================

@case(AUDIO)
def listen_after_you_finish(ctx):
    with ctx.layer() as lay:
        check("UNENCRYPTED" in lay.text, "says it's unencrypted")
        for phrase in ("hello", "machine"):
            mark = lay.mark()
            audio = say_and_record(ctx, lay, phrase)
            got = " ".join(texts(audio))
            check(similar(got, PHRASES[phrase]), f"heard {got!r} for {PHRASES[phrase]!r}:\n{lay.text[-1500:]}")
            check(lay.wait_for("▲|>>", 1, since=mark), "logged the sent message")
            # your voice is muted: before the screech, nothing of the speech reached the virtual mic
            first = _first_chirp(audio)
            check(first > SR and rms(audio[:first - SR // 4]) < 0.003,
                  f"voice leaked into the virtual mic (rms {rms(audio[:max(1, first - SR // 4)]):.4f})")


def _first_chirp(audio: np.ndarray) -> int:
    loud = np.flatnonzero(np.abs(audio) > 0.05)
    return int(loud[0]) if len(loud) else len(audio)


@case(AUDIO)
def listen_as_you_talk(ctx):
    with ctx.layer("--live") as lay:
        check(lay.status()["send_mode"] == "live", "live mode")
        audio = say_and_record(ctx, lay, "machine", settle=2.0)
        got = " ".join(texts(audio))
        check(similar(got, PHRASES["machine"], 0.7), f"live: {got!r}:\n{lay.text[-1500:]}")


@case(AUDIO)
def listen_encrypted_and_with_decoys(ctx):
    with ctx.layer("-k", KEY) as lay:
        check(lay.status()["privacy"] == "encrypted", "a key means encrypted")
        audio = say_and_record(ctx, lay, "hello")
        check(texts(audio) == [] and all(m.locked for m in decode(audio)), f"readable without the key: {decode(audio)}")
        check(similar(" ".join(texts(audio, KEY)), PHRASES["hello"]),
              f"the key reads {texts(audio, KEY)} from {decode(audio, KEY)}:\n{lay.text[-1500:]}")
        check(texts(audio, "heresy") == [], "a wrong key doesn't")
    with ctx.layer("-k", KEY, "--privacy", "decoys", "--decoys", "2") as lay:
        audio = say_and_record(ctx, lay, "gate")
        frames = decode(audio)
        check(len(frames) >= 3 and all(m.locked for m in frames), f"real + 2 decoys, all locked: {frames}")
        real = texts(audio, KEY)
        check(len(real) >= 1 and similar(" ".join(real), PHRASES["gate"]), f"key holder: {real}")
    with ctx.layer("-k", KEY, "--privacy", "plain") as lay:
        audio = say_and_record(ctx, lay, "hello")
        check(similar(" ".join(texts(audio)), PHRASES["hello"]), f"key kept but sent in the open: {texts(audio)}:\n{lay.text[-1500:]}")
    ctx.fails("listen", "--privacy", "encrypted", want="needs a key")


@case(AUDIO)
def listen_hex_and_binary_modes(ctx):
    for mode in ("hex", "binary"):
        with ctx.layer("-m", mode) as lay:
            audio = say_and_record(ctx, lay, "hello")
            frames = decode(audio)
            check(frames and all(m.mode_id == modem.MODES[mode].id for m in frames), f"{mode}: {frames}")
            check(similar(" ".join(texts(audio)), PHRASES["hello"]), f"{mode}: {texts(audio)}")


@case(AUDIO)
def layer_sound_options(ctx):
    # --speed (both sides must match), --show, --volume and --duplex
    with ctx.layer("--speed", "1.5", "--show", "hex", "--volume", "0.3", "--duplex", "full") as lay:
        st = lay.status()
        check(st["speed"] == 1.5 and st["full_duplex"] and abs(st["volume"] - 0.3) < 1e-6, st)
        audio = say_and_record(ctx, lay, "hello")
        fast = [m.text for m in modem.decode_audio(audio, SR, speed=1.5) if m.text]
        check(similar(" ".join(fast), PHRASES["hello"]), f"--speed 1.5: {fast}")
        check(float(np.abs(audio).max()) < 0.4, f"--volume 0.3: peak {np.abs(audio).max():.2f}")
        hexed = " ".join(f"{b:02X}" for b in fast[0].encode())
        check(any(hexed in line.upper() for line in lay.lines), f"--show hex prints the hex: {hexed}\n{lay.text[-800:]}")
    rec = ctx.record("vmic")
    ctx.ok("say", "--volume", "0.9", "loud")
    loud = rec.stop()
    rec = ctx.record("vmic")
    ctx.ok("say", "--volume", "0.1", "quiet")
    quiet = rec.stop()
    check(texts(loud) == ["loud"] and texts(quiet) == ["quiet"], "both decode")
    check(np.abs(loud).max() > 4 * np.abs(quiet).max(), "say --volume")


@case(AUDIO)
def model_and_device_options(ctx):
    with ctx.layer("--model", "tiny.en", "--device", "cpu") as lay:
        check("tiny.en" in lay.status()["engine_name"] and "CPU" in lay.status()["engine_name"], lay.status())
        audio = say_and_record(ctx, lay, "hello")
        check(similar(" ".join(texts(audio)), PHRASES["hello"], 0.6), f"tiny.en: {texts(audio)}")


@case(AUDIO)
def controls_of_a_running_layer(ctx):
    with ctx.layer("-k", KEY, "--privacy", "plain") as lay:
        ctx.fails("listen", *ctx.rig.layer_args(), want="already running")
        check("ON" in ctx.ok("status").out, "status")
        check("OFF" in ctx.ok("off").out and not lay.status()["enabled"], "off")
        audio = say_and_record(ctx, lay, "hello")
        check(decode(audio) == [], "nothing is sent while OFF")
        check("ON" in ctx.ok("on").out, "on")
        audio = say_and_record(ctx, lay, "hello")
        check(similar(" ".join(texts(audio)), PHRASES["hello"]), f"sent again after ON: {texts(audio)}:\n{lay.text[-1500:]}")
        check("OFF" in ctx.ok("toggle").out and "ON" in ctx.ok("toggle").out, "toggle twice")
        check("as you talk" in ctx.ok("send", "live").out, "send live")
        check("after you finish" in ctx.ok("send", "toggle").out, "send toggle")
        check("as you talk" in ctx.ok("send").out and "after you finish" in ctx.ok("send", "phrase").out, "send")
        for level, word in (("encrypted", "encrypted"), ("decoys", "gibberish"), ("plain", "UNENCRYPTED")):
            check(word in ctx.ok("privacy", level).out and lay.status()["privacy"] == level, level)
        rec = ctx.record("vmic")
        r = ctx.ok("say", "typed into the layer")
        check("queued in the running binharic" in r.out, r)
        lay.idle()
        check(texts(rec.stop()) == ["typed into the layer"], "say goes through the running layer")
        check("test mode is now ON" in ctx.ok("test").out and lay.status()["test"], "test toggles test mode on")
        check("test mode OFF" in ctx.ok("test").out and not lay.status()["test"], "...and off")


@case(AUDIO)
def start_off_and_passthrough(ctx):
    with ctx.layer("--start-off") as lay:
        check(not lay.status()["enabled"], "--start-off")
        audio = say_and_record(ctx, lay, "hello")
        check(decode(audio) == [] and rms(audio) < 0.003, "OFF and no passthrough: silence")
    with ctx.layer("--start-off", "--passthrough") as lay:
        audio = say_and_record(ctx, lay, "hello")
        check(decode(audio) == [] and rms(audio) > 0.01, f"passthrough: your voice (rms {rms(audio):.4f})")
        ctx.ok("on")
        audio = say_and_record(ctx, lay, "hello")
        first = _first_chirp(audio)
        check(texts(audio) and rms(audio[:max(1, first - SR // 4)]) < 0.003, "ON mutes the voice again")


@case(AUDIO)
def hear_incoming_binharic(ctx):
    with ctx.layer("--start-off", *ctx.rig.hear_args()) as lay:
        lay.wait_for("decoding incoming binharic", 10)
        ctx.record(ctx.rig.hear_from)  # what --hear receives, saved if this fails
        ctx.rig.play(modem.encode_text("message from a friend", sr=SR), ctx.rig.hear_from)
        lay.wait_for("▼ message from a friend|<< message from a friend", 20)
    # a friend with their own key: --hear-key reads it, and hides their decoys
    with ctx.layer("--start-off", "-k", KEY, "--hear-key", "friends key", *ctx.rig.hear_args()) as lay:
        lay.wait_for("decoding incoming binharic", 10)
        ctx.rig.play(modem.encode_text("their secret", key="friends key", sr=SR, decoys=2), ctx.rig.hear_from)
        lay.wait_for("▼ their secret|<< their secret", 30)
        time.sleep(2)
        check("[encrypted" not in lay.text, f"decoys stay hidden:\n{lay.text[-1000:]}")


@case(AUDIO)
def stops_cleanly_and_recovers_from_a_crash(ctx):
    from binharic.core import socket_path
    lay = ctx.layer()
    check(socket_path().exists(), "control socket while running")
    code = lay.stop()
    check(code == 0, f"clean exit code, got {code}:\n{lay.text[-1500:]}")
    check(not socket_path().exists(), "control socket removed on exit")
    lay = ctx.layer()
    lay.kill()  # a crash: the socket stays behind
    check(socket_path().exists(), "stale socket after a crash")
    with ctx.layer() as lay:
        check(lay.status()["enabled"], "starts fine over a stale socket")


@case(AUDIO)
def test_mode_round_trip_and_over_the_air(ctx):
    loop = None
    if not WINDOWS:  # Windows: the default output is the rig's mic cable, which is "the air"
        loop = rigmod.LinuxRig.pactl("load-module", "module-loopback",
                                     f"source={ctx.rig.SPEAKERS}.monitor", f"sink={ctx.rig.ROOM}",
                                     "latency_msec=30").strip()
    try:
        lay = ctx.layer("--air", cmd="test")
        with lay:
            ctx.record("mic")  # what the mic heard (the speech, then the screech over the air)
            rec = ctx.record("vmic")
            ctx.rig.play(speech("hello"))
            lay.wait_for("round trip OK|OK round trip", 60)
            lay.wait_for("over the air OK|OK over the air", 60)
            lay.wait_for("⏱|latency", 5)
            lay.idle()
            check(decode(rec.stop()) == [], "test mode sends nothing to the virtual mic")
    finally:
        if loop:
            rigmod.LinuxRig.pactl("unload-module", loop)


@case(AUDIO)
def vosk_engine(ctx):
    ctx.ok("download-model", "--engine", "vosk", timeout=900)
    with ctx.layer("--engine", "vosk") as lay:
        check("vosk" in lay.status()["engine_name"], lay.status())
        audio = say_and_record(ctx, lay, "hello")
        check(similar(" ".join(texts(audio)), PHRASES["hello"], 0.6), f"vosk: {texts(audio)}")


@case(AUDIO)
def accurate_whisper(ctx):
    with ctx.layer("--accurate") as lay:
        check("small.en" in lay.status()["engine_name"], lay.status())
        audio = say_and_record(ctx, lay, "machine")
        check(similar(" ".join(texts(audio)), PHRASES["machine"]), f"--accurate: {texts(audio)}")


@case({"linux"})
def survives_a_sound_server_restart(ctx):
    with ctx.layer() as lay:
        mark = lay.mark()
        ctx.rig.restart_server()
        # both the mic and the output reconnect, and the Binharic Mic comes back
        end = time.monotonic() + 60
        while sum("audio reconnected" in line for line in lay.lines[mark:]) < 2 and time.monotonic() < end:
            time.sleep(0.2)
        lay.wait_for("re-created the Binharic Mic", 5, since=mark)
        check(sum("audio reconnected" in line for line in lay.lines[mark:]) >= 2,
              f"mic and output should both reconnect:\n{lay.text[-2000:]}")
        time.sleep(1)
        audio = say_and_record(ctx, lay, "hello")
        check(similar(" ".join(texts(audio)), PHRASES["hello"]), f"after the restart: {texts(audio)}:\n{lay.text[-2000:]}")


@case({"linux"})
def ignores_hijacked_defaults(ctx):
    rigmod.LinuxRig.pactl("set-default-source", "binharic_mic")
    rigmod.LinuxRig.pactl("set-default-sink", "binharic_out")
    try:
        with ctx.layer() as lay:
            check("E2E_Mic" in lay.text, f"uses the real mic, not the Binharic Mic:\n{lay.text}")
            audio = say_and_record(ctx, lay, "hello")
            check(similar(" ".join(texts(audio)), PHRASES["hello"]), f"works anyway: {texts(audio)}:\n{lay.text[-1500:]}")
    finally:
        ctx.rig.set_defaults()


@case({"linux"})
def no_subcommand_picks_a_front_end(ctx):
    env = dict(ctx.env)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    lay = Layer(ctx, None, ctx.rig.layer_args(), env=env)
    lay.wait_for(r"ready \(", 300)
    check("hotkeys" in lay.text, "no display, no terminal: the plain log")
    check(lay.stop() == 0, "stops")


@case(AUDIO)
def gui_every_control(ctx):
    _driver(ctx, "gui_driver", 420)


@case(AUDIO)
def tui_every_key(ctx):
    _driver(ctx, "tui_driver", 420)


def _driver(ctx, name: str, timeout: float):
    env = dict(ctx.env, BINHARIC_E2E_ARTIFACTS=str(ctx.artifacts))
    p = subprocess.run([sys.executable, "-m", f"tests.e2e.{name}", *ctx.rig.layer_args()], cwd=str(HERE.parent.parent),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, env=env)
    out = p.stdout.decode("utf-8", "replace")
    (ctx.artifacts / f"{name}.log").write_text(out, encoding="utf-8")
    check(p.returncode == 0 and "ALL OK" in out, f"{name} failed:\n{out[-5000:]}")


# == runner =============================================================================

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows CI logs are cp1252 otherwise
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", "--only", action="append", default=[], help="run cases whose name contains this")
    ap.add_argument("--skip", action="append", default=[])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--artifacts", default=os.environ.get("BINHARIC_E2E_ARTIFACTS", "e2e-artifacts"))
    a = ap.parse_args(argv)
    chosen = [(n, f) for n, plats, f in CASES if PLATFORM in plats
              and (not a.only or any(o in n for o in a.only)) and not any(s in n for s in a.skip)]
    if a.list:
        for n, _ in chosen:
            print(n)
        return 0
    artifacts = Path(a.artifacts).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    rig = None
    if PLATFORM != "ish":
        rig = rigmod.make_rig()
        rig.up()
    print(f"binharic e2e on {PLATFORM} ({rig.server() if rig else 'no audio'}) · {len(chosen)} cases"
          f" × {a.repeat}", flush=True)
    report, failed_any = [], False
    try:
        for rnd in range(1, a.repeat + 1):
            failures = 0
            for name, fn in chosen:
                tmp = Path(tempfile.mkdtemp(prefix="bh-e2e-"))
                ctx = Ctx(rig, tmp, artifacts)
                t = time.monotonic()
                print(f"[{rnd}] {name} …", end=" ", flush=True)
                err = None
                try:
                    fn(ctx)
                except Exception:
                    err = traceback.format_exc()
                finally:
                    ctx.close()
                    if rig:
                        rig.reset()
                    shutil.rmtree(tmp, ignore_errors=True)
                secs = time.monotonic() - t
                print(("ok" if err is None else "FAILED") + f" ({secs:.0f}s)", flush=True)
                if err:
                    failures += 1
                    for i, rec in enumerate(ctx.recorders):
                        write_wav(str(artifacts / f"{PLATFORM}-r{rnd}-{name}-{i}.wav"), rec.audio, SR)
                    print("    " + err.replace("\n", "\n    "), flush=True)
                report.append({"round": rnd, "case": name, "ok": err is None, "seconds": round(secs, 1),
                               "error": err})
            print(f"== round {rnd}: {len(chosen) - failures}/{len(chosen)} passed", flush=True)
            failed_any = failed_any or failures > 0
    finally:
        if rig:
            rig.down()
        (artifacts / f"report-{PLATFORM}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 1 if failed_any else 0


if __name__ == "__main__":
    sys.exit(main())
