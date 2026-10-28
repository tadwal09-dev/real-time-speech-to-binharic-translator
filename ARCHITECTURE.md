# How binharic works (for agents and contributors)

Read this before changing code. It describes what every part does, how the
parts talk to each other, the rules that keep it working, and how to add a
feature without breaking a platform. `README.md` is the user's view;
`HANDOFF.md` is the running project log (what the user asked for, what's
open). This file is the map of the code.

## 1. What it is, in one paragraph

`binharic` sits between your microphone and apps like Discord. While it's
ON it mutes your voice, recognises what you say (Whisper, offline), and
plays it instead as a screech: binary beeps, 16-tone hex, or warbling
"binharic". The screech goes into a virtual microphone ("Binharic Mic" on
Linux/PipeWire, VB-Audio Virtual Cable on Windows) that the other app
records from. Anyone running `binharic decode` turns the screech back into
text; with a key, only key holders can. iSH on iOS gets a text-to-WAV
version, because iSH can't reach the phone's audio.

## 2. Data flow

```
mic ─► Input ─► _mic_loop ─► _mic_q ─► _stt_loop: Decimator 48k→16k ─► Transcriber
                                                 (EnergyVAD → Engine: Whisper/Vosk)
                                                         │ on_send(text, final)
                                                         ▼
                         _queue_text: fixes privacy (key, decoys) in the item's meta
                                                         │ _pending
                                                         ▼
_out_loop ─► _take_pending ─► _play_text: codec.split_text → modem.frames_with_decoys
            (joins items with   (codec.build_frame / build_decoy) → modem.lead_in +
             the same privacy)   modulate_body → Output.write ─► virtual mic ─► Discord

the other side:  Input(virtual mic / speakers monitor / WAV) ─► modem.StreamDecoder
                 or modem.decode_audio ─► codec.parse_frame ─► Message (text or locked)
```

Muting: the mic is never passed to the output while ON. Only the screech
is written. While OFF, the output is silent, or your voice with
`--passthrough`.

## 3. Repository layout

| Path | What |
|---|---|
| `binharic/cli.py` | Every command (`argparse`), argument checks, front-end picking, signal handling, output-encoding safety |
| `binharic/core.py` | `Settings`, `Translator` (the running layer: threads, muting, queue, privacy, test mode, control socket), `send_command` |
| `binharic/stt.py` | Speech engines (`WhisperEngine`, `VoskEngine`), `Transcriber` (phrase/live logic), model download, `data_dir` |
| `binharic/vad.py` | `EnergyVAD` (adaptive speech detector), `Decimator` (48 kHz → 16 kHz) |
| `binharic/modem.py` | Sound modes, sync chirp, modulation, `decode_audio`, `StreamDecoder` (live decoding) |
| `binharic/codec.py` | Frame format, encryption (scrypt + HMAC-SHA256), decoys, text chunking, text ↔ binary/hex |
| `binharic/audio.py` | `Input`/`Output`: pacat/parec processes (PipeWire/Pulse) or PortAudio (`sounddevice`); WAV read/write |
| `binharic/pulse.py` | `pactl` helpers: Binharic Mic up/down, real mic/speakers (never our own virtual devices), device checks |
| `binharic/pa.py` | PortAudio helpers: device lists, VB-Cable detection, name → index resolution, mic probing |
| `binharic/fix.py` | `binharic fix`: repairs (PipeWire, virtual mic, Bluetooth headset mode, mic choice, models, CUDA wheels) |
| `binharic/osinfo.py` | Platform detection (Windows, iSH), runtime/config dirs, saved config |
| `binharic/gpu.py` | Loads CUDA 12 cuBLAS/cuDNN from pip wheels for Whisper |
| `binharic/ish.py` | iSH mode: text (dictation) → WAV files in `~/binharic`, m4a input via ffmpeg |
| `binharic/ui_cli.py` | `listen`: the plain log (`Printer` turns events into lines) |
| `binharic/ui_tui.py` | `tui`: curses dashboard (`Dashboard`) |
| `binharic/ui_gui.py` | `gui`: Tk window (`run`) |
| `install.sh`, `install.ps1`, `install-ish.sh` | Installers: Arch, Windows, iSH |
| `contrib/` | systemd user unit, desktop entry |
| `tests/test_*.py` | Unit tests (fakes, no sound devices): `pytest -q tests` |
| `tests/e2e/` | End-to-end tests on a real sound system, every command (section 10) |
| `.github/workflows/ci.yml` | CI: unit tests on Linux and Windows, and the e2e suite on Windows, Arch, Ubuntu and iSH |
| `.github/scripts/` | Runner setup: Windows virtual audio, PipeWire without systemd, Arch and Windows install-and-test |

### Commands (`cli.build_parser`; each runs a `cmd_*` function)

| Command | What it does | Where |
|---|---|---|
| *(none)* | Picks a front end: the GUI on a desktop (always on Windows), the TUI in a terminal, the plain log otherwise; `ish` on iSH | `cmd_pick` |
| `gui` / `tui` / `listen` | Start the layer with a front end (`ui_gui`, `ui_tui`, `ui_cli`). The layer options (`_run_options`) work before or after the command | `_run_ui` |
| `test [--air]` | The layer in test mode: the screech plays on your speakers and is decoded back and timed (`--air`: also from what the mic hears) | `cmd_test` |
| `toggle` / `on` / `off` / `status` | Control a running layer through the control socket | `cmd_ctl` → `_control` |
| `privacy LEVEL`, `send WHEN` | Change a running layer's privacy, or when it screeches | `_control` |
| `say TEXT` | Screech typed text: into the running layer if there is one, else straight into the Binharic Mic (or the speakers), or `--wav FILE` | `cmd_say` |
| `decode` | Turn binharic back into text, from the mic, a source/device, or a WAV | `cmd_decode` |
| `text` | Show text as binary/hex, or `--reverse` to read it back | `cmd_text` |
| `mic up/down/status` | Create or remove the Binharic Mic (Linux); explains VB-Cable on Windows | `cmd_mic` |
| `devices` | List sinks, sources and PortAudio devices | `cmd_devices` |
| `doctor` / `fix` | Check the setup / repair it (`fix.py`) | `cmd_doctor`, `cmd_fix` |
| `ish` | iSH: dictate or type, get WAV files (`ish.py`) | `cmd_ish` |
| `download-model` | Fetch a Whisper or Vosk model | `cmd_download` |

## 4. The layer: `core.Translator`

### Threads (all started by `start()`, all daemon threads via `_spawn`)

| Thread | Job | If it dies |
|---|---|---|
| `mic` | `Input.read()` into `_mic_q`; replaces audio with zeros while screeching in half duplex, and feeds `_air_q` in `test --air` | fatal: the layer stops and `fatal` says why |
| `stt` | loads the engine, then VAD + recognition; emits `level`, `speech`, `partial`; calls `_on_send` | fatal |
| `out` | owns the output: plays queued screeches, passthrough voice, or silence (PortAudio needs a steady stream) | fatal |
| `air` | test mode: decodes what the mic heard from the speakers | logged |
| `hear` | `--hear`: decodes incoming binharic from another source or device | logged |
| `guard` | PipeWire, screeching into the Binharic Mic: every 2 s, moves apps that picked the Binharic Mic but were linked elsewhere back onto it (`pulse.misrouted_mic_streams`) | logged |
| `control` | the control socket (below) | logged |

`stop()` is idempotent. It sets `_stop` and `_abort`, joins the threads
(1.5 s budget) **before** closing devices (closing a PortAudio stream
mid-read can crash), then removes the socket file. Signal handlers only
call `request_stop()`.

### Events (`subscribe(fn)`; `fn(kind, data)`; a crashing listener is printed, never fatal)

| kind | data |
|---|---|
| `log` | `text`, `level` (`info`/`warn`/`error`) |
| `error` | `text` (a thread failed) |
| `status` | everything from `status()`: settings (`key` as a bool), `ready`, `engine_name`, `speaking`, `screeching`, `full_duplex`, `queued` |
| `level` | `level` (mic RMS, about 20 per second) |
| `speech` | `speaking` |
| `partial` | `text` (interim transcript) |
| `queued` | `text`, `final` |
| `sent` | `text`, `hex`, `binary`, `seconds`, `mode`, `synth_ms`, `test`, `decoys`, `privacy`; plus `latency_ms`, `stt_ms`, `speculative` for the first chunk of spoken text |
| `verify` | test mode: `via` (`digital`/`air`), `sent`, `decoded`, `ok`, `decode_ms` |
| `received` | `--hear`: `text`, `locked`, `mode` |

The three front ends are just event printers plus controls; anything a UI
can do goes through `Translator` methods (`toggle`, `set_send_mode`,
`set_mode`, `set_privacy`, `set_key`, `set_decoys`, `set_test`,
`set_passthrough`, `set_speed`, `set_end_ms`, `say`).

### Rules that keep it correct

* **Privacy is fixed when you speak, not when it plays.** `_queue_text`
  stores `key`, `decoys` and `privacy` in the item's meta, `_play_text`
  uses only those, and `_take_pending` joins only items with the same key
  and decoys. Switching to plain must never send earlier words in the
  open (there is a regression test for this).
* `send_key` is `None` in plain privacy even when a key is set. Encryption
  without a key is refused (`set_privacy` returns False), never silently
  sent plain. Clearing the key falls back to plain with a warning.
* Half duplex (the default unless the screech goes to a virtual device):
  the mic is ignored while screeching and until the end has actually
  played (the output's `latency`, which is large on Windows) plus 0.4 s,
  so the layer never transcribes its own screech. Test mode is always
  half duplex.
* A whole chunk is synthesised before any of it plays (a gap mid-screech
  corrupts it). Writes are 100 ms, and `_abort` (OFF) cuts a screech
  between writes.
* OFF clears the queue. Typed text (`say`) is `forced`: it plays even while OFF.

### Control socket (`binharic toggle`, hotkeys, `say` into a running layer)

Linux: a unix socket at `$XDG_RUNTIME_DIR/binharic.sock` (mode 0600).
Windows: TCP on 127.0.0.1, with the port in `%LOCALAPPDATA%\binharic\binharic.port`.
The protocol is one line in and one line out. Commands: `ping`, `status`,
`toggle`, `on`, `off`, `send [toggle|live|phrase]`, `mode NAME`,
`passthrough [on|off]`, `privacy plain|encrypted|decoys`, `decoys N`,
`test [on|off]`, `say TEXT`, `quit`. The reply is the status JSON, or
`error: …`. A stale socket from a crash is detected (ping fails) and
replaced; a live one makes the second instance refuse to start. The
accept loop wakes every 0.25 s, because shutting the socket down doesn't
interrupt `accept()` on Windows.

### Devices: `_resolve_devices`

* **PipeWire/Pulse available** (`pulse.available()`): everything goes
  through pacat/parec (PortAudio over ALSA is unreliable on PipeWire).
  The mic is `pulse.real_source()` and the speakers `pulse.real_sink()`;
  both skip our own virtual devices even when PipeWire made them the
  defaults. `--sink auto` means `binharic_out` when the Binharic Mic
  exists (`cli` resolves it).
* **Otherwise** (Windows, or Linux without a sound server):
  `_resolve_portaudio` resolves names to indices with `pa.resolve`,
  uses VB-Cable (`pa.find_cable`) as the virtual output, and turns
  `--hear` into an input device.

## 5. Sound: `modem.py`

* A transmission is `lead_in` (a 0.15 s rising chirp from 500 to 3500 Hz,
  plus a gap), then one tone per symbol on an exact time grid (`_grid`),
  then a short tail.
* Modes (`MODES`): `binary` (id 0; 2 tones, 1 bit per symbol), `hex`
  (id 1; 16 tones, 4 bits per symbol) and `binharic` (id 2; hex tones that
  glide, warble and buzz; the decoder reads only the steady part of each
  symbol). `--speed` scales the symbol time, and both sides must use the
  same speed.
* `find_preambles` uses FFT normalised cross-correlation against the chirp.
* `_Demod` reads symbols with an early/late gate that follows clock drift
  between sound cards.
* `decode_audio(sig, sr, mode, key, speed)` decodes a whole recording.
  `try_decode_at` raises `NeedMore` (not all here yet) or `BadFrame`.
* `StreamDecoder.feed(block)` decodes live audio. It keeps candidate chirp
  positions, and a cut-off frame never blocks the next one. **It must
  never trim audio it hasn't searched yet**: that bug lost whole messages
  depending on timing (regression test
  `test_live_decoder_never_drops_unsearched_audio`). `flush()` decodes
  what's left when input ends.
* `frames_with_decoys` hides the real frame at a random place among N
  decoys of random length (system RNG).

## 6. Frames and encryption: `codec.py`

```
flags 1 (0xB0 | mode_id<<1 | encrypted) · [nonce 6 · tag 6] · length 2 · payload · CRC-16 2
```

* The key comes from scrypt(N=2^14, r=8, p=1, a fixed salt) and is split
  into an encryption key and a MAC key. `_keys` is `lru_cache`d, so it's
  about 0.1 s once per passphrase.
* The ciphertext is the payload XOR an HMAC-SHA256(enc_key, nonce || counter)
  stream. The tag is HMAC-SHA256(mac_key, flags + nonce + length +
  ciphertext)[:6], checked with `hmac.compare_digest`.
* A wrong key or a tampered frame comes back as `Message(locked=True)`.
  The UI shows `[encrypted, N bytes …]`, never garbage.
* `build_decoy` makes a frame with a valid structure and random contents.
  The tag check fails for everyone, so key holders drop it.
* `split_text` cuts text into frames of 240 UTF-8 bytes at most, at
  spaces (each frame re-syncs).
* Changing the frame format or the KDF breaks compatibility with copies
  already in use. If you have to, bump `KDF_SALT` (`binharic-v2 kdf`) or
  the magic number, and say so in the README.

## 7. Speech: `stt.py` and `vad.py`

* `EnergyVAD`: minimum-statistics noise floor; emits start, audio, pause,
  resume and end events; end comes after `end_ms` of quiet.
* `Transcriber`: phrase mode sends one final text per utterance (with a
  speculative final pass at `pause`). Live mode commits words once two
  hypotheses agree (`agreed_prefix`) and keeps the last word back.
  `remaining_words` aligns the final pass with the words already sent.
* `WhisperEngine`: a final model (`small.en` by default, `base.en` with
  `--fast`), an optional live model, a warm-up pass, GPU → CPU fallback,
  and a hallucination filter (`_HALLUCINATIONS`).
* `VoskEngine`: lighter and less accurate.
* `cli._settings` picks the fast setup automatically when there's no
  usable GPU.
* Models live in `stt.data_dir()`: `~/.local/share/binharic`
  (`XDG_DATA_HOME`) on Linux, `%LOCALAPPDATA%\binharic` on Windows.

## 8. Audio I/O: `audio.py`, `pulse.py`, `pa.py`

* `Input`/`Output` with a `source`/`sink` name start `parec`/`pacat` in
  their own session. Ctrl+C in the terminal must not kill them; we close
  them ourselves. They are checked at start (a wrong name fails within
  0.4 s, with pacat's message), and they reconnect for up to 30 s after a
  sound-server restart. `before_reconnect` rebuilds the Binharic Mic first.
* For 10 s after a pacat/parec stream (re)connects, `_PulseStream._watch`
  checks which device the sound server linked it to (`pulse.stream_device`,
  found by the process id) and moves it back if it's wrong. PipeWire's
  session manager, when it is starting at the same moment (a restart, a
  login), can link a stream to the default device instead of the one named.
* Other apps' streams go astray too. An app that picked the Binharic Mic (its
  `target.object`) can still be linked to your real mic: when a link fails to activate,
  WirePlumber falls back to the default source and never retries. The `guard` thread and
  `binharic fix` move such streams back with `pactl move-source-output`. If a move doesn't
  stick (links someone made by hand), the guard gives up after 3 tries and says what to do.
  PulseAudio has no `target.object`, so there it does nothing. (When the requested device
  doesn't exist yet, WirePlumber relinks by itself as soon as it appears.)
* `Input` on PortAudio uses `latency="high"`: with "low", Windows' MME dropped
  input whenever a read came late. It also counts overflows (samples lost when
  the computer is busy) and warns once a minute through `notify`.
* `BINHARIC_DUMP_INPUT=folder` makes every `Input` write what it recorded as a
  WAV when it closes. Use it to see exactly what binharic heard; the e2e suite
  keeps these dumps for failed cases.
* `Output` on pacat has a 250 ms buffer (`PACAT_LATENCY_MS`). One gap in the
  middle of a screech loses the whole message, and a deep buffer costs no start
  delay (an idle stream fills instantly). `latency` is how long written audio
  keeps playing (the mute window uses it); `start_latency` is how soon a new
  screech is heard (the latency report uses it).
* PortAudio streams must be fed continuously: an unfed stream glitches at the
  start of the next write, and here that would hit the sync chirp. `_out_loop`
  feeds the main output and test mode's speaker stream with silence. A one-off
  `say` starts with 0.2 s of silence.
* `Output.ensure_alive()` is polled by the `out` thread so an idle pacat
  notices a restart.
* With no name, `Input`/`Output` use PortAudio (`sounddevice`).
  `Output.needs_feeding` is True there: PortAudio wants continuous writes,
  pacat doesn't.
* `pulse.mic_up()` loads `module-null-sink binharic_out` and
  `module-remap-source binharic_mic`, and restores the previous defaults
  when PipeWire hijacked them.
* `pulse.mic_down()` unloads the remap source **first** (unloading the
  sink first takes the source with it, and then the second unload fails).
* `pa.py`: Windows lists every device once per host API (MME, DirectSound,
  WASAPI, WDM-KS), and MME cuts names to 31 characters. `pa.resolve` tries
  an exact match, then a prefix in either direction (for MME's short
  names), then a substring, MME first.
* `find_cable` / `find_cable_output` match VB-Cable itself
  (`_VB_CABLE`: `^CABLE(-[A-D])? (Input|Output)`), never VB-Audio's Hi-Fi
  Cable or Voicemeeter. The plain CABLE wins over the paid CABLE-A/B
  (that's the one Discord is told to record from), MME first.
  `real_inputs` excludes every virtual cable.

## 9. Platforms

| | Linux (Arch + PipeWire) | Windows | iSH (iOS) |
|---|---|---|---|
| Install | `install.sh`: pacman, a uv venv on Python 3.12 at `~/.local/share/binharic/venv`, linked into `/usr/local/bin` (on every PATH, keybinds included) and `~/.local/bin` | `install.ps1`: uv, a venv in `%LOCALAPPDATA%\binharic\venv`, `bin\binharic.cmd` on the PATH, a Start-menu shortcut | `install-ish.sh`: apk python and numpy, venv `--system-site-packages`, `pip --no-deps` |
| Virtual mic | `binharic_out` → `binharic_mic` (pactl) | VB-Audio Virtual Cable (play into CABLE Input; Discord records CABLE Output) | none: WAV files |
| Control | unix socket | TCP on localhost plus a port file | (same as Linux; rarely used) |
| Stop | SIGTERM / SIGHUP / Ctrl+C | Ctrl+C, Ctrl+Break / closing the console (SIGBREAK) | Ctrl+D or an empty line |
| Config | `~/.config/binharic/config.json` | `%APPDATA%\binharic\config.json` | `~/.config/binharic` |
| Live modes | yes | yes | refused (`_no_live_audio_on_ish`) |

`osinfo.ISH` is detected from `uname` / `/proc/ish`, or with
`BINHARIC_ISH=1`. `cli.safe_output()` makes stdout/stderr UTF-8 when
they're redirected (Windows writes cp1252 otherwise and crashed on ✔ ▲ ⚙).
Plain `binharic` picks the GUI on a desktop (always on Windows), the TUI
in a terminal, or the plain log otherwise, and `binharic ish` on iSH.

## 10. Tests

### Unit tests: `python -m pytest -q tests`

No sound devices needed: fakes for audio, engines, pactl (a shell script,
so those tests skip on Windows) and sounddevice. `tests/conftest.py` gives
each test its own runtime dir (control socket). Every fixed bug gets a
regression test here; the test's comment names the bug. `FakeIn` and
`FakeOut` (`tests/test_pipeline.py`) deliver and take audio in real time,
as real devices do. A fake that takes audio instantly lets the output
thread write minutes of silence per second, and the test then crawls.

### End-to-end: `python -m tests.e2e.run [-k NAME] [--repeat N] [--list]`

The suite runs the real `binharic` command (or `BINHARIC_E2E_CMD`, e.g.
an installed copy) against a real sound system.

* `tests/e2e/rig.py` builds a fake desktop:
  * **Linux**: null sinks inside the running PipeWire/PulseAudio. A
    "room" sink feeds the fake mic `alsa_input.e2e_mic` (with faint noise,
    like a real mic), there are fake speakers, and the virtual mic's far
    end is recorded.
  * **Windows**: VB-Audio cables. "Hi-Fi Cable" is the mic; "CABLE" is the
    Binharic Mic, as on a real PC.
* Speech comes from `tests/e2e/fixtures/*.wav` (espeak-ng; the phrases are
  in `PHRASES`).
* A case is a function decorated with `@case(platforms)`. It uses `ctx.bh`
  / `ctx.ok` / `ctx.fails` for commands, `ctx.layer(...)` for a running
  layer (a context manager that checks for a clean exit and no
  tracebacks), `say_and_record` to speak and capture what Discord would
  get, and `decode`/`texts` to read it.
* Recordings from a failed case are saved as WAVs in the artifacts folder.
* `gui_driver.py` runs the real window in-process and clicks every
  control. `tui_driver.py` runs `binharic tui` in a pseudo-terminal
  (ConPTY on Windows) and presses every key. Both play speech and check
  the virtual mic.
* CI (`ci.yml`):
  * unit tests on Ubuntu and Windows × Python 3.10 / 3.12 / 3.14
  * e2e on Windows (installs via `install.ps1`, after
    `.github/scripts/windows-audio.ps1` installs the cables and allows
    mic access)
  * e2e on Arch Linux with PipeWire (a container, a normal user,
    `install.sh`, `.github/scripts/pipewire.sh` instead of systemd)
  * e2e on Ubuntu with PulseAudio (pip install)
  * e2e on iSH, using a 32-bit Alpine container as a stand-in
  * `workflow_dispatch` with `repeat=N` runs the suite N times in a row.

**Adding a case**: write `@case(AUDIO)` (or `ALL`, `{"linux"}`,
`{"windows"}`, `{"ish"}`), use only the rig's methods for audio (so the
same case runs on every platform), check observable results (what the
virtual mic carries, the command's output, its exit code), and run
`--repeat 3` locally before relying on it.

## 11. Recipes

**A new command**: add a `cmd_x(args)` in `cli.py` and a subparser in
`build_parser()`. Errors are `sys.exit("one-line message")` or a
`RuntimeError`/`OSError` (`main` prints those without a traceback). Use
`say_line(markup, plain)` for output and `esc()` for anything
user-supplied inside markup. Then add an e2e case, a `--help` line, and
the README.

**A new layer setting**:
1. Add a field to `Settings` (and `public()` if it's secret).
2. Add a `Translator.set_x` that ends with `self._changed()`.
3. Add a control command in `handle` and, if needed, a CLI subcommand.
4. Add it to all three UIs (GUI row, TUI key plus header, `ui_cli.describe`).
5. Add it to `_run_options` and `_settings`.
6. Add unit and e2e tests.

**A new sound mode**: add a `ModeSpec` in `modem.MODES` with an unused id
(0–7: three bits of the flags byte). The decoder tries all modes in
`auto`. Round-trip it at several speeds in the unit tests, and add it to
the e2e mode cases.

**Anything privacy-related**: keep the decision at queue time (section 4),
never send plain when the user chose encryption, and test with a key, the
wrong key and no key.

**Before pushing**: `ruff check --select F,E9 binharic tests`,
`python -m pytest -q tests`, and the e2e cases you touched with `--repeat 3`.
Push to the working branch; CI runs everything on all four platforms.

## 12. Gotchas (each of these was a real bug)

* `pkill -f`/`pgrep -f` with a pattern also matches the shell running it.
  Use saved PIDs, or filter `ps -eo pid=,comm=,args=` on `comm`.
* Background jobs started from a non-interactive shell ignore SIGINT: test
  Ctrl+C in tmux or a pty.
* Unloading `binharic_out` before `binharic_mic` fails on PulseAudio.
* PipeWire can make a new virtual device the default. Code must never
  treat our own devices as "your mic" or "your speakers".
* Windows: an ambiguous device name makes sounddevice fail (the same
  device under four APIs). Always go through `pa.resolve`.
* Windows: VB-Audio's other products also contain "cable". Match VB-Cable
  exactly.
* Windows: redirected output is cp1252, so `safe_output()` must run first
  in `main`.
* Windows: microphone privacy ("let desktop apps access your microphone")
  blocks recording with "Unanticipated host error". `fix` tells the user.
* A recorder or decoder that connects late misses the sync chirp: a
  message is only decodable from its start.
* Files the iSH Files app shows must sort by name in creation order.
* Tk's packer gives space in packing order. The GUI packs its bottom rows
  (type to screech, hints) before the expanding log, so a short window shrinks
  the log rather than hiding them. The window height fits the screen (768 px
  laptops). The GUI driver checks that every control is visible.
* The Windows CI rig: VB-Audio's Hi-Fi Cable is bit-perfect (it never
  resamples), and on the runner its two ends start at different rates. This
  warps everything sent through it. `windows-audio.ps1` aligns the formats, and
  `windows-audio-check.py` verifies that both cables carry a sweep faithfully
  before any test runs. Never trust e2e results from an unverified rig.
* PipeWire without realtime priority (containers, CI) misses cycles and
  plays silence into the middle of a screech, which then can't be decoded.
  Desktops give PipeWire realtime priority through rtkit. Don't "fix" e2e
  flakiness that comes from this in the modem; fix the environment
  (`pipewire.sh` tries `chrt`). Test recorders use 100 ms latency so they
  don't force tiny cycles.
* After a PipeWire restart the reconnected screech was linked to the default
  speakers, not `binharic_out`: Discord got silence and the screech played out
  loud. Never assume a stream is where you asked; `_PulseStream._watch` checks.
* Discord (in Firefox) had picked the Binharic Mic, but PipeWire linked it to the user's
  Bluetooth earbuds: a link failed while they switched to headset mode. The friend got the
  earbuds' mic and never the screech. The `guard` thread moves such streams back.
* A test helper process that isn't cleaned up keeps running for hours:
  every `Layer` registers itself with `ctx`, which kills it.
