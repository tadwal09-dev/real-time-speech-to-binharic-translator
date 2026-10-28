#!/bin/sh
# iSH (iOS) installer. In iSH:
#   apk add git && git clone -b claude/happy-gates-btt1f9 https://github.com/tadwal09-dev/real-time-speech-to-binharic-translator.git
#   cd real-time-speech-to-binharic-translator && sh install-ish.sh
#
# iSH can't reach the iPhone's mic or speaker, so this version works with
# text (type, or dictate with the keyboard's mic key) and sound files.
set -e
cd "$(dirname "$0")"

echo ":: packages (python, numpy from Alpine - compiling it in iSH would take hours)"
apk add --no-cache python3 py3-numpy py3-pip
echo ":: ffmpeg lets you decode Voice Memos recordings (m4a); skip with NO_FFMPEG=1"
[ -n "$NO_FFMPEG" ] || apk add --no-cache ffmpeg || echo "!! ffmpeg didn't install; WAV files still work"

echo ":: installing binharic"
python3 -m venv --system-site-packages "$HOME/.binharic-venv"
# --no-deps: numpy comes from Alpine; the mic/speaker and speech parts can't work in iSH anyway
"$HOME/.binharic-venv/bin/pip" install --quiet --no-deps .
ln -sf "$HOME/.binharic-venv/bin/binharic" /usr/local/bin/binharic

echo ":: checking"
binharic fix --no-gpu || true

cat <<'MSG'

Done. Start with:   binharic
  type a line, or tap the keyboard's mic key and talk, then Enter.
  Each line becomes a sound file: Files app -> iSH -> root -> binharic (latest.wav = newest).
Decode a recording: save it from Voice Memos to Files -> iSH, then
  binharic decode --wav ~/name.m4a
MSG
