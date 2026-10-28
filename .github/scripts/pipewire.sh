#!/usr/bin/env bash
# Run a PipeWire desktop sound stack without systemd (CI containers):
#   pipewire.sh start | restart | stop
# Needs a D-Bus session (dbus-run-session) and XDG_RUNTIME_DIR.
set -euo pipefail
: "${XDG_RUNTIME_DIR:?}"
LOG=${PIPEWIRE_LOG:-$XDG_RUNTIME_DIR/pipewire.log}

stop() {
  for p in pipewire-pulse wireplumber pipewire; do pkill -x "$p" 2>/dev/null || true; done
  for _ in $(seq 50); do pgrep -x pipewire >/dev/null || return 0; sleep 0.1; done
}

start() {
  (pipewire >>"$LOG" 2>&1 &)
  sleep 0.5
  (wireplumber >>"$LOG" 2>&1 &)
  (pipewire-pulse >>"$LOG" 2>&1 &)
  sleep 0.5
  # a desktop gives PipeWire realtime priority (rtkit), so it never misses a
  # cycle under load; do the same where the container allows it
  for p in $(pgrep -x pipewire) $(pgrep -x pipewire-pulse); do
    sudo -n chrt -f -p 20 "$p" 2>/dev/null && echo "realtime priority for $(ps -o comm= -p "$p")" || true
  done
  for _ in $(seq 100); do pactl info >/dev/null 2>&1 && { pactl info | grep -E "Server Name"; return 0; }; sleep 0.1; done
  echo "PipeWire didn't start:"; cat "$LOG"; return 1
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 0.5; start ;;
esac
