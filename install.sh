#!/usr/bin/env bash
# Arch Linux installer. Run from the repo root: ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

echo ":: system packages (portaudio, libpulse, tk, pipx, libnotify)"
sudo pacman -S --needed --noconfirm portaudio libpulse tk python-pipx libnotify

echo ":: installing binharic into ~/.local/bin (pipx)"
if ! pipx install --force '.[all]'; then
  echo "!! Whisper didn't install for this Python; falling back to Vosk only"
  pipx install --force '.[vosk]'
fi
BIN=~/.local/bin/binharic

mkdir -p ~/.config/systemd/user ~/.local/share/applications
install -m644 contrib/binharic.service ~/.config/systemd/user/binharic.service
install -m644 contrib/binharic.desktop ~/.local/share/applications/binharic.desktop

echo ":: downloading speech models (Whisper small.en + base.en, ~600 MB)"
"$BIN" download-model || "$BIN" download-model --engine vosk

echo ":: creating the Binharic Mic"
"$BIN" mic up || true
"$BIN" doctor

cat <<'MSG'

Done.
  binharic                 # opens the window (or the terminal dashboard over SSH)
  binharic tui | listen    # terminal dashboard / plain log
  binharic toggle          # hotkey: ON/OFF
  binharic send toggle     # hotkey: after I finish <-> as I talk

In Discord/OBS/etc. pick "Binharic Mic" as your microphone.
Background service: systemctl --user daemon-reload && systemctl --user enable --now binharic
MSG
