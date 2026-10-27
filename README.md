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
git clone <this repo> && cd real-time-speech-to-binharic-translator
./install.sh
```

The installer does five things:
- installs `portaudio libpulse tk python-pipx libnotify` with pacman
- installs the `binharic` command into `~/.local/bin` with pipx
- downloads the offline Whisper models (`small.en` and `base.en`, about 600 MB)
- creates the Binharic Mic
- runs `binharic doctor` to check everything

Nothing you say is sent to the cloud.

## Three ways to run it

| Command | What you get |
|---|---|
| `binharic gui` | A desktop window with a big ON/OFF button, send-mode and sound switches, key field, speed and pause sliders, a mic meter, a live transcript, history, and a "type to screech" box |
| `binharic tui` | The same as a full-screen terminal dashboard. Keys: `space` on/off · `l` after-finish ⇄ live · `m` mode · `k` key · `t` type · `p` passthrough · `+/-` speed · `q` quit |
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
| Faster on a weak CPU | `--model base.en` |
| Best accuracy on a strong PC | `--model medium.en` |
| Best accuracy with an NVIDIA GPU | `--model large-v3 --device cuda` |
| Non-English | `--model small` or `--model large-v3` (multilingual) |
| Very light, lower accuracy | `--engine vosk` (needs `binharic download-model --engine vosk`) |

For reference, on a 4-core CPU `small.en` takes about 3 s to transcribe 5 s of speech
and `base.en` takes about 1 s.

## Sound modes

| `--mode` | Sounds like | How |
|---|---|---|
| `binharic` (default) | Mechanicus scrapcode: buzzing, warbling, gliding screeches | 16 tones (one per hex digit) with vibrato, harmonics, buzz and glides between tones, about 0.1 s per letter |
| `hex` | Clean 16-tone R2-D2 beeping | One pure tone per hex digit |
| `binary` | Literal ones and zeros | A low beep for 0 and a high beep for 1, 8 per letter (slowest) |

Every message starts with a rising sync chirp and ends with a checksum, so garbled
audio is dropped rather than decoded wrong. `--speed` changes the tempo, and listeners
must use the same speed.

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
binharic say "the flesh is weak"          # screech typed text
binharic say -m binary "hi" --wav hi.wav  # write a WAV
binharic text "Hi"                        # binary 01001000 01101001 · hex 48 69
binharic text -r "01001000 01101001"      # back to text
binharic mic up | down | status           # manage the Binharic Mic
binharic devices                          # list sinks, sources and devices
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

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[all,test]'
.venv/bin/pytest
```
