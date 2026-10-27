#!/usr/bin/env bash
# Arch Linux installer. Run from the repo root: ./install.sh
#
# Installs into its own environment (~/.local/share/binharic/venv) built with
# uv on Python 3.12, so it doesn't depend on pipx or on whichever Python
# version Arch currently ships (Whisper's dependencies can lag behind).
#
#   ./install.sh               install / update
#   ./install.sh --uninstall   remove it again (keeps downloaded models)
set -euo pipefail
cd "$(dirname "$0")"

PY_VERSION=3.12
DATA=~/.local/share/binharic
VENV=$DATA/venv
BIN_DIR=~/.local/bin
BIN=$BIN_DIR/binharic

step() { printf '\n\033[1;31m::\033[0m \033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now binharic 2>/dev/null || true
  "$BIN" mic down 2>/dev/null || true
  rm -rf "$VENV" "$BIN" ~/.config/systemd/user/binharic.service ~/.local/share/applications/binharic.desktop
  echo "removed (speech models are still in $DATA - delete that folder to free the space)"
  exit 0
fi

if [[ -z "${BINHARIC_SKIP_PACMAN:-}" ]]; then
  step "system packages: portaudio libpulse tk libnotify uv"
  sudo pacman -S --needed --noconfirm portaudio libpulse tk libnotify uv \
    || fail "pacman failed - try 'sudo pacman -Syu' first, then run ./install.sh again"
fi
command -v uv >/dev/null || fail "uv is missing (sudo pacman -S uv)"

step "Python $PY_VERSION environment in $VENV"
mkdir -p "$DATA" "$BIN_DIR"
uv python install "$PY_VERSION" || fail "uv couldn't download Python $PY_VERSION (network?)"
if [[ ! -x "$VENV/bin/python" ]] || ! "$VENV/bin/python" -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))"; then
  rm -rf "$VENV"
  uv venv --python "$PY_VERSION" "$VENV" || fail "couldn't create the environment"
fi

step "installing binharic + Whisper speech recognition (first time: a few hundred MB)"
if ! uv pip install --python "$VENV/bin/python" --upgrade '.[all]'; then
  echo "!! Whisper failed to install (see the error above); installing the lighter Vosk engine instead"
  uv pip install --python "$VENV/bin/python" --upgrade '.[vosk]' || fail "installation failed (see the error above)"
  ENGINE=vosk
else
  ENGINE=whisper
fi
ln -sf "$VENV/bin/binharic" "$BIN"
"$BIN" --help >/dev/null || fail "binharic was installed but doesn't start"

step "desktop entry and background service"
mkdir -p ~/.config/systemd/user ~/.local/share/applications
install -m644 contrib/binharic.service ~/.config/systemd/user/binharic.service
install -m644 contrib/binharic.desktop ~/.local/share/applications/binharic.desktop
systemctl --user daemon-reload 2>/dev/null || true

step "downloading speech models ($ENGINE)"
if [[ $ENGINE == whisper ]]; then
  "$BIN" download-model --engine whisper || fail "model download failed (network?) - rerun ./install.sh"
else
  "$BIN" download-model --engine vosk || fail "model download failed (network?) - rerun ./install.sh"
fi

step "creating the Binharic Mic"
"$BIN" mic up || echo "!! couldn't create it now - is PipeWire running? try later: binharic mic up"

step "checking"
"$BIN" doctor

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf '\n\033[1;33m%s is not on your PATH.\033[0m Add it with:\n' "$BIN_DIR"
     echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
esac

cat <<'MSG'

Done. Try it:
  binharic test --fast     # say something, hear the screech, see it decoded + timed
  binharic                 # the window (or the terminal dashboard over SSH)

In Discord/OBS/etc. pick "Binharic Mic" as your microphone.
Hotkeys: binharic toggle · binharic send toggle
Background service: systemctl --user enable --now binharic
MSG
