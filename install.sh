#!/usr/bin/env bash
# Arch Linux installer. Run from the repo root: ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

echo ":: installing system packages (portaudio, libpulse, pipx)"
sudo pacman -S --needed --noconfirm portaudio libpulse python-pipx libnotify

echo ":: installing binharic into ~/.local/bin via pipx"
pipx install --force '.[speech]'

mkdir -p ~/.local/share/binharic ~/.config/systemd/user
install -m755 scripts/virtual-mic.sh ~/.local/share/binharic/virtual-mic.sh
install -m644 contrib/binharic.service ~/.config/systemd/user/binharic.service

echo ":: fetching offline speech model (~40 MB)"
~/.local/bin/binharic download-model

cat <<'MSG'

Done. Quick start:
  ~/.local/share/binharic/virtual-mic.sh up
  binharic listen --sink binharic_out --passthrough
  # in Discord etc. choose "Binharic Mic" as your input
  binharic toggle          # flip ON/OFF (bind this to a hotkey)

Or run it as a background service:
  systemctl --user daemon-reload && systemctl --user enable --now binharic
MSG
