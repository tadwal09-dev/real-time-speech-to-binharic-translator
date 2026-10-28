# real-time-speech-to-binharic-translator

You talk. Your voice is **muted**, and what comes out instead is **beeps and screeches**:
real binary, hexadecimal tones, or Adeptus Mechanicus–style binharic scrapcode.
Anyone with `binharic decode` (and your key, if you set one) hears the actual words.
Everyone else hears a servitor having a bad day.

Built for **Arch Linux + PipeWire**. It works as a *layer*: a virtual microphone,
**Binharic Mic**, that you select in Discord, Mumble, OBS, TeamSpeak and so on.

```
 your mic ─► voice detection ─► Whisper (offline) ─► frame + key ─► modem ─► Binharic Mic
             (mutes your voice)  "after I finish" or                (beeps / screech)
                                 "as I talk"
 incoming ─► binharic decode ─► "praise the omnissiah"
```

## Install (Arch)

```sh
git clone -b claude/happy-gates-btt1f9 https://github.com/tadwal09-dev/real-time-speech-to-binharic-translator.git
cd real-time-speech-to-binharic-translator
./install.sh
```

The installer does five things:
- installs `portaudio libpulse tk libnotify uv` with pacman
- builds a private Python 3.12 environment with uv in `~/.local/share/binharic/venv`,
  so it doesn't matter which Python Arch ships, and links `binharic` into `~/.local/bin`
- downloads the offline Whisper models (`small.en` and `base.en`, about 600 MB)
- creates the Binharic Mic
- runs `binharic doctor` to check everything

Nothing you say is sent to the cloud. To update, `git pull` and run `./install.sh`
again. `./install.sh --uninstall` removes it.

## Something not working? `binharic fix`

```sh
binharic fix          # asks you to talk, then fixes what it can
binharic fix --yes    # don't ask, just fix
```

`binharic doctor` only lists problems. `binharic fix` tests everything for real and repairs it:

* starts PipeWire if it's down
* creates the Binharic Mic, and puts your normal speakers and mic back as the defaults if
  PipeWire switched them to the virtual devices
* unmutes your mic and turns it up if it's at a low volume
* **Bluetooth earbuds:** in music (A2DP) mode they have no working mic, so it offers to
  switch them to headset mode
* records from **each microphone while you talk**, picks one that actually hears you, and
  **saves it**, so every command uses it from then on
* plays a test screech into the Binharic Mic and decodes what comes out, the same thing
  Discord would receive
* downloads missing speech models, and the NVIDIA GPU libraries **in resumable pieces**:
  a dropped connection picks up where it stopped instead of starting the 1.5 GB over

## Windows

In PowerShell, from the repo folder:

```powershell
git clone -b claude/happy-gates-btt1f9 https://github.com/tadwal09-dev/real-time-speech-to-binharic-translator.git
cd real-time-speech-to-binharic-translator
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

It installs uv and a private Python, plus Whisper, a `binharic` command and a Start-menu
shortcut, then runs `binharic fix`. For Discord, install the free
**[VB-Audio Virtual Cable](https://vb-audio.com/Cable/)** (as administrator, then reboot).
binharic plays into "CABLE Input"; pick **"CABLE Output"** as your mic in Discord. Everything
else (GUI, TUI, `test`, hotkey commands) works the same as on Linux.

## iPhone / iPad (iSH)

iSH is a Linux terminal app for iOS, but it can't use the phone's microphone or speaker,
and it's far too slow for speech recognition. So the phone does those parts:

* **talking:** tap the keyboard's **mic key** and dictate (or type), then press Enter
* **sound:** each line becomes a WAV file. Play it from the Files app
  (iSH → root → binharic; `latest.wav` is always the newest), or share/AirDrop it
* **decoding:** record a screech with Voice Memos, save it to Files → iSH, then run
  `binharic decode --wav ~/recording.m4a`

```sh
apk add git
git clone -b claude/happy-gates-btt1f9 https://github.com/tadwal09-dev/real-time-speech-to-binharic-translator.git
cd real-time-speech-to-binharic-translator && sh install-ish.sh
binharic
```

## Three ways to run it

| Command | What you get |
|---|---|
| `binharic gui` | A desktop window with a big ON/OFF button, send-mode and sound switches, key field, speed and pause sliders, a mic meter, a live transcript, history, and a "type to screech" box |
| `binharic tui` | The same as a full-screen terminal dashboard. Keys: `space` on/off · `l` after-finish ⇄ live · `x` test mode · `m` mode · `k` key · `t` type · `p` passthrough · `+/-` speed · `q` quit |
| `binharic listen` | A plain, colour-coded log. Good for a background service or a tmux pane |
| `binharic` | Picks for you: the GUI on a desktop, the TUI in a terminal |

All three take the same options, e.g. `binharic gui -k "my cant key" --live --passthrough`.
Run `binharic -h` to see them all.

## How it behaves

* **ON**: your real voice never reaches the Binharic Mic. While you talk, it's muted, and
  the screech is sent instead.
* **OFF**: nothing is sent. With `--passthrough` (or the GUI checkbox), your normal
  voice goes through instead, so you never have to switch mics in Discord.
* **Send mode** (switch it live at any time):
  * **After I finish** (`phrase`, the default): it waits until you pause (*Pause to
    send*, 700 ms by default), then screeches the whole sentence. This is the most
    accurate mode.
  * **As I talk** (`live`, `--live`): words are screeched while you're still speaking,
    as soon as the recogniser is sure of them (two passes in a row must agree). It's
    faster to react but slightly less accurate, because it uses the quicker model for
    the live words.

### Test mode: is it working?

```sh
binharic test            # say something; hear the screech on your speakers
binharic test --air      # ...and check the mic can decode it back (use speakers, not headphones)
binharic test --fast     # same, with the low-latency settings
```

Nothing goes to the Binharic Mic in test mode. For every phrase it shows:

```
▲ binharic The quick brown fox jumps over the lazy dog… (9.5s)
  ⏱ screech started 1384 ms after you stopped talking · recognition 1143 ms (started early) · synth 96 ms
  ✔ round trip OK in 74 ms: The quick brown fox jumps over the lazy dog…
  ✔ over the air OK: The quick brown fox jumps over the lazy dog…
```

You can also switch it on in the GUI (*Test mode* checkbox or Ctrl+T) or in the TUI (`x`).
If binharic is already running (e.g. as the background service), `binharic test` switches
test mode **on in that one and leaves it on** until you run `binharic test` again, so check
you've switched it back before you expect your friends to hear you.

## Discord (and other voice apps)

Pick **Binharic Mic** as your input device. Voice apps "clean up" microphone audio, which
can mangle or even delete a screech, so in Discord's *Voice & Video* settings turn **off**:

* **Noise suppression** (Krisp, or "Standard"): it treats steady tones as noise
* **Echo cancellation** and **Automatic gain control**
* "Automatically determine input sensitivity" can stay on, or use push-to-talk (then hold the
  key while the screech plays)

Mumble, TeamSpeak and OBS have similar noise-suppression options. If your friends'
`binharic decode` misses messages, that's the first thing to check.

### Hotkeys

Bind these in your WM / DE. Each one also shows a desktop notification.

```sh
binharic toggle          # ON/OFF
binharic send toggle     # after I finish ⇄ as I talk   (or: send live / send phrase)
binharic on | off | status
```

Hyprland: `bind = SUPER, B, exec, binharic toggle` and `bind = SUPER SHIFT, B, exec, binharic send toggle`.
i3/sway: `bindsym $mod+b exec binharic toggle`.

To run it in the background on every login (it starts OFF, with passthrough), use
`systemctl --user daemon-reload && systemctl --user enable --now binharic`. Only one
copy can run at a time; the GUI tells you if the service already holds the mic.

## Speech recognition

The default engine is **Whisper** (via faster-whisper), running locally. It's far more
accurate than the old Vosk engine and copes with accents and odd words. It uses two models:

* `small.en` for the final, careful pass on each sentence
* `base.en` for the live words and the running transcript, so it stays real-time on a CPU

| Want | Use |
|---|---|
| Lowest latency | `--fast` (see above) |
| Faster on a weak CPU | `--model base.en` |
| Best accuracy on a strong PC | `--model medium.en` |
| Best accuracy with an NVIDIA GPU | `--model large-v3 --device cuda` |
| Non-English | `--model small` or `--model large-v3` (multilingual: it detects the language; `--language de` fixes it) |
| Very light, lower accuracy | `--engine vosk` (needs `binharic download-model --engine vosk`) |

**NVIDIA GPUs** are used automatically. When `install.sh` sees an NVIDIA driver, it adds
the CUDA 12 libraries Whisper needs (about 1.5 GB, into binharic's own environment, so
your system CUDA is untouched; skip this with `BINHARIC_NO_GPU=1 ./install.sh`). If the
GPU can't be used, binharic says why and runs on the CPU instead. `binharic doctor` shows
the GPU status, and `--device cpu` forces the CPU.

For reference, on a 4-core CPU `small.en` takes about 3 s to transcribe 5 s of speech
and `base.en` takes about 1 s.

### Speed: how long until the screech starts

The screech starts as soon as the recogniser has your sentence. Several things keep that short:

* **Speculative final pass**: transcription starts 200 ms into a pause, while it is still
  waiting to see whether you've finished. If you keep talking, that result is thrown away.
* Whisper uses all your CPU cores but one; the spare one keeps the audio glitch-free.
* The whole screech is generated before it starts playing (50–100 ms) and goes out through
  a 100 ms buffer, so a busy CPU can't make it drop out mid-message (which would garble it).

Measured on a 4-core CPU, from when you stop talking to the first screech sound:

| Settings | Latency | Accuracy |
|---|---|---|
| **default without a working GPU** (`base.en`, greedy, no live transcript; same as `--fast`) | **about 1.4 s** | very good |
| `--accurate` on the CPU (`small.en`) | about 4.0 s | best |
| default with a working NVIDIA GPU (`small.en`) | expected to be a few hundred ms (not measured) | best |
| `--fast --model tiny.en` | about 0.9 s | poor |
| any model with `--device cuda` (NVIDIA GPU) | a few hundred ms | same as the model |

Almost all of that time is Whisper itself, which is already optimized C++
(CTranslate2). The screech encoder and decoder together take under 1% of the time, so
rewriting them in C++ wouldn't make it feel any faster. A GPU, or a faster CPU, would.

## Sound modes

| `--mode` | Sounds like | How |
|---|---|---|
| `binharic` (default) | Mechanicus scrapcode: buzzing, warbling, gliding screeches | 16 tones (one per hex digit) with vibrato, harmonics, buzz and glides between tones, about 0.1 s per letter |
| `hex` | Clean 16-tone R2-D2 beeping | One pure tone per hex digit |
| `binary` | Literal ones and zeros | A low beep for 0 and a high beep for 1, 8 per letter (slowest) |

Every message starts with a rising sync chirp and ends with a checksum, so garbled
audio is dropped rather than decoded wrong. Long text goes out as several messages, each
with its own chirp. The decoder follows small differences in speed between the two
computers' sound cards (it decodes fine with the sender's clock 0.3% fast or slow, about
30 times more than real sound cards differ). `--speed` changes the tempo (0.25–4×), and
listeners must use the same speed.

## Keys ("speaking in cant")

With `-k KEY` (or `BINHARIC_KEY`, or the GUI key field), the text is scrambled before it
becomes sound. Someone decoding without the key gets `[encrypted, N bytes …]`. Someone
with it gets the words. Without a key, anyone running the decoder can read it.

> The key scrambling is a SHA-256 keystream. It keeps casual listeners out. It is not
> audited cryptography.

## The other side: understanding it

```sh
binharic decode -k KEY                    # listen on the mic
binharic decode -k KEY --source speakers  # decode whatever your PC is playing (e.g. Discord)
binharic decode --wav recording.wav
binharic gui --hear speakers              # see incoming messages decoded inside the GUI/TUI
```

## Other commands

```sh
binharic say "the flesh is weak"          # screech typed text (through the running layer, if any)
binharic say -m binary "hi" --wav hi.wav  # write a WAV
binharic text "Hi"                        # binary 01001000 01101001 · hex 48 69
binharic text -r "01001000 01101001"      # back to text
binharic mic up | down | status           # manage the Binharic Mic
binharic devices                          # list inputs/outputs, and which ones binharic uses
binharic doctor                           # check the install
binharic download-model medium.en         # fetch another Whisper model
```

## Notes

* The screech goes to the Binharic Mic automatically once it exists (`--sink auto`).
  Without it, the screech plays on your speakers, and the mic is then ignored while it
  plays so it can't feed back. To hear yourself as well:
  `pactl load-module module-loopback source=binharic_out.monitor`.
* If you speak faster than the screech can play, it queues up and later words are sent
  in bigger batches. `--speed 1.5` helps.
* If PipeWire restarts (an update, suspend/resume), binharic reconnects by itself and
  re-creates the Binharic Mic. Your voice app may need you to re-select it.
* `systemctl --user stop binharic`, closing the terminal, or Ctrl+C all shut it down
  cleanly.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[all,test]'
.venv/bin/pytest
```
