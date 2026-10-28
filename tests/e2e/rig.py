"""A fake desktop for the end-to-end tests: a "microphone" the tests can talk
into, "speakers", and recorders on the far end of the virtual mic.

Linux (PulseAudio or PipeWire): null sinks inside the running sound server.
  e2e room sink  --monitor-->  remap source "alsa_input.e2e_mic"   (the mic)
  "alsa_output.e2e_speakers"                                        (speakers)
  binharic_out --monitor--> binharic_mic                            (what Discord records)

Windows: VB-Audio's cables (see .github/scripts/windows-audio.ps1).
  "Hi-Fi Cable Input" --> "Hi-Fi Cable Output"   (the mic)
  "CABLE Input"       --> "CABLE Output"         (the Binharic Mic, as on a real PC)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

import numpy as np

from binharic.audio import read_wav

SR = 48000
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def speech(name: str) -> np.ndarray:
    """A fixture phrase at 48 kHz."""
    x, sr = read_wav(os.path.join(FIXTURES, f"{name}.wav"))
    t = np.arange(int(len(x) * SR / sr)) / SR
    return np.interp(t, np.arange(len(x)) / sr, x).astype(np.float32) * 0.8


class Recording:
    """Collects blocks from a recorder thread; ``audio`` is everything so far."""

    def __init__(self):
        self.blocks: list[np.ndarray] = []
        self.started = time.monotonic()

    def wait_flowing(self, timeout: float = 10.0) -> None:
        """Return once audio really arrives: a recorder that connects late would
        miss the sync chirp at the start of a screech."""
        end = time.monotonic() + timeout
        while not self.blocks and time.monotonic() < end:
            time.sleep(0.02)
        if not self.blocks:
            raise RuntimeError("the recorder never received any audio")
        time.sleep(0.2)

    @property
    def audio(self) -> np.ndarray:
        return np.concatenate(self.blocks) if self.blocks else np.zeros(1, np.float32)


# -- Linux ---------------------------------------------------------------------------

class LinuxRig:
    name = "linux"
    ROOM, MIC, SPEAKERS = "e2e_room", "alsa_input.e2e_mic", "alsa_output.e2e_speakers"

    def __init__(self):
        self._noise = None
        self._modules: list[str] = []

    @staticmethod
    def pactl(*args: str) -> str:
        return subprocess.run(["pactl", *args], check=True, capture_output=True, text=True).stdout

    def server(self) -> str:
        info = self.pactl("info")
        return "PipeWire" if "PipeWire" in info else "PulseAudio"

    def attach(self) -> None:
        pass

    def up(self) -> None:
        try:
            self.pactl("info")
        except (subprocess.CalledProcessError, FileNotFoundError):
            if not shutil.which("pulseaudio"):
                raise RuntimeError("no sound server: start PipeWire (or PulseAudio) first") from None
            self._start_pulseaudio()
            for _ in range(50):
                try:
                    self.pactl("info")
                    break
                except subprocess.CalledProcessError:
                    time.sleep(0.1)
        sinks = self.pactl("list", "short", "sinks")
        if self.ROOM not in sinks:
            self._modules.append(self.pactl("load-module", "module-null-sink", f"sink_name={self.ROOM}",
                                            "sink_properties=device.description=E2E_Room").strip())
            self._modules.append(self.pactl("load-module", "module-null-sink", f"sink_name={self.SPEAKERS}",
                                            "sink_properties=device.description=E2E_Speakers").strip())
            time.sleep(0.3)
            self._modules.append(self.pactl("load-module", "module-remap-source", f"master={self.ROOM}.monitor",
                                            f"source_name={self.MIC}",
                                            "source_properties=device.description=E2E_Mic").strip())
        for _ in range(50):
            if self.MIC in self.pactl("list", "short", "sources"):
                break
            time.sleep(0.1)
        self.reset()
        self._start_noise()

    def _start_noise(self) -> None:
        # a real mic never delivers pure digital silence: keep faint room noise playing
        self._noise = subprocess.Popen(
            ["pacat", "--playback", f"--device={self.ROOM}", "--raw", "--format=s16le", f"--rate={SR}",
             "--channels=1", "--latency-msec=50", "--client-name=e2e-noise"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def noise():
            rng = np.random.default_rng(0)
            try:
                while True:
                    self._noise.stdin.write((rng.standard_normal(2400) * 0.003 * 32767).astype("<i2").tobytes())
            except (BrokenPipeError, OSError, ValueError):
                pass
        threading.Thread(target=noise, daemon=True).start()

    @staticmethod
    def _start_pulseaudio() -> None:
        # no hardware here: just the protocol module; the rig adds its own devices
        subprocess.run(["pulseaudio", "-D", "-n", "--exit-idle-time=-1", "--disallow-exit",
                        "--load=module-native-protocol-unix", "--load=module-always-sink"], check=True)

    def set_defaults(self) -> None:
        self.pactl("set-default-sink", self.SPEAKERS)
        self.pactl("set-default-source", self.MIC)

    def reset(self) -> None:
        """Between cases: our defaults, and the Binharic Mic set up (as after install)."""
        from binharic import pulse
        if not pulse.virtual_mic_exists():
            pulse.mic_up()
        self.set_defaults()

    def restart_server(self) -> None:
        """Restart the sound server, as an update or suspend/resume does. Everything
        loaded into it is gone afterwards; the layer must rebuild the Binharic Mic."""
        if self._noise:
            self._noise.kill()
        cmd = os.environ.get("BINHARIC_E2E_RESTART")  # PipeWire: how this machine restarts it
        if cmd:
            subprocess.run(cmd, shell=True, check=True)
        else:
            subprocess.run(["pulseaudio", "-k"], check=False)
            time.sleep(1.5)
            self._start_pulseaudio()
        for _ in range(100):
            try:
                self.pactl("info")
                break
            except subprocess.CalledProcessError:
                time.sleep(0.1)
        self._modules = []
        # our fake hardware comes back with the server (a real mic and speakers would)
        self._modules.append(self.pactl("load-module", "module-null-sink", f"sink_name={self.ROOM}",
                                        "sink_properties=device.description=E2E_Room").strip())
        self._modules.append(self.pactl("load-module", "module-null-sink", f"sink_name={self.SPEAKERS}",
                                        "sink_properties=device.description=E2E_Speakers").strip())
        time.sleep(0.3)
        self._modules.append(self.pactl("load-module", "module-remap-source", f"master={self.ROOM}.monitor",
                                        f"source_name={self.MIC}",
                                        "source_properties=device.description=E2E_Mic").strip())
        self.set_defaults()
        self._start_noise()

    def down(self) -> None:
        if self._noise:
            self._noise.kill()
        for m in reversed(self._modules):
            try:
                self.pactl("unload-module", m)
            except subprocess.CalledProcessError:
                pass

    def play(self, audio: np.ndarray, where: str = "mic") -> None:
        """Play into the mic ("mic"), the speakers ("speakers") or the virtual mic ("vmic")."""
        sink = {"mic": self.ROOM, "speakers": self.SPEAKERS, "vmic": "binharic_out"}[where]
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        subprocess.run(["pacat", "--playback", f"--device={sink}", "--raw", "--format=s16le", f"--rate={SR}",
                        "--channels=1", "--client-name=e2e-play"], input=pcm, check=True)

    def record(self, where: str = "vmic") -> "LinuxRecorder":
        source = {"vmic": "binharic_mic", "speakers": f"{self.SPEAKERS}.monitor", "mic": self.MIC}[where]
        return LinuxRecorder(source)

    # binharic options that make a layer use this rig
    def layer_args(self) -> list[str]:
        return []  # the rig's mic and speakers are the defaults, and the Binharic Mic is set up

    def decode_args(self, where: str = "vmic") -> list[str]:
        return ["--source", {"vmic": "binharic_mic", "speakers": "speakers"}[where]]

    def hear_args(self) -> list[str]:
        return ["--hear", "speakers"]


class LinuxRecorder(Recording):
    def __init__(self, source: str):
        super().__init__()
        self.proc = subprocess.Popen(["parec", f"--device={source}", "--raw", "--format=s16le", f"--rate={SR}",
                                      "--channels=1", "--latency-msec=20", "--client-name=e2e-rec"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._run, daemon=True).start()
        self.wait_flowing()

    def _run(self):
        while True:
            raw = self.proc.stdout.read(4800)
            if not raw:
                return
            self.blocks.append(np.frombuffer(raw[: len(raw) // 2 * 2], "<i2").astype(np.float32) / 32768)

    def stop(self) -> np.ndarray:
        time.sleep(0.5)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        return self.audio


# -- Windows ---------------------------------------------------------------------------

class WindowsRig:
    name = "windows"

    def __init__(self):
        import sounddevice as sd
        self.sd = sd
        self._noise = None

    def _find(self, prefix: str, kind: str) -> int:
        key = "max_input_channels" if kind == "input" else "max_output_channels"
        apis = self.sd.query_hostapis()
        found = [(apis[d["hostapi"]]["name"], i) for i, d in enumerate(self.sd.query_devices())
                 if d["name"].startswith(prefix) and d[key] > 0]
        if not found:
            raise RuntimeError(f"no {kind} device '{prefix}…': run .github/scripts/windows-audio.ps1")
        found.sort(key=lambda f: f[0] != "MME")  # MME: shared mode, many clients at once
        return found[0][1]

    def attach(self) -> None:
        """Find the devices (another process already set the rig up)."""
        self.mic_in = self._find("Hi-Fi Cable Input", "output")     # we "talk" into this...
        self.mic_out = self._find("Hi-Fi Cable Output", "input")    # ...and binharic hears it here
        self.vmic_in = self._find("CABLE Input", "output")          # binharic screeches into this
        self.vmic_out = self._find("CABLE Output", "input")         # and Discord would record this
        self.mic_name = self.sd.query_devices(self.mic_out)["name"]
        self.vmic_name = self.sd.query_devices(self.vmic_out)["name"]

    def up(self) -> None:
        self.attach()
        self._noise = self.sd.OutputStream(samplerate=SR, channels=1, dtype="float32", device=self.mic_in,
                                           callback=self._noise_cb)
        self._rng = np.random.default_rng(0)
        self._noise.start()

    def _noise_cb(self, out, frames, t, status):
        out[:, 0] = self._rng.standard_normal(frames).astype(np.float32) * 0.003

    def server(self) -> str:
        return "Windows audio (VB-Audio cables)"

    def set_defaults(self) -> None:
        pass

    def reset(self) -> None:
        pass

    def down(self) -> None:
        if self._noise:
            self._noise.stop()
            self._noise.close()

    def play(self, audio: np.ndarray, where: str = "mic") -> None:
        dev = {"mic": self.mic_in, "vmic": self.vmic_in, "speakers": self.mic_in}[where]
        self.sd.play(np.asarray(audio, np.float32).reshape(-1, 1), samplerate=SR, device=dev, blocking=True)

    def record(self, where: str = "vmic") -> "WindowsRecorder":
        return WindowsRecorder(self.sd, {"vmic": self.vmic_out, "mic": self.mic_out, "speakers": self.mic_out}[where])

    def layer_args(self) -> list[str]:
        return ["--input-device", self.mic_name]

    def decode_args(self, where: str = "vmic") -> list[str]:
        return ["--input-device", self.vmic_name if where == "vmic" else self.mic_name]

    def hear_args(self) -> list[str]:
        # no "what you hear" loopback through PortAudio: incoming screeches arrive on a device
        return ["--hear", self.mic_name]


class WindowsRecorder(Recording):
    def __init__(self, sd, device: int):
        super().__init__()
        self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=device,
                                     callback=lambda d, f, t, s: self.blocks.append(d[:, 0].copy()))
        self.stream.start()
        self.wait_flowing()

    def stop(self) -> np.ndarray:
        time.sleep(0.5)
        self.stream.stop()
        self.stream.close()
        return self.audio


def make_rig():
    return WindowsRig() if sys.platform == "win32" else LinuxRig()
