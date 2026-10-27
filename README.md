# real-time-speech-to-binharic-translator

You speak, it listens, and what comes out is **beeps and screeches** instead of your
voice: real binary, hexadecimal tones, or Adeptus Mechanicus–style binharic scrapcode.
Anyone with `binharic decode` (and your key, if you set one) hears the actual words.
Everyone else hears a servitor having a bad day.

Built for **Arch Linux + PipeWire**. It works as a *layer*: it shows up as a virtual
microphone ("Binharic Mic") you can select in Discord, Mumble, OBS, TeamSpeak, etc.

```
 your mic ──► Vosk (offline speech-to-text) ──► frame + optional key ──► modem ──► Binharic Mic
                                                                               (beeps / screech)

 their speakers / a recording ──► binharic decode ──► "praise the omnissiah"
```

## Install (Arch)

```sh
git clone <this repo> && cd real-time-speech-to-binharic-translator
./install.sh
```

That installs `portaudio libpulse python-pipx libnotify` with pacman, installs the
`binharic` command into `~/.local/bin` with pipx, and downloads the offline English
speech model (~40 MB, Vosk). Nothing is sent to the cloud.

Manual alternative: `pipx install '.[speech]' && binharic download-model`.

## Use it

```sh
~/.local/share/binharic/virtual-mic.sh up       # creates the "Binharic Mic" device
binharic listen --sink binharic_out --passthrough -k "my cant key"
```

Then pick **Binharic Mic** as the input device in your voice app.

* **ON**: your voice is cut off. Each phrase you finish is transcribed and screeched out.
* **OFF**: with `--passthrough` your normal voice goes through the same virtual mic,
  so you never have to change devices. Without it, OFF is silent.
* `binharic toggle` flips ON/OFF on the running layer and pops up a desktop
  notification. Bind it to a hotkey (Hyprland: `bind = SUPER, B, exec, binharic toggle`;
  i3/sway: `bindsym $mod+b exec binharic toggle`; KDE/GNOME: custom shortcut).
  `binharic off` forces it OFF.
* Want to hear your own screeching? `pactl load-module module-loopback source=binharic_out.monitor`

To run it in the background on every login (starts OFF, with passthrough):

```sh
systemctl --user daemon-reload && systemctl --user enable --now binharic
```

### Modes

| `--mode`   | What it sounds like | How |
|------------|---------------------|-----|
| `binharic` (default) | Mechanicus scrapcode: buzzing, warbling, gliding screeches | 16 tones (one per hex digit) with vibrato, harmonics, buzz and glides between tones |
| `hex`      | Clean 16-tone R2-D2 beeping | One pure tone per hex digit |
| `binary`   | Literal ones and zeros | Two tones, low = 0 and high = 1, 8 beeps per letter |

Every transmission starts with a rising sync chirp so the decoder can lock on, and ends
with a checksum, so garbled audio is thrown away instead of being decoded wrong.
`--speed 1.5` makes it faster. The listener must use the same `--speed`.

### Keys ("speaking in cant")

With `-k KEY` (or the `BINHARIC_KEY` env var), the text is scrambled before it is turned
into sound. Anyone who decodes it **without** the key gets
`[encrypted, N bytes: ...]`. Only people with the same key get the words. Without a key,
anyone running the decoder can read it. That's still obscure, but it isn't private.

> The key scrambling is a SHA-256 keystream. It keeps people out of casual
> conversation. It is not audited cryptography, so don't stake anything serious on it.

## Understanding it (the other side)

```sh
binharic decode -k "my cant key"                       # listen on the default mic
binharic decode -k "my cant key" --source <sink>.monitor  # decode what your PC is playing (e.g. Discord)
binharic decode --wav recording.wav                    # decode a recording
```

`pactl list short sources` lists the monitor sources. The decoder detects the mode by
itself. Over the air (speaker to mic) it works best in a quiet room at a decent volume.
If it misses messages, lower `--threshold`, for example to `0.35`.

## Other commands

```sh
binharic say "the flesh is weak"            # screech some typed text
binharic say -m binary "hi" --wav hi.wav    # save as a WAV instead
binharic text "Hi"                          # binary: 01001000 01101001 / hex: 48 69
binharic text -r "01001000 01101001"        # back to text
```

## Notes

* Speech recognition is Vosk's small English model. Bigger and more accurate models
  are at <https://alphacephei.com/vosk/models>. Use them with
  `binharic download-model vosk-model-en-us-0.22`. Other languages work the same way.
* A phrase is sent once you pause, so there is a short delay before the screech.
  Binary mode is the slowest, at about 0.25 s per letter.
* By default the layer stops listening while it screeches, so a mic that picks up your
  speakers can't create a feedback loop. If the output only goes to the virtual mic,
  `--full-duplex` lets you keep talking over it.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[speech,test]'
.venv/bin/pytest
```
