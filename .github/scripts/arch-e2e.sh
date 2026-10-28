#!/usr/bin/env bash
# Arch Linux + PipeWire, like the user's desktop: install with ./install.sh,
# then run the end-to-end suite against the installed `binharic`.
# Runs as a normal user (with sudo for pacman) inside dbus-run-session.
set -euo pipefail
cd "$(dirname "$0")/../.."
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
.github/scripts/pipewire.sh start

echo "::group::./install.sh"
BINHARIC_NO_GPU=1 ./install.sh
echo "::endgroup::"

VENV=~/.local/share/binharic/venv
~/.local/bin/binharic --help >/dev/null
# a normal command: found without ~/.local/bin on the PATH (keybinds, launchers)
env PATH=/usr/local/bin:/usr/bin binharic --help >/dev/null
uv pip install --python "$VENV/bin/python" --quiet pyte
export BINHARIC_E2E_CMD=~/.local/bin/binharic
export BINHARIC_E2E_RESTART="$PWD/.github/scripts/pipewire.sh restart"
xvfb-run -a -s "-screen 0 1280x1024x24" "$VENV/bin/python" -m tests.e2e.run "$@"

echo "::group::./install.sh --uninstall"
./install.sh --uninstall
test ! -e ~/.local/bin/binharic && test ! -e /usr/local/bin/binharic && test ! -d "$VENV"
echo "::endgroup::"
