"""`binharic fix`: find what's wrong and fix it, instead of just listing it.

Every step checks something for real (records from the mic, plays a screech
through the virtual mic and decodes it) and repairs what it can. What works
is saved to the config, so every other command uses it from then on.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

from . import modem, osinfo

SPEAK_SECONDS = 3.0
HEARD = 0.01     # a peak this loud while you talk = the mic hears you
ALIVE = 3e-4     # anything above digital silence = the mic delivers audio


class Fixer:
    def __init__(self, yes: bool = False, gpu: bool = True, out=print):
        self.yes = yes
        self.gpu = gpu
        self.say = out
        self.problems: list[str] = []
        self.fixed: list[str] = []

    # -- output helpers ----------------------------------------------------
    def step(self, title: str):
        self.say(f"\n== {title}")

    def ok(self, text: str):
        self.say(f"   ok    {text}")

    def did(self, text: str):
        self.fixed.append(text)
        self.say(f"   FIXED {text}")

    def bad(self, text: str, how: str = ""):
        self.problems.append(text + (f" -> {how}" if how else ""))
        self.say(f"   !!    {text}" + (f"\n         {how}" if how else ""))

    def ask(self, question: str) -> bool:
        if self.yes:
            return True
        if not sys.stdin.isatty():
            return False
        try:
            return input(f"   ?     {question} [Y/n] ").strip().lower() in ("", "y", "yes")
        except EOFError:
            return False

    def countdown(self, what: str):
        self.say(f"   >>    {what}: keep talking for {SPEAK_SECONDS:.0f} seconds (count out loud: one, two, three…)")
        if not self.yes and sys.stdin.isatty():
            try:
                input("         press Enter, then start talking ")
            except EOFError:
                pass

    # -- the run -----------------------------------------------------------
    def run(self) -> int:
        if osinfo.ISH:
            self.ish()
        elif osinfo.WINDOWS or not self._pipewire_tools():
            self.portaudio()
        else:
            self.pipewire()
        self.models()
        if self.gpu:
            self.cuda()
        self.say("\n== summary")
        for f in self.fixed:
            self.say(f"   fixed: {f}")
        for p in self.problems:
            self.say(f"   still wrong: {p}")
        if not self.problems:
            self.say("   everything works. Next: " + ("binharic   (type or dictate a line)" if osinfo.ISH
                                                     else "binharic test"))
        return 1 if self.problems else 0

    # -- Linux / PipeWire ----------------------------------------------------
    def _pipewire_tools(self) -> bool:
        return bool(shutil.which("pactl") and shutil.which("parec") and shutil.which("pacat"))

    def pipewire(self):
        from . import pulse

        self.step("sound server (PipeWire)")
        if not pulse.available():
            self.say("   ..    not answering; starting it")
            subprocess.run(["systemctl", "--user", "start", "pipewire", "pipewire-pulse", "wireplumber"],
                           capture_output=True)
            for _ in range(20):
                if pulse.available():
                    break
                time.sleep(0.5)
        if not pulse.available():
            self.bad("PipeWire isn't running", "sudo pacman -S pipewire pipewire-pulse wireplumber, then log out and in")
            return
        self.ok("PipeWire is running")

        self.step("Binharic Mic (the virtual microphone)")
        before = (pulse._default("sink"), pulse._default("source"))
        if not pulse.virtual_mic_exists():
            pulse.mic_up()
            self.did("created the Binharic Mic")
        else:
            self.ok("Binharic Mic exists")
        self._unhijack_defaults(before)

        self.step("speakers")
        sink = pulse.real_sink()
        if sink:
            self.ok(f"{pulse.describe(sink, 'sinks')} ({sink})")
        else:
            self.bad("no speakers/headphones found", "plug some in, or check pavucontrol")

        self.step("microphone")
        self._find_mic()

        self.step("virtual mic end to end")
        self._loop_check()
        self._misrouted_apps()

    def _misrouted_apps(self):
        """Discord picked the Binharic Mic, but PipeWire linked it to the real mic."""
        from . import pulse
        stray = pulse.misrouted_mic_streams()
        for index, _, _ in stray:
            try:
                pulse.move_stream(index, pulse.MIC, playback=False)
            except Exception:
                pass  # gone meanwhile, or it didn't move: checked below
        if not stray:
            return
        time.sleep(1.0)  # the session manager relinks it
        still = {s[0] for s in pulse.misrouted_mic_streams()}
        for index, app, where in stray:
            if index in still:
                self.bad(f"{app} picked the Binharic Mic but records from {where or 'nothing'}, and won't move back",
                         f"in {app}, pick another microphone, then the Binharic Mic again")
            else:
                self.did(f"{app} picked the Binharic Mic but was recording from {where or 'nothing'} - moved it back")

    def _unhijack_defaults(self, before):
        from . import pulse
        sink, source = pulse._default("sink"), pulse._default("source")
        if sink == pulse.SINK:
            real = before[0] if before[0] and before[0] != pulse.SINK else pulse.real_sink()
            if real:
                pulse._pactl("set-default-sink", real)
                self.did(f"your default output was the virtual sink - set it back to {pulse.describe(real, 'sinks')}")
        if source in (pulse.MIC, f"{pulse.SINK}.monitor"):
            real = pulse.real_source()
            if real:
                pulse._pactl("set-default-source", real)
                self.did(f"your default input was the Binharic Mic - set it back to {pulse.describe(real, 'sources')}")

    def _cards(self) -> list[dict]:
        from . import pulse
        try:
            return json.loads(pulse._pactl("-f", "json", "list", "cards"))
        except Exception:
            return []

    def _bluetooth_headset_profiles(self) -> list[tuple[str, str, str]]:
        """(card, current profile, best headset profile) for Bluetooth cards
        not in a mode where their microphone works."""
        out = []
        for card in self._cards():
            name = card.get("name", "")
            if not name.startswith("bluez_card"):
                continue
            profiles = card.get("profiles", {}) or {}
            active = card.get("active_profile", "")
            headset = [p for p, info in profiles.items()
                       if ("headset" in p or "hfp" in p or "hsp" in p)
                       and (not isinstance(info, dict) or info.get("available", True) is not False)]
            if headset and active not in headset:
                headset.sort(key=lambda p: ("msbc" not in p, "head-unit" not in p, p))  # wideband first
                out.append((name, active, headset[0]))
        return out

    def _mic_candidates(self) -> list[str]:
        from . import pulse
        ours = lambda n: n == pulse.MIC or n.endswith(".monitor") or n.startswith(pulse.SINK)  # noqa: E731
        names = [d["name"] for d in pulse._info("sources") if not ours(d["name"])]
        default = pulse.real_source()
        names.sort(key=lambda n: (n != default, n.startswith("bluez")))  # default first
        return names

    def _unmute(self, source: str):
        from . import pulse
        try:
            muted = "yes" in pulse._pactl("get-source-mute", source).lower()
            vol = pulse._pactl("get-source-volume", source)
        except Exception:
            return
        if muted:
            pulse._pactl("set-source-mute", source, "0")
            self.did(f"unmuted {pulse.describe(source, 'sources')}")
        pct = [int(p.rstrip("%")) for p in vol.split() if p.endswith("%")]
        if pct and max(pct) < 50:
            pulse._pactl("set-source-volume", source, "100%")
            self.did(f"turned {pulse.describe(source, 'sources')} up from {max(pct)}% to 100%")

    def _find_mic(self):
        from . import pulse

        # Bluetooth earbuds have no working mic in A2DP (music) mode: offer headset mode
        for card, active, headset in self._bluetooth_headset_profiles():
            self.say(f"   ..    Bluetooth device {card} is in '{active}' mode, where its mic doesn't work")
            if self.ask("switch it to headset mode so its mic works? (sound quality drops while it's in that mode)"):
                try:
                    pulse._pactl("set-card-profile", card, headset)
                    time.sleep(2)  # the headset link takes a moment
                    self.did(f"switched {card} to {headset}")
                except Exception as e:
                    self.bad(f"couldn't switch {card} to {headset}: {e}")

        candidates = self._mic_candidates()
        if not candidates:
            self.bad("no microphone found", "plug one in, or connect your earbuds and run `binharic fix` again")
            return
        for c in candidates:
            self._unmute(c)
        self.countdown("testing your microphones")
        results = []
        for c in candidates:
            label = pulse.describe(c, "sources")
            peak = pulse.probe_source(c, SPEAK_SECONDS)
            results.append((peak or 0.0, c))
            state = ("can't record from it" if peak is None else
                     f"level {peak:.3f} - hears you" if peak >= HEARD else
                     f"level {peak:.3f} - too quiet" if peak >= ALIVE else "level 0 - silent")
            self.say(f"   ..    {label}: {state}")
            if peak is not None and peak >= HEARD:
                break  # good enough: don't make you talk through every device
            if len(candidates) > 1 and c != candidates[-1]:
                self.say("         (keep talking, trying the next one)")
        best_peak, best = max(results)
        if best_peak >= HEARD:
            osinfo.save_config(source=best)
            self.did(f"binharic will use {pulse.describe(best, 'sources')} ({best}) - saved")
        elif best_peak >= ALIVE:
            osinfo.save_config(source=best)
            self.bad(f"{pulse.describe(best, 'sources')} works but is very quiet",
                     "turn its volume up in pavucontrol (Input Devices), or speak closer")
        else:
            bt = any(c.startswith("bluez") for c in candidates)
            self.bad("no microphone delivers any sound",
                     "Bluetooth earbuds often have no usable mic on Linux; try your laptop's built-in mic "
                     "or a wired headset, then run `binharic fix` again" if bt else
                     "check the mic's mute switch, and pavucontrol -> Input Devices")

    def _loop_check(self):
        """Play a screech into the Binharic Mic and decode it from the other end:
        what Discord would receive."""
        from . import pulse
        from .audio import Input, Output
        sr = 48000
        text = "binharic self test"
        try:
            rec = Input(sr, source=pulse.MIC, blocksize=sr // 10)
            out = Output(sr, sink=pulse.SINK)
        except RuntimeError as e:
            self.bad(f"can't open the virtual mic: {e}", "run `binharic mic down && binharic mic up`")
            return
        got: list[np.ndarray] = []
        done = [False]

        import threading

        def record():
            while not done[0]:
                try:
                    got.append(rec.read())
                except EOFError:
                    return
        t = threading.Thread(target=record, daemon=True)
        t.start()
        time.sleep(0.3)
        out.write(modem.encode_text(text, "binharic", sr=sr))
        out.write(np.zeros(sr // 2, dtype=np.float32))
        time.sleep(0.8)
        done[0] = True
        out.close()
        rec.close()
        heard = [m.text for m in modem.decode_audio(np.concatenate(got) if got else np.zeros(1), sr)]
        if text in heard:
            self.ok("a test screech went into the Binharic Mic and decoded perfectly - Discord will get it")
        else:
            self.bad("the test screech didn't come out of the Binharic Mic",
                     "run `binharic mic down && binharic mic up`, then `binharic fix` again")

    # -- Windows / PortAudio ---------------------------------------------------
    def portaudio(self):
        from . import pa
        self.step("audio devices (PortAudio)")
        if not pa.available():
            self.bad("PortAudio isn't working", "reinstall: the Windows installer brings it with sounddevice"
                     if osinfo.WINDOWS else "sudo pacman -S portaudio")
            return
        self.ok("PortAudio works")

        self.step("virtual mic (VB-Audio Virtual Cable)")
        if pa.find_cable() is not None:
            self.ok("VB-Audio Virtual Cable is installed: pick 'CABLE Output' as your mic in Discord")
            self._cable_loop_check()
        else:
            self.bad("VB-Audio Virtual Cable isn't installed (needed to send the screech to Discord)",
                     f"download it from {pa.CABLE_URL}, run its setup as administrator, reboot, run `binharic fix`")
            if osinfo.WINDOWS and self.ask("open the download page now?"):
                os.startfile(pa.CABLE_URL)  # noqa: S606 (Windows only)

        self.step("microphone")
        inputs = pa.real_inputs()
        if not inputs:
            self.bad("no microphone found", "plug one in, and check Settings -> Privacy -> Microphone")
            return
        seen, uniq = set(), []
        for d in inputs:  # Windows lists each mic once per audio API: test each name once
            if d["name"] not in seen:
                seen.add(d["name"])
                uniq.append(d)
        self.countdown("testing your microphones")
        best = (0.0, None)
        for d in uniq:
            peak = pa.probe_input(d["index"], SPEAK_SECONDS)
            self.say(f"   ..    {d['name']}: " + ("can't record from it" if peak is None else f"level {peak:.3f}"))
            if peak is not None and peak > best[0]:
                best = (peak, d)
            if peak is not None and peak >= HEARD:
                break
        peak, d = best
        if d is not None and peak >= ALIVE:
            osinfo.save_config(input_device=d["name"])
            self.did(f"binharic will use '{d['name']}' - saved")
            if peak < HEARD:
                self.bad("that mic is very quiet", "raise its level in Sound settings -> Input")
        else:
            self.bad("no microphone delivers any sound",
                     "Windows: Settings -> Privacy & security -> Microphone -> allow desktop apps; "
                     "check the mic isn't muted")

    def _cable_loop_check(self):
        from . import pa
        import sounddevice as sd
        sr = 48000
        cable_in, cable_out = pa.find_cable(), pa.find_cable_output()
        if cable_out is None:
            return
        text = "binharic self test"
        sig = np.concatenate([np.zeros(sr // 4), modem.encode_text(text, "binharic", sr=sr), np.zeros(sr // 2)])
        try:
            rec = sd.playrec(sig.reshape(-1, 1), samplerate=sr, channels=1, dtype="float32",
                             device=(cable_out, cable_in))
            sd.wait()
        except Exception as e:
            self.bad(f"couldn't play/record through the cable: {e}")
            return
        if text in [m.text for m in modem.decode_audio(rec[:, 0], sr)]:
            self.ok("a test screech went through the cable and decoded perfectly - Discord will get it")
        else:
            self.bad("the test screech didn't come through the cable",
                     "in Sound settings make sure CABLE Input/Output are enabled and not muted")

    # -- iSH (iOS) -----------------------------------------------------------
    def ish(self):
        from .ish import outbox
        self.step("iSH (iOS)")
        self.say("   ..    iSH can't reach the iPhone's microphone or speaker, so binharic works with text and\n"
                 "         sound files there: dictate or type, and it writes WAV files you play from the Files app.")
        try:
            path = outbox()
            (path / ".probe").write_text("x")
            (path / ".probe").unlink()
            self.ok(f"output folder works: {path} (in the Files app: iSH -> root -> binharic)")
        except OSError as e:
            self.bad(f"can't write the output folder: {e}")
        if shutil.which("ffmpeg"):
            self.ok("ffmpeg is installed: you can decode Voice Memos (m4a) too")
        else:
            self.bad("ffmpeg is missing (only needed to decode Voice Memos recordings)", "apk add ffmpeg")
        a = modem.encode_text("ish self test", "binharic", sr=16000)
        if [m.text for m in modem.decode_audio(a, 16000)] == ["ish self test"]:
            self.ok("encoding and decoding work")
        else:
            self.bad("the modem self test failed")

    # -- speech models -----------------------------------------------------------
    def models(self):
        if osinfo.ISH:
            return  # no speech recognition in iSH: iOS dictation does that part
        from . import stt
        self.step("speech recognition")
        if not stt.have_whisper():
            self.bad("Whisper isn't installed", "rerun the installer")
            return
        try:
            from faster_whisper.utils import download_model
        except ImportError:
            return
        for name in (stt.DEFAULT_WHISPER, stt.DEFAULT_WHISPER_LIVE):
            try:
                download_model(name, cache_dir=str(stt.data_dir() / "whisper"), local_files_only=True)
                self.ok(f"model {name} is downloaded")
            except Exception:
                self.say(f"   ..    downloading model {name}")
                try:
                    download_model(name, cache_dir=str(stt.data_dir() / "whisper"))
                    self.did(f"downloaded model {name}")
                except Exception as e:
                    self.bad(f"couldn't download {name}: {e}", "check the network, run `binharic fix` again")

    # -- NVIDIA GPU -----------------------------------------------------------
    def cuda(self):
        if osinfo.ISH:
            return
        from . import gpu
        nvidia = gpu.has_nvidia_driver() or (osinfo.WINDOWS and shutil.which("nvidia-smi"))
        if not nvidia:
            return
        self.step("NVIDIA GPU (makes recognition several times faster)")
        if gpu.cuda_devices() == 0:
            self.bad("the NVIDIA driver is there but Whisper can't see the GPU",
                     "update the driver (sudo pacman -Syu) and reboot")
            return
        if gpu.libs_available():
            self.ok("CUDA 12 libraries are installed: Whisper runs on the GPU")
            return
        self.say("   ..    the CUDA 12 libraries are missing; downloading them in resumable pieces\n"
                 "         (about 1.5 GB; a dropped connection just picks up where it stopped)")
        try:
            files = [download_wheel(pkg, spec, self.say) for pkg, spec in CUDA_WHEELS]
            install_wheels(files)
        except Exception as e:
            self.bad(f"couldn't install the CUDA libraries: {e}",
                     "run `binharic fix` again - downloads resume - or use the CPU (it still works)")
            return
        gpu._loaded = False
        if gpu.libs_available():
            self.did("installed the CUDA 12 libraries: Whisper now runs on the GPU")
        else:
            self.bad("the CUDA libraries installed but don't load", "send the output of `binharic doctor`")


# -- resumable wheel downloads (pip/uv restart a dropped 700 MB file from zero) --

CUDA_WHEELS = [("nvidia-cublas-cu12", "12."), ("nvidia-cudnn-cu12", "9.")]


def wheel_dir() -> Path:
    from .stt import data_dir
    d = data_dir() / "wheels"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pick_wheel(pkg: str, prefix: str) -> dict:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=30) as r:
        info = json.load(r)
    plat = "win_amd64" if osinfo.WINDOWS else "manylinux"
    arch = "" if osinfo.WINDOWS else "x86_64"

    def key(v):
        return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))
    for version in sorted((v for v in info["releases"] if v.startswith(prefix)), key=key, reverse=True):
        for f in info["releases"][version]:
            name = f["filename"]
            if name.endswith(".whl") and plat in name and arch in name and not f.get("yanked"):
                return f
    raise RuntimeError(f"no {pkg} {prefix}x wheel for this system on PyPI")


def download_wheel(pkg: str, prefix: str, say=print, attempts: int = 200) -> Path:
    f = _pick_wheel(pkg, prefix)
    dest = wheel_dir() / f["filename"]
    part = dest.with_suffix(".part")
    size, want = f["size"], f["digests"]["sha256"]
    if dest.exists() and dest.stat().st_size == size:
        return dest
    last = time.monotonic()
    for attempt in range(attempts):
        have = part.stat().st_size if part.exists() else 0
        if have >= size:
            break
        req = urllib.request.Request(f["url"], headers={"Range": f"bytes={have}-"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab") as fh:
                if have and r.status != 206:  # server ignored the range: start over
                    fh.truncate(0)
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
                    if time.monotonic() - last > 3:
                        last = time.monotonic()
                        say(f"         {f['filename'][:40]}… {part.stat().st_size * 100 // size}%")
        except (OSError, urllib.error.URLError) as e:
            say(f"         connection dropped ({e.__class__.__name__}), resuming…")
            time.sleep(min(30, 2 + attempt))
    if not part.exists() or part.stat().st_size != size:
        raise RuntimeError(f"download of {pkg} didn't finish")
    h = hashlib.sha256()
    with open(part, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != want:
        part.unlink()
        raise RuntimeError(f"{pkg} download was corrupted; deleted it, run again")
    part.rename(dest)
    return dest


def install_wheels(files: list[Path]) -> None:
    uv = shutil.which("uv")
    cmd = ([uv, "pip", "install", "--python", sys.executable, "--no-deps", *map(str, files)] if uv else
           [sys.executable, "-m", "pip", "install", "--no-deps", *map(str, files)])
    subprocess.run(cmd, check=True)


def run(yes: bool = False, gpu: bool = True) -> int:
    return Fixer(yes=yes, gpu=gpu).run()
