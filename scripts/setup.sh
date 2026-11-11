#!/usr/bin/env bash
# One-time setup for /reel-fx. Prints READY, or NEED_FFMPEG / NEED_PYTHON and what to install.
set -e
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
cd "$(dirname "$0")/.."
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then echo "NEED_FFMPEG"; exit 1; fi
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "NEED_PYTHON (3.10 to 3.13)"; exit 1; fi
if [ ! -x .venv/bin/python ]; then "$PY" -m venv .venv; fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m playwright install chromium >/dev/null
M=https://storage.googleapis.com/mediapipe-models
get() { [ -s "models/$2" ] || curl -sfL -o "models/$2" "$1"; }
get "$M/image_segmenter/selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite" selfie_multiclass_256x256.tflite
get "$M/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite" blaze_face_short_range.tflite
.venv/bin/python -c "import mediapipe, numpy, PIL, playwright" && .venv/bin/python -c "import faster_whisper" && echo "READY"
