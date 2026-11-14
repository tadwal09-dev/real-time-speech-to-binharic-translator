#!/usr/bin/env bash
# Arch Linux installer. Run from the repo root: ./install.sh
#
# Installs into its own environment (~/.local/share/binharic/venv) built with
# uv on Python 3.12, so it doesn't depend on pipx or on whichever Python
# version Arch currently ships (Whisper's dependencies can lag behind).
#
#   ./install.sh               install / update
#   ./install.sh --uninstall   remove it again (keeps downloaded models)
#
# `binharic update` runs this with BINHARIC_UPDATE=1: the quick mode. It asks for
# sudo only if something is really missing, upgrades nothing but binharic itself
# (the speech and GPU libraries stay exactly as they were), and skips `binharic fix`.
set -euo pipefail
cd "$(dirname "$0")"

PY_VERSION=3.12
DATA=~/.local/share/binharic
VENV=$DATA/venv
BIN_DIR=~/.local/bin
BIN=$BIN_DIR/binharic
# a normal command everywhere: ~/.local/bin is only on the PATH of shells that read
# ~/.bashrc, not of Hyprland/Sway keybinds or app launchers
GLOBAL=/usr/local/bin/binharic

# big downloads (CUDA, models) can drop mid-way: retry a few times
retry() { local i; for i in 1 2 3; do "$@" && return 0; echo "   retrying ($i/3)…"; sleep 3; done; return 1; }
export UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT:-600}
step() { printf '\n\033[1;31m::\033[0m \033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now binharic 2>/dev/null || true
  "$BIN" mic down 2>/dev/null || true
  if [[ "$(readlink "$GLOBAL" 2>/dev/null)" == "$VENV/bin/binharic" ]]; then sudo rm -f "$GLOBAL" || true; fi
  rm -rf "$VENV" "$BIN" ~/.config/systemd/user/binharic.service ~/.local/share/applications/binharic.desktop
  echo "removed (speech models are still in $DATA - delete that folder to free the space)"
  exit 0
fi

UPDATE=${BINHARIC_UPDATE:-}
PACKAGES=(portaudio libpulse tk libnotify uv)
# only what's missing, so nothing asks for a password when everything is there
if [[ -z "${BINHARIC_SKIP_PACMAN:-}" ]] && command -v pacman >/dev/null && ! pacman -T "${PACKAGES[@]}" >/dev/null; then
  step "system packages: ${PACKAGES[*]}"
  sudo pacman -S --needed --noconfirm "${PACKAGES[@]}" \
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

# --reinstall-package: always rebuild binharic itself from this folder. An update leaves
# every other package alone (a newer speech library could want other GPU libraries)
PIP=(uv pip install --python "$VENV/bin/python" --reinstall-package binharic)
[[ -n $UPDATE ]] || PIP+=(--upgrade)
rm -rf build  # a stale build folder would put deleted files back into the package
if [[ -n $UPDATE ]]; then step "installing the new binharic"; else
  step "installing binharic + Whisper speech recognition (first time: a few hundred MB)"; fi
if [[ -n $UPDATE ]] && ! "$VENV/bin/python" -c "import faster_whisper" 2>/dev/null; then
  # this copy runs on Vosk (Whisper didn't install here): keep it that way
  retry "${PIP[@]}" '.[vosk]' || fail "installation failed (see the error above)"
  ENGINE=vosk
elif ! retry "${PIP[@]}" '.[all]'; then
  echo "!! Whisper failed to install (see the error above); installing the lighter Vosk engine instead"
  "${PIP[@]}" '.[vosk]' || fail "installation failed (see the error above)"
  ENGINE=vosk
else
  ENGINE=whisper
fi
ln -sf "$VENV/bin/binharic" "$BIN"
"$BIN" --help >/dev/null || fail "binharic was installed but doesn't start"
# which commit this is: `binharic update` compares it with the clone
git rev-parse HEAD > "$VENV/binharic-commit" 2>/dev/null || rm -f "$VENV/binharic-commit"
if [[ "$(readlink "$GLOBAL" 2>/dev/null)" == "$VENV/bin/binharic" ]]; then
  GLOBAL_OK=1  # already there: no password needed
elif command -v sudo >/dev/null && sudo ln -sf "$VENV/bin/binharic" "$GLOBAL"; then
  GLOBAL_OK=1
else
  GLOBAL_OK=
  echo "!! couldn't link $GLOBAL: binharic works only where ~/.local/bin is on the PATH"
fi

step "desktop entry and background service"
mkdir -p ~/.config/systemd/user ~/.local/share/applications
install -m644 contrib/binharic.service ~/.config/systemd/user/binharic.service
# app launchers often don't have ~/.local/bin on their PATH: use the full path
sed "s|^Exec=binharic |Exec=$BIN |" contrib/binharic.desktop > ~/.local/share/applications/binharic.desktop
systemctl --user daemon-reload 2>/dev/null || true

step "speech models ($ENGINE)"
if [[ $ENGINE == whisper ]]; then
  retry "$BIN" download-model --engine whisper || fail "model download failed (network?) - rerun ./install.sh"
else
  retry "$BIN" download-model --engine vosk || fail "model download failed (network?) - rerun ./install.sh"
fi

step "creating the Binharic Mic"
"$BIN" mic up || echo "!! couldn't create it now - is PipeWire running? try later: binharic mic up"

if [[ -n $UPDATE ]]; then
  echo; "$BIN" --version
  exit 0  # `binharic update`: the setup was already checked when it was installed
else
  step "finding and fixing problems (it will ask you to talk; downloads GPU libraries if you have NVIDIA)"
  if [[ -n "${BINHARIC_NO_GPU:-}" ]]; then "$BIN" fix --no-gpu || true; else "$BIN" fix || true; fi
fi

if [[ -z $GLOBAL_OK && ":$PATH:" != *":$BIN_DIR:"* ]]; then
  printf '\n\033[1;33m%s is not on your PATH.\033[0m Add it with:\n' "$BIN_DIR"
  echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
fi

cat <<'MSG'

Done. Try it:
  binharic test --fast     # say something, hear the screech, see it decoded + timed
  binharic                 # the window (or the terminal dashboard over SSH)

In Discord/OBS/etc. pick "Binharic Mic" as your microphone.
Hotkeys: binharic toggle · binharic send toggle
Background service: systemctl --user enable --now binharic
MSG
