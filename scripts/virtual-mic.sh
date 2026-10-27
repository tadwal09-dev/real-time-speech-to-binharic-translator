#!/usr/bin/env bash
# Create (or remove) a virtual "Binharic Mic" on PipeWire / PulseAudio.
#
#   binharic listen --sink binharic_out   ->  plays into the null sink
#   Discord / OBS / Mumble                ->  select "Binharic Mic" as input
#
# Usage: scripts/virtual-mic.sh [up|down|status]
set -euo pipefail

SINK=binharic_out
MIC=binharic_mic

command -v pactl >/dev/null || { echo "pactl missing: sudo pacman -S libpulse pipewire-pulse" >&2; exit 1; }

has() { pactl list short "$1" | awk '{print $2}' | grep -qx "$2"; }

case "${1:-up}" in
  up)
    has sinks "$SINK" || pactl load-module module-null-sink \
      sink_name="$SINK" sink_properties=device.description=Binharic_Output >/dev/null
    has sources "$MIC" || pactl load-module module-remap-source \
      master="$SINK.monitor" source_name="$MIC" source_properties=device.description=Binharic_Mic >/dev/null
    echo "ready: run 'binharic listen --sink $SINK' and pick 'Binharic Mic' in your voice app"
    echo "to hear yourself too: pactl load-module module-loopback source=$SINK.monitor"
    ;;
  down)
    for m in $(pactl list short modules | awk -v s="$SINK" -v m="$MIC" \
        '$0 ~ "sink_name="s || $0 ~ "source_name="m {print $1}'); do
      pactl unload-module "$m"
    done
    echo "removed"
    ;;
  status)
    pactl list short sinks | grep "$SINK" || echo "sink missing"
    pactl list short sources | grep "$MIC" || echo "mic missing"
    ;;
  *) echo "usage: $0 [up|down|status]" >&2; exit 2 ;;
esac
