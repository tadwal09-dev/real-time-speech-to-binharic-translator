# Handoff: where we left off

Read this first in a new session, then the README for what the user sees.
Last updated: 2026-10-28 on branch `claude/happy-gates-btt1f9`.

## What this is

`binharic`: an Arch Linux + PipeWire layer that mutes your voice while you talk and
screeches what you said instead, as binary beeps, hex tones or Warhammer 40k-style
binharic. Anyone with `binharic decode` (and the key, if set) gets the words back. Output
goes to a virtual microphone, "Binharic Mic", that you pick in Discord and similar apps.

The user (tadwa, on Arch) asked for, in order:
1. the layer itself
2. a nicer CLI, a GUI and a TUI
3. better speech recognition (Whisper)
4. muting while you talk, with a toggle between sending after you finish and sending as
   you talk
5. a test mode, and as low a latency as possible
6. "test everything and fix bugs"

All of that is built and was tested against a simulated desktop (see below). **It has not
yet worked end to end on the user's real machine.** The latest report, below, is the
current blocker.

## Status after the latest session (everything tested on Windows, Arch, Ubuntu and iSH)

The user asked to "test every function on Windows and Linux, fix everything, and loop until
it passes 3 times in a row on all systems, then ship to main and write a file describing what
everything does". Done in this session:

- **`tests/e2e/`**: an end-to-end suite that drives the real `binharic` command on a real
  sound system: every command, the layer in every mode and privacy level, control commands,
  signals, a crash and restart, a sound-server restart, test mode over the air, both
  engines, and the real GUI and TUI (`gui_driver.py`, `tui_driver.py`). `rig.py` is the
  fake desktop: null sinks in PipeWire/PulseAudio, VB-Audio cables on Windows.
- **CI** (`.github/workflows/ci.yml`):
  - unit tests on Ubuntu and Windows, Python 3.10/3.12/3.14
  - e2e on Windows, installed with `install.ps1`; `.github/scripts/windows-audio.ps1`
    installs VB-Cable + Hi-Fi Cable and allows mic access
  - e2e on Arch Linux with PipeWire, installed with `install.sh`
  - e2e on Ubuntu with PulseAudio
  - e2e on 32-bit Alpine as the iSH stand-in, installed with `install-ish.sh`
  - `workflow_dispatch` with `repeat` runs the suite N times in a row.
- **`ARCHITECTURE.md`**: the map of the code for agents (modules, threads, events, control
  protocol, frame format, platforms, tests, recipes, gotchas).
- Bugs found and fixed (each has a regression test):
  - **live decoding lost whole messages.** `StreamDecoder` trimmed audio it hadn't searched
    yet. This affected `decode` live, `--hear` and `test --air`.
  - **Windows: redirected output crashed** on ✔ ▲ ⚙ (cp1252). `cli.safe_output()` fixes it.
  - **Windows: VB-Audio's Hi-Fi Cable was taken for VB-Cable**, so the screech went to the
    wrong cable.
  - **Windows: test mode heard its own screech.** The output buffers a lot there and the mic
    unmuted too early, so Whisper hallucinated sentences.
  - Windows:
    - ambiguous device names (`decode --input-device`, `say --output-device`)
    - `--hear` needed PipeWire
    - `binharic mic` crashed
    - plain `binharic` opened the TUI instead of the window
    - stopping took 1.5 s longer
    - models went to `~/.local/share` instead of `%LOCALAPPDATA%\binharic`
  - `binharic mic down` failed on PulseAudio (the remap source must go first).
  - iSH: a second file in the same second sorted before the first.
  - A mic that drops audio (input overflow) is now reported.
  - `decode` and `--hear` announced "listening" before any audio flowed, so a message sent
    right then was missed.
  - Windows: test mode's speaker stream was left unfed between screeches, and the start-up
    glitch mangled the next sync chirp. `say` now starts with a short silence for the same
    reason. PortAudio input uses high latency (low dropped input on MME).
  - The pacat playback buffer went from 100 to 250 ms so a busy CPU can't starve a screech.
    It measured no later start. `Output.start_latency` is used for the latency report.
  - GUI: on a 768 px screen the type-to-screech row was cut off (window size, packing order).
  - With VB-Audio's CABLE-A/B installed, the plain CABLE Input is still the one used.
  - After a PipeWire restart, the session manager sometimes linked the reconnected screech
    stream to the default speakers instead of `binharic_out`: the Binharic Mic went silent
    and the screech played out loud. pacat/parec streams now check where they were linked
    for 10 s after (re)connecting and move themselves back (`pulse.stream_device`).
  - Unit tests: the fake speakers took audio instantly, so the output thread "played"
    hours of silence and one test took 5 minutes (and once ran out of memory) on Windows /
    Python 3.10. The fakes now run in real time, like real devices.
- **Result**: three CI runs in a row fully green on the same commit (`6dda0cb`, runs 12–14 of
  `ci.yml`): Windows e2e 30/30 cases, Arch 34/34, Ubuntu 34/34, iSH 9/9, and the unit tests
  on Ubuntu and Windows × Python 3.10/3.12/3.14. That commit was then fast-forwarded to
  `main`.
- `BINHARIC_DUMP_INPUT=folder` saves what every `Input` recorded as WAVs. It's for
  diagnosing a user's mic, and the e2e suite keeps these dumps for failed cases.
- The Windows CI rig's test mic (VB-Audio Hi-Fi Cable) has mismatched rates out of the box
  (48 kHz playback end, 44.1 kHz recording end) and warps everything. `windows-audio.ps1`
  sets the recording end to 48 kHz through IPolicyConfig. `windows-audio-check.py` refuses
  to run the tests if a cable doesn't carry a sweep faithfully (VB-Cable measured 1.000; the
  unfixed Hi-Fi Cable 0.073).
- Known environment limit: PipeWire in a container has no realtime priority and misses
  cycles (xruns) often. The inserted silence corrupts a screech. A desktop gives PipeWire
  realtime priority (rtkit). CI tries to do the same (`pipewire.sh` uses `chrt`, and the
  container gets `SYS_NICE`). Local docker runs without realtime are flaky for this reason
  alone.

## Status after the session before (Windows, iSH, `binharic fix`, faster default)

Added in response to the user's report (a Bluetooth-earbud mic that delivers nothing, a
CUDA download that keeps dropping, and "doctor doesn't work"):

- **`binharic fix`** (`binharic/fix.py`) actually repairs things. It:
  - starts PipeWire
  - creates the Binharic Mic and undoes any defaults PipeWire hijacked
  - unmutes the mic and raises a low volume
  - switches Bluetooth earbuds from A2DP to headset mode (msbc preferred)
  - probes every mic *while the user talks* and **saves the one that hears them** to
    `~/.config/binharic/config.json`, which `_settings` uses as the default source or input device
  - plays a screech into the Binharic Mic and decodes it from the other end
  - downloads the models
  - downloads the CUDA wheels with **HTTP-range resume** and checks their sha256, then
    `uv pip install --no-deps`

  The installers now run `fix` at the end. `doctor` stays as a read-only check and points
  to `fix`.
- **Privacy options** (latest): `plain` / `encrypted` / `decoys` via `--privacy`, GUI Privacy row, TUI `c`, `binharic privacy X`. `Settings.privacy`; `Translator.send_key`/`send_decoys`. Privacy is captured at queue time in the item meta (`_queue_text`), and `_take_pending` only merges items with the same key and decoy count (regression test in `tests/test_privacy.py`). Cipher rewritten in `codec.py`: scrypt KDF (cached), HMAC-SHA256 keystream, 6-byte nonce plus 6-byte encrypt-then-MAC tag; this breaks old encrypted frames. Verified on real PulseAudio: an eavesdropper reads only the plain message, a wrong key the same, the right key all three.
- **Decoy chirps** (`--decoys N`, GUI checkbox under Key, TUI `c`, control `decoys N`): `codec.build_decoy` + `modem.frames_with_decoys` hide each encrypted frame among look-alike gibberish frames; receivers holding a key drop frames that fail the key check.
- **Ctrl+C bug fixed:** the pacat/parec children now run with `start_new_session=True`.
- **Faster by default:** without a usable GPU, `listen`, `test`, `gui` and `tui` use the
  `--fast` setup automatically (`base.en` only, greedy). `--accurate` restores `small.en`.
  With the GPU working (via `fix`), the default is `small.en`.
- **Windows** (`install.ps1`, `binharic/pa.py`):
  - PortAudio devices
  - VB-Audio Virtual Cable as the virtual mic: we play into "CABLE Input", Discord records
    from "CABLE Output"
  - the control socket is TCP on localhost with a port file (Windows Python has no AF_UNIX)
  - no fcntl, no SIGHUP
  - `windows-curses` dependency; CUDA DLLs via `os.add_dll_directory`

  **None of this has been run on real Windows**; it's tested on Linux with fakes in
  `tests/test_platforms.py`.
- **iSH** (`install-ish.sh`, `binharic/ish.py`):
  - detected through `uname`/`/proc/ish`, or the `BINHARIC_ISH=1` override
  - text in (iOS keyboard dictation), WAV files out to `~/binharic`
  - Voice Memos m4a decoded through ffmpeg
  - live modes refuse with an explanation
  - installs with Alpine's py3-numpy and `pip --no-deps`

  **Not yet run in a real iSH**; tested by forcing iSH mode on Linux.

### Still open / next steps
1. The user needs to `git pull`, run `./install.sh` (or just `binharic fix`), and paste the
   output. The key unknown: does the CMF Buds Pro 2 mic work in headset mode? If not,
   `fix` recommends the built-in or a wired mic.
2. Confirm that the resumable CUDA download finishes on their connection, and measure GPU latency.
3. Windows passes the e2e suite on a GitHub runner (Windows Server, VB-Cable), but a real
   PC with real hardware and Discord, and a real iSH on an iPhone, are still untested.

## The user's environment

- Arch Linux, PipeWire, user `tadwa`, repo cloned at `~/real-time-speech-to-binharic-translator`
- The system Python is 3.14, so the installer uses uv to build a Python 3.12 environment
  at `~/.local/share/binharic/venv`, linked to `~/.local/bin/binharic`
- Arch's `python-pipx` is broken on their machine (missing `filelock`), so we don't use pipx
- They have an NVIDIA GPU and a working driver, but no CUDA 12 cuBLAS/cuDNN yet (point 3 above)
- Their default audio device is the Bluetooth earbuds "CMF Buds Pro 2"
- They paste terminal output back; give exact commands to run

## Layout

| File | What it does |
|---|---|
| `binharic/cli.py` | Commands: `gui tui listen test`, `toggle on off status send`, `say decode text`, `mic devices doctor download-model`. Validates arguments, handles SIGTERM and SIGHUP, uses `os._exit` after the UIs |
| `binharic/core.py` | `Translator`: the threads (mic, speech recognition, output, air decoder, incoming decoder, control socket at `$XDG_RUNTIME_DIR/binharic.sock`), muting, passthrough, test mode, timing measurements |
| `binharic/stt.py` | Whisper engine (`small.en` final pass plus `base.en` live, speculative final pass, warm-up, GPU-to-CPU fallback, hallucination filter) and Vosk engine. `Transcriber` does the phrase/live logic (agreement, holding back the last word, `remaining_words` alignment) |
| `binharic/vad.py` | Energy VAD with minimum-statistics noise floor, pause/resume events, `since_voice`; `Decimator` (48 kHz to 16 kHz) |
| `binharic/modem.py` | Chirp sync plus FSK. Modes: binary (2 tones), hex and binharic (16 tones; binharic adds warble, buzz and glides). Exact timing grid, an early/late gate that tracks clock drift, and `StreamDecoder` that handles candidates without blocking |
| `binharic/codec.py` | Frame format (magic/mode/flags, nonce, MAC tag, length, CRC-16), scrypt plus HMAC cipher, decoys, `split_text` (240-byte chunks) |
| `binharic/audio.py` | pacat/parec streams (errors at start, reconnect, `ensure_alive`), with PortAudio as the fallback; WAV input/output |
| `binharic/pulse.py` | `pactl` helpers: create/remove the virtual mic (and restore the defaults), `real_source`/`real_sink` (never our own virtual devices), `check_device`, `probe_source` |
| `binharic/fix.py` | `binharic fix`: the real repair tool (see status above), including resumable wheel downloads |
| `binharic/osinfo.py`, `pa.py`, `ish.py` | Platform detection and saved config; PortAudio and VB-Cable helpers (Windows); iSH mode |
| `binharic/gpu.py` | Loads the CUDA 12 libraries from the pip wheels without `LD_LIBRARY_PATH` |
| `binharic/ui_cli.py`, `ui_tui.py`, `ui_gui.py` | The log, curses and Tk front ends |
| `install.sh`, `contrib/` | Installer (pacman, uv, models, mic, doctor, `--uninstall`), user systemd unit, desktop entry |
| `tests/` | Unit tests (`pytest -q tests`); `tests/e2e/` is the end-to-end suite (`python -m tests.e2e.run`), see ARCHITECTURE.md; `test_privacy.py` covers the cipher and the three options; `test_platforms.py` covers Windows/iSH/fix with fakes. `test_fixes.py` holds the regressions from the full test pass |

## How it was tested (rebuild this; the scripts were in a session scratch folder)

- Unit tests: `python -m pytest -q tests`. All pass. Without rich installed, one test is
  skipped.
- Simulated desktop in a container, with a real sound server:
  ```sh
  export XDG_RUNTIME_DIR=/tmp/bh   # keep it short: AF_UNIX path limit
  pulseaudio -D --exit-idle-time=-1 -n --load=module-native-protocol-unix \
    --load="module-null-sink sink_name=room" \
    --load="module-remap-source master=room.monitor source_name=alsa_input.usb-Fake_Mic" \
    --load="module-null-sink sink_name=alsa_output.fake_speakers"
  # a real mic never stops: keep quiet noise (rms ~0.003) playing into "room" with pacat
  # "say" into the mic:      paplay --device=room speech.wav   (espeak-ng -w, resampled to 48k)
  # record what Discord hears: parec --device=binharic_mic --raw --format=s16le --rate=48000 --channels=1 --latency-msec=20
  # speaker->mic loop for --air: pactl load-module module-loopback source=alsa_output.fake_speakers.monitor sink=room
  # PipeWire's "defaults switch to our virtual devices" trap: pactl set-default-source binharic_mic; pactl set-default-sink binharic_out
  ```
  Decode the recordings with `modem.decode_audio`.
- GUI: `Xvfb :99` with `xdotool` clicks and `import -window root` screenshots. Tk exists
  only for Python 3.12 in that container (`python3.12 -m venv`). Dialogs need
  `xdotool windowraise`, because there's no window manager.
- TUI: `tmux new-session -d … ; tmux send-keys …; tmux capture-pane -p`.
- Watch out: `pkill -f <pattern>` and `pgrep -f` matched the running shell's own command
  line several times and killed it. Use saved PIDs, or `ps -eo pid=,comm=,args=` filtered
  on `comm`.
- Also: background jobs started from a non-interactive shell ignore SIGINT, so test
  Ctrl+C in tmux and use SIGTERM elsewhere.

## Known limits and untested areas

- Not yet confirmed on real hardware: the mic, the earbuds, the NVIDIA GPU, and Discord.
  Discord's noise suppression (Krisp), echo cancellation and automatic gain control may
  mangle the tones. The README tells users to turn them off.
- CPU latency, measured in the container (4 cores): `--fast` about 1.4 s from the end of
  speech to the first screech, default about 4 s. A GPU should be much faster but is
  unmeasured.
- Encryption: scrypt plus HMAC-SHA256, with a 48-bit nonce and tag (shortened for airtime). Sound, but not audited.

## Conventions

- Develop and push on `claude/happy-gates-btt1f9`. Don't open a PR unless asked.
- `main` holds the shipped code. Ship to it (fast-forward) only after CI passes three times in a
  row on the commit being shipped (`workflow_dispatch` reruns, `repeat` input for more rounds).
- Read `ARCHITECTURE.md` before changing code, and update it when you change how something works.
- Commit trailers: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and a
  `Claude-Session:` line. Put no model names in code or docs.
- Keep the user-facing README in sync, and add a regression test for every bug fixed.
