"""Check the Windows test rig's cables carry sound faithfully before any test uses them.

Plays a sweep into each cable and correlates what comes out of the other end
with what went in: a cable that drifts or drops chunks would make every test
that speaks or screeches through it fail for reasons that have nothing to do
with binharic.
"""
import sys
import time

import numpy as np
import sounddevice as sd

SR = 48000


def find(prefix, kind):
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    apis = sd.query_hostapis()
    found = [(apis[d["hostapi"]]["name"] != "MME", i) for i, d in enumerate(sd.query_devices())
             if d["name"].startswith(prefix) and d[key] > 0]
    return sorted(found)[0][1]


def fidelity(play, rec):
    t = np.arange(int(0.6 * SR)) / SR
    sweep = (0.5 * np.sin(2 * np.pi * (300 * t + 0.5 * 5000 * t * t))).astype(np.float32)
    got = []
    stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=rec,
                            callback=lambda d, f, tt, st: got.append(d[:, 0].copy()))
    stream.start()
    time.sleep(0.3)
    pad = np.zeros(SR // 4, np.float32)
    sd.play(np.concatenate([pad, sweep, pad, pad]), samplerate=SR, device=play, blocking=True)
    time.sleep(0.3)
    stream.stop()
    stream.close()
    x = np.concatenate(got).astype(np.float64)
    c = np.correlate(x, sweep.astype(np.float64), "valid")
    k = int(np.argmax(np.abs(c)))
    return abs(float(c[k])) / (np.linalg.norm(sweep) * np.linalg.norm(x[k:k + len(sweep)]) + 1e-9)


ok = True
for name, play, rec in (("VB-Cable (the Binharic Mic)", "CABLE Input", "CABLE Output"),
                        ("Hi-Fi Cable (the test mic)", "Hi-Fi Cable Input", "Hi-Fi Cable Output")):
    score = fidelity(find(play, "output"), find(rec, "input"))
    print(f"{name}: a sweep comes out with correlation {score:.3f}")
    ok = ok and score > 0.9
for d in sd.query_devices():
    if "Cable" in d["name"] and sd.query_hostapis(d["hostapi"])["name"] == "Windows WASAPI":
        print(f"  WASAPI {d['name']}: {d['default_samplerate']:.0f} Hz")
sys.exit(0 if ok else "a cable distorts sound: the e2e results would be meaningless")
