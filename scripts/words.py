#!/usr/bin/env python3
"""Word timings for a video, so effects can start on a spoken word.
  words.py <video>                 prints one line per word: start  end  word
  words.py <video> --json w.json   also writes [{"text", "start", "end"}, ...]
  --model base|small|...           faster-whisper model (default base; downloaded once, about 150 MB)"""
import argparse, json, shutil, subprocess, sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video')
    ap.add_argument('--json')
    ap.add_argument('--model', default='base')
    ap.add_argument('--language')
    a = ap.parse_args()
    ffmpeg = next((c for c in [shutil.which('ffmpeg'), '/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg'] if c and Path(c).exists()), None)
    if not ffmpeg:
        sys.exit('ffmpeg not found. Run scripts/setup.sh first.')
    import numpy as np
    from faster_whisper import WhisperModel
    # ffmpeg decodes the sound (16 kHz mono floats), so PyAV is never used
    pcm = subprocess.run([ffmpeg, '-v', 'error', '-i', a.video, '-vn', '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'], capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.float32)
    if not audio.size:
        sys.exit('This video has no sound.')
    model = WhisperModel(a.model, device='cpu', compute_type='int8')
    segments, _ = model.transcribe(audio, word_timestamps=True, language=a.language)
    words = [{'text': w.word.strip(), 'start': round(w.start, 3), 'end': round(w.end, 3)} for s in segments for w in (s.words or []) if w.word.strip()]
    for w in words:
        print(f"{w['start']:7.2f} {w['end']:7.2f}  {w['text']}")
    if a.json:
        Path(a.json).write_text(json.dumps(words, indent=1))


if __name__ == '__main__':
    main()
