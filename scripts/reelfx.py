#!/usr/bin/env python3
"""claude-reel-effects renderer.

  reelfx.py --plan plan.json <video> <out.mp4>             render every effect in the plan
  reelfx.py --fx orbit --start 3 --end 6 <video> <out.mp4>  one effect (add --text "..." and --opt key=value)
  add --still 4.5,9.2 to write check images (<out>_4.5s.png ...) instead of the video

plan.json: {"effects": [{"fx": "orbit", "start": 3.2, "end": 6.0}, {"fx": "spotlight", "start": 8, "end": 10, "text": "Claude"}]}
Every key besides fx/start/end is an option for that effect (text, color, images, ...).

The output is 1080x1920 at 30 fps with the original audio. Per effect window: frames from the video, a person cut-out
(MediaPipe selfie segmenter), the face box (MediaPipe face detector) and a clean background plate, drawn frame by frame
by engine/effects.html in headless Chromium, then laid over the video with ffmpeg. Work files are cached in work/.
"""
import argparse, base64, hashlib, http.server, io, json, math, os, shutil, subprocess, sys, threading, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FPS, W, H = 30, 1080, 1920
EFFECTS = ['glass-widgets', 'vertigo', 'orbit', 'lockdown', 'paper-cut', 'spotlight', 'backdrop-blur', 'digital-zoom', 'rabbit-hole',
           'shadow-puppet', 'shortcut-rain', 'reject-stamp', 'giant-captions', 'countdown', 'punch-in', 'comment-type', 'follow-tap',
           'checklist', 'receipt', 'magazine-cover', 'neon-sign', 'freeze-arrow', 'before-after', 'napkin-note', 'notifications']
ALIASES = {'glass': 'glass-widgets', 'lock': 'lockdown', 'locked-on': 'lockdown', 'papercut': 'paper-cut', 'spot': 'spotlight',
           'backdrop': 'backdrop-blur', 'zoom': 'digital-zoom', 'rabbit': 'rabbit-hole', 'puppet': 'shadow-puppet', 'rain': 'shortcut-rain',
           'stamp': 'reject-stamp', 'captions': 'giant-captions', 'count': 'countdown', 'punch': 'punch-in', 'comment': 'comment-type',
           'follow': 'follow-tap', 'todo': 'checklist', 'cover': 'magazine-cover', 'neon': 'neon-sign', 'freeze': 'freeze-arrow',
           'swipe': 'before-after', 'napkin': 'napkin-note', 'messages': 'notifications'}
NEEDS_PLATE = {'vertigo', 'rabbit-hole'}
NEEDS_WORDS = {'giant-captions'}


def tool(name):
    for c in [shutil.which(name), f'/opt/homebrew/bin/{name}', f'/usr/local/bin/{name}']:
        if c and Path(c).exists():
            return c
    sys.exit(f'{name} not found. Run scripts/setup.sh first.')


FF, FP = tool('ffmpeg'), tool('ffprobe')


def ff(args, quiet=True):
    r = subprocess.run([FF, '-v', 'error', '-y', '-nostdin'] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {r.stderr[-800:]}')


def probe(path):
    out = json.loads(subprocess.run([FP, '-v', 'error', '-show_entries', 'stream=codec_type,width,height:format=duration', '-of', 'json', str(path)],
                                    capture_output=True, text=True).stdout or '{}')
    v = next((s for s in out.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not v:
        sys.exit(f'No video stream in {path}')
    return int(v['width']), int(v['height']), float(out['format']['duration']), any(s.get('codec_type') == 'audio' for s in out['streams'])


# ---------------- the plan ----------------
def load_plan(a):
    if a.plan:
        raw = json.loads(Path(a.plan).read_text())
        items = raw['effects'] if isinstance(raw, dict) else raw
    else:
        if not a.fx:
            sys.exit('Give --plan plan.json or --fx <effect> --start S --end E')
        item = {'fx': a.fx, 'start': a.start, 'end': a.end}
        if a.text is not None:
            item['text'] = a.text
        for kv in a.opt or []:
            k, _, v = kv.partition('=')
            item[k.strip()] = v
        items = [item]
    effects = []
    for it in items:
        name = ALIASES.get(str(it['fx']).strip().lower(), str(it['fx']).strip().lower())
        if name not in EFFECTS:
            sys.exit(f'Unknown effect "{it["fx"]}". Effects: {", ".join(EFFECTS)}')
        opts = dict(it.get('opts') or {})
        opts.update({k: v for k, v in it.items() if k not in ('fx', 'start', 'end', 'opts')})
        effects.append({'fx': name, 'a': float(it.get('start') or 0), 'b': float(it['end']) if it.get('end') is not None else None, 'opts': opts})
    return effects


def fix_windows(effects, duration):
    effects.sort(key=lambda e: e['a'])
    for i, e in enumerate(effects):
        e['b'] = duration if e['b'] is None else min(e['b'], duration)
        e['a'] = max(0.0, e['a'])
        if i + 1 < len(effects) and e['b'] > effects[i + 1]['a']:
            print(f'note: {e["fx"]} ends where {effects[i + 1]["fx"]} starts ({effects[i + 1]["a"]:.2f}s)', flush=True)
            e['b'] = effects[i + 1]['a']
        if e['b'] - e['a'] < 0.2:
            sys.exit(f'{e["fx"]}: the window {e["a"]:.2f}-{e["b"]:.2f}s is too short (the video is {duration:.2f}s long)')
        e['n0'], e['n1'] = math.ceil(e['a'] * FPS - 1e-6), math.ceil(e['b'] * FPS - 1e-6)
    return effects


# ---------------- vision ----------------
class Vision:
    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks import python as mpt
        from mediapipe.tasks.python import vision
        seg_m, face_m = ROOT / 'models' / 'selfie_multiclass_256x256.tflite', ROOT / 'models' / 'blaze_face_short_range.tflite'
        if not seg_m.exists() or not face_m.exists():
            sys.exit('Models are missing. Run scripts/setup.sh first.')
        self.mp = mp
        self.seg = vision.ImageSegmenter.create_from_options(vision.ImageSegmenterOptions(
            base_options=mpt.BaseOptions(model_asset_path=str(seg_m)), running_mode=vision.RunningMode.IMAGE,
            output_confidence_masks=True, output_category_mask=False))
        self.face = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=mpt.BaseOptions(model_asset_path=str(face_m)), running_mode=vision.RunningMode.IMAGE, min_detection_confidence=0.5))

    def image(self, arr):
        return self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=arr)

    def person(self, small):     # person probability, small-frame size
        import numpy as np
        bg = self.seg.segment(self.image(small)).confidence_masks[0].numpy_view()
        return np.clip(1.0 - bg[..., 0] if bg.ndim == 3 else 1.0 - bg, 0, 1)

    def face_box(self, small, scale):
        det = self.face.detect(self.image(small)).detections
        if not det:
            return None
        b = max(det, key=lambda d: d.bounding_box.width).bounding_box
        return (b.origin_x + b.width / 2) * scale, (b.origin_y + b.height / 2) * scale, b.width * scale


def first_face(vis, frame_path):
    import numpy as np
    from PIL import Image
    im = Image.open(frame_path).convert('RGB')
    sc = 640 / max(im.size)
    small = np.asarray(im.resize((round(im.width * sc), round(im.height * sc))))
    f = vis.face_box(small, 1 / sc)
    return f


def window_assets(vis, base, e, wdir):
    """head frames, RGBA cut-outs, faces.json and (if needed) plate.jpg for one effect window."""
    import numpy as np
    from PIL import Image, ImageFilter
    head, cut = wdir / 'head', wdir / 'cut'
    if (wdir / 'faces.json').exists() and (e['fx'] not in NEEDS_PLATE or (wdir / 'plate.jpg').exists()):
        return json.loads((wdir / 'faces.json').read_text())
    shutil.rmtree(wdir, ignore_errors=True)
    head.mkdir(parents=True)
    cut.mkdir()
    ff(['-ss', f'{e["n0"] / FPS:.4f}', '-i', str(base), '-frames:v', str(e['n1'] - e['n0']), '-q:v', '2', str(head / '%05d.jpg')])
    files = sorted(head.glob('*.jpg'))
    prev, faces, last = None, [], None
    for f in files:
        im = Image.open(f).convert('RGB')
        small = np.asarray(im.resize((540, 960), Image.BILINEAR))
        p = vis.person(small)
        p = np.clip((p - 0.3) / 0.4, 0, 1)                       # a firm edge: the model is never fully sure
        p = p if prev is None else 0.65 * p + 0.35 * prev          # less flicker between frames
        prev = p
        a = Image.fromarray(np.uint8(p * 255)).resize((W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.2))
        rgba = im.copy()
        rgba.putalpha(a)
        rgba.save(cut / (f.stem + '.png'), compress_level=1)
        box = vis.face_box(small, 2.0)
        if box:
            last = box if last is None else tuple(0.5 * b + 0.5 * l for b, l in zip(box, last))
        faces.append(last)
    fill = next((x for x in faces if x), None) or (540, 700, 300)
    faces = [{'x': round(x[0]), 'y': round(x[1]), 'w': round(x[2])} for x in [(fx or fill) for fx in faces]]
    # frames before the first detection take the first found face
    if e['fx'] in NEEDS_PLATE:
        plate(head, cut, wdir / 'plate.jpg')
    (wdir / 'faces.json').write_text(json.dumps(faces))
    return faces


def plate(head_dir, cut_dir, out):
    """the background without the person: average of the uncovered pixels, holes filled from a blur pyramid."""
    import numpy as np
    from PIL import Image, ImageFilter
    hs, cs = sorted(head_dir.glob('*.jpg')), sorted(cut_dir.glob('*.png'))
    step = max(1, len(hs) // 30)
    acc = w = None
    for h, c in list(zip(hs, cs))[::step]:
        bg = (np.asarray(Image.open(c))[:, :, 3:4] / 255.0 < 0.05).astype(np.float32)
        im = np.asarray(Image.open(h).convert('RGB'), np.float32)
        acc = im * bg if acc is None else acc + im * bg
        w = bg if w is None else w + bg
    img, mask = acc / np.maximum(w, 1), (w[:, :, 0] > 0).astype(np.float32)
    for r in (8, 16, 32, 64, 128, 256):
        if mask.min() >= 0.5:
            break
        bi = np.asarray(Image.fromarray(np.uint8(np.clip(img * mask[:, :, None], 0, 255))).filter(ImageFilter.GaussianBlur(r)), np.float32)
        bm = np.asarray(Image.fromarray(np.uint8(mask * 255)).filter(ImageFilter.GaussianBlur(r)), np.float32) / 255
        fill, hole = bi / np.maximum(bm[:, :, None], 1e-3), (mask < 0.5) & (bm > 0.02)
        img[hole] = fill[hole]
        mask = np.where(hole, 1.0, mask)
    Image.fromarray(np.uint8(np.clip(img, 0, 255))).save(out, quality=92)


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser(description='Add reel effects to a talking-head video.')
    ap.add_argument('video')
    ap.add_argument('out')
    ap.add_argument('--plan')
    ap.add_argument('--fx')
    ap.add_argument('--start', type=float, default=0.0)
    ap.add_argument('--end', type=float)
    ap.add_argument('--text')
    ap.add_argument('--opt', action='append', help='key=value, repeatable')
    ap.add_argument('--still', help='seconds, comma separated: write check images instead of the video')
    ap.add_argument('--images', help='a folder of your own screenshots or covers for orbit')
    ap.add_argument('--words', help='a words.json from scripts/words.py (else made when an effect needs it)')
    a = ap.parse_args()

    src = Path(a.video).expanduser().resolve()
    if not src.exists():
        sys.exit(f'Video not found: {src}')
    out = Path(a.out).expanduser().resolve()
    effects = load_plan(a)
    sw, sh, dur, has_audio = probe(src)

    st = src.stat()
    key = hashlib.md5(f'{src}|{st.st_size}|{int(st.st_mtime)}'.encode()).hexdigest()[:12]
    work = ROOT / 'work' / key
    work.mkdir(parents=True, exist_ok=True)

    print('Loading the face and person models...', flush=True)
    vis = Vision()
    base = work / 'base.mp4'
    if not base.exists():
        print('Preparing the video (1080x1920, 30 fps)...', flush=True)
        sc = max(W / sw, H / sh)
        tw, th = round(sw * sc / 2) * 2, round(sh * sc / 2) * 2
        cx, cy = tw / 2, th / 2
        if tw > W or th > H:          # crop around the face
            probe_jpg = work / 'probe.jpg'
            ff(['-ss', f'{min(1.0, dur / 2):.2f}', '-i', str(src), '-frames:v', '1', '-vf', f'scale={tw}:{th}', str(probe_jpg)])
            f = first_face(vis, probe_jpg)
            if f:
                cx, cy = f[0], f[1] + H * 0.12
        x, y = int(min(max(0, cx - W / 2), tw - W)), int(min(max(0, cy - H / 2), th - H))
        ff(['-i', str(src), '-an', '-vf', f'fps={FPS},scale={tw}:{th}:flags=lanczos,crop={W}:{H}:{x}:{y},setsar=1',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16', '-pix_fmt', 'yuv420p', str(base)])
    bdur = probe(base)[2]
    effects = fix_windows(effects, bdur)

    words = []
    if any(e['fx'] in NEEDS_WORDS for e in effects):
        wf = Path(a.words) if a.words else work / 'words.json'
        if not wf.exists():
            print('Listening for word timings...', flush=True)
            if not has_audio:
                sys.exit('giant-captions needs speech, and this video has no sound. Give the words with "text" instead.')
            subprocess.run([sys.executable, str(ROOT / 'scripts' / 'words.py'), str(src), '--json', str(wf)], check=True, stdout=subprocess.DEVNULL)
        words = json.loads(wf.read_text())

    # thumbnails for orbit: the user's own images, else frames from across the video
    tdir = work / 'thumbs'
    img_dir = a.images or next((e['opts'].get('images') for e in effects if e['opts'].get('images')), None)
    thumbs = []
    if any(e['fx'] == 'orbit' for e in effects):
        shutil.rmtree(tdir, ignore_errors=True)
        tdir.mkdir()
        if img_dir and Path(img_dir).expanduser().is_dir():
            from PIL import Image
            pics = sorted(p for p in Path(img_dir).expanduser().iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'))[:8]
            for i, p in enumerate(pics):
                im = Image.open(p).convert('RGB')
                im.thumbnail((480, 860))
                im.save(tdir / f't{i}.jpg', quality=90)
                thumbs.append(f't{i}')
        if not thumbs:
            for i, f in enumerate([0.12, 0.28, 0.44, 0.6, 0.76, 0.9]):
                ff(['-ss', f'{bdur * f:.2f}', '-i', str(base), '-frames:v', '1', '-vf', 'scale=360:-1', '-q:v', '3', str(tdir / f't{i}.jpg')])
                thumbs.append(f't{i}')

    # what each window needs
    stills = [float(x) for x in a.still.split(',')] if a.still else None
    wanted = effects if not stills else [e for e in effects if any(e['a'] <= t < e['b'] for t in stills)]
    for i, e in enumerate(effects):
        e['i'] = i
    for e in wanted:
        print(f'Tracking you for {e["fx"]} ({e["a"]:.2f}-{e["b"]:.2f}s)...', flush=True)
        e['dir'] = work / f'w_{e["fx"]}_{e["n0"]}_{e["n1"]}'
        e['faces'] = window_assets(vis, base, e, e['dir'])

    # ---- draw ----
    mounts = {'/engine/': ROOT / 'engine', '/fonts/': ROOT / 'fonts', '/work/': work}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = urllib.parse.unquote(path.split('?', 1)[0])
            for pre, base_dir in mounts.items():
                if path.startswith(pre):
                    return str(base_dir / path[len(pre):])
            return str(ROOT / '__none__')

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{srv.server_address[1]}'
    rel = lambda p: f'{url}/work/{p.relative_to(work).as_posix()}'

    from playwright.sync_api import sync_playwright
    page_plan = {'effects': [{'fx': e['fx'], 'a': e['a'], 'b': e['b'], 'opts': e['opts']} for e in effects], 'words': words, 'thumbs': len(thumbs)}
    statics = {t: f'{url}/work/thumbs/{t}.jpg' for t in thumbs}
    statics.update({f'plate{e["i"]}': rel(e['dir'] / 'plate.jpg') for e in wanted if e['fx'] in NEEDS_PLATE})
    written = []
    clips = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={'width': W, 'height': H})
        errors = []
        pg.on('pageerror', lambda err: errors.append(str(err)))
        pg.goto(f'{url}/engine/effects.html')
        pg.evaluate('([plan, s]) => window.init(plan, s)', [page_plan, statics])

        def frame(e, n):
            k = 1 if e['fx'] == 'freeze-arrow' else min(len(e['faces']), max(1, n - e['n0'] + 1))
            data = pg.evaluate('([t, i, h, m, pk, f]) => window.renderFrame(t, i, h, m, pk, f)',
                               [n / FPS, e['i'], rel(e['dir'] / 'head' / f'{k:05d}.jpg'), rel(e['dir'] / 'cut' / f'{k:05d}.png'), f'plate{e["i"]}', e['faces'][k - 1]])
            if errors:
                raise RuntimeError(f'{e["fx"]} failed to draw: {errors[0]}')
            return base64.b64decode(data.split(',', 1)[1])

        if stills:
            from PIL import Image
            for t in stills:
                e = next((x for x in wanted if x['a'] <= t < x['b']), None)
                dest = out.with_name(f'{out.stem}_{t:g}s.png')
                if e:
                    Image.open(io.BytesIO(frame(e, min(e['n1'] - 1, max(e['n0'], round(t * FPS)))))).save(dest)
                else:
                    ff(['-ss', f'{t:.3f}', '-i', str(base), '-frames:v', '1', str(dest)])
                written.append(dest)
        else:
            for e in effects:
                print(f'Drawing {e["fx"]} ({e["n1"] - e["n0"]} frames)...', flush=True)
                clip = e['dir'] / 'fx.mp4'
                enc = subprocess.Popen([FF, '-v', 'error', '-y', '-f', 'image2pipe', '-framerate', str(FPS), '-c:v', 'mjpeg', '-i', '-',
                                        '-c:v', 'libx264', '-preset', 'medium', '-crf', '15', '-pix_fmt', 'yuv420p', str(clip)], stdin=subprocess.PIPE)
                for n in range(e['n0'], e['n1']):
                    enc.stdin.write(frame(e, n))
                enc.stdin.close()
                if enc.wait() != 0:
                    raise RuntimeError(f'encoding {e["fx"]} failed')
                clips.append((e, clip))
        b.close()
    srv.shutdown()

    if stills:
        for w in written:
            print(f'check image: {w}')
        return

    print('Putting it together...', flush=True)
    inputs = ['-i', str(base), '-i', str(src)]
    g, last = '', '[0:v]'
    for k, (e, clip) in enumerate(clips):
        inputs += ['-i', str(clip)]
        g += (f'[{k + 2}:v]setpts=PTS-STARTPTS+{e["n0"]}/{FPS}/TB[f{k}];'
              f"{last}[f{k}]overlay=eof_action=pass:enable='between(t,{e['n0'] / FPS - 0.001:.4f},{e['b']:.4f})'[v{k}];")
        last = f'[v{k}]'
    g += f'{last}format=yuv420p[v]'
    common = ['-filter_complex', g, '-map', '[v]'] + (['-map', '1:a:0'] if has_audio else []) + ['-t', f'{bdur:.3f}',
              '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
    try:
        ff(inputs + common + (['-c:a', 'copy'] if has_audio else []) + [str(out)])
    except RuntimeError:
        ff(inputs + common + (['-c:a', 'aac', '-b:a', '192k'] if has_audio else []) + [str(out)])
    print(f'done: {out}')


if __name__ == '__main__':
    main()
