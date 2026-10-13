#!/usr/bin/env python3
"""
meh-render: JSON2Video formatındaki film tanımını ücretsiz olarak ffmpeg ile render eder.
Kullanım:  python render.py job.json cikti.mp4
Desteklenen: scenes[].background-color / transition(fade) / elements: video, image, audio, text
"""
import json, sys, os, re, math, hashlib, subprocess, shutil, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

FPS = 25
SR = 44100
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get('RENDER_CACHE', '/tmp/render_cache')
os.makedirs(CACHE, exist_ok=True)
MAX_MB = float(os.environ.get('MAX_MB', '47'))     # Telegram bot sınırı 50 MB

# ---------------- yardımcılar ----------------
def log(*a): print(*a, flush=True)

def color(c, default=(0, 0, 0, 0)):
    if not c or c == 'transparent': return default
    c = c.strip()
    if c.startswith('#'):
        h = c[1:]
        if len(h) == 3: h = ''.join(x * 2 for x in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        a = int(h[6:8], 16) if len(h) == 8 else 255
        return (r, g, b, a)
    m = re.match(r'rgba?\(([^)]+)\)', c)
    if m:
        p = [x.strip() for x in m.group(1).split(',')]
        a = float(p[3]) if len(p) > 3 else 1.0
        return (int(float(p[0])), int(float(p[1])), int(float(p[2])), int(round(a * 255)))
    return default

def fetch(url):
    if not url: return None
    if os.path.exists(url): return url
    ext = os.path.splitext(url.split('?')[0])[1][:5]
    if not re.match(r'^\.[a-z0-9]+$', ext or ''): ext = ''
    p = os.path.join(CACHE, hashlib.md5(url.encode()).hexdigest() + ext)
    if os.path.exists(p) and os.path.getsize(p) > 0: return p
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 meh-render'})
            with urllib.request.urlopen(req, timeout=120) as r, open(p + '.part', 'wb') as f:
                shutil.copyfileobj(r, f)
            os.replace(p + '.part', p)
            return p
        except Exception as e:
            log('  indirme hatası', attempt + 1, url[:90], e)
    return None

def probe_duration(path):
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                             capture_output=True, text=True).stdout.strip()
        return float(out)
    except Exception:
        return 0.0

def has_audio(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index', '-of', 'csv=p=0', path],
                         capture_output=True, text=True).stdout.strip()
    return bool(out)

def load_audio(path, vol=1.0):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vn', '-ac', '2', '-ar', str(SR), '-f', 'f32le', '-'],
                         capture_output=True).stdout
    a = np.frombuffer(raw, np.float32).reshape(-1, 2).copy()
    return a * vol

# ---------------- fontlar ----------------
FONT_FILES = {
    900: ['fonts/Montserrat-Black.ttf'], 800: ['fonts/Montserrat-ExtraBold.ttf', 'fonts/Montserrat-Black.ttf'],
    700: ['fonts/Montserrat-Bold.ttf', 'fonts/Montserrat-ExtraBold.ttf'],
}
FALLBACK = ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf']
_fonts = {}
def font(weight, size):
    w = 900 if weight >= 850 else (800 if weight >= 750 else 700)
    key = (w, size)
    if key not in _fonts:
        for f in FONT_FILES[w] + FALLBACK:
            fp = f if os.path.isabs(f) else os.path.join(HERE, f)
            if os.path.exists(fp):
                _fonts[key] = ImageFont.truetype(fp, size); break
    return _fonts[key]

def parse_shadow(s):
    """KENAR tipi (çok yönlü, bulanıksız) gölge -> kontur; tek bulanık gölge -> yumuşak gölge."""
    if not s or s == 'none': return None
    parts = [p.strip() for p in re.split(r',(?![^(]*\))', s)]
    stroke, soft = 0, None
    for p in parts:
        m = re.match(r'(-?\d+)px\s+(-?\d+)px\s+(\d+)px\s+(.+)|(-?\d+)\s+(-?\d+)px\s+(\d+)px\s+(.+)|(-?\d+)(?:px)?\s+(-?\d+)(?:px)?\s+(\d+)(?:px)?\s+(.+)', p)
        if not m: continue
        g = [x for x in m.groups() if x is not None]
        dx, dy, blur, col = int(g[0]), int(g[1]), int(g[2]), color(g[3])
        if blur == 0: stroke = max(stroke, abs(dx), abs(dy))
        else: soft = (dx, dy, blur, col)
    return {'stroke': stroke, 'soft': soft}

def wrap(draw, text, f, maxw, stroke=0):
    words = text.split()
    lines, cur = [], ''
    for w in words:
        t = (cur + ' ' + w).strip()
        if draw.textlength(t, font=f) + 2 * stroke <= maxw or not cur: cur = t
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def render_text(el, W, H):
    """Yazı elemanını tam kare RGBA katman olarak üretir (bir kez)."""
    st = el.get('settings', {})
    text = str(el.get('text', '')).strip()
    size = int(re.sub(r'[^\d]', '', str(st.get('font-size', '64'))) or 64)
    weight = int(re.sub(r'[^\d]', '', str(st.get('font-weight', '700'))) or 700)
    fcol = color(st.get('font-color', '#FFFFFF'), (255, 255, 255, 255))
    bg = color(st.get('background-color'))
    radius = int(re.sub(r'[^\d]', '', str(st.get('border-radius', '0'))) or 0)
    sh = parse_shadow(st.get('text-shadow'))
    vpos = st.get('vertical-position', 'center')
    align = st.get('text-align', 'center')
    custom = el.get('position') == 'custom'
    if custom:
        bx, by = int(el.get('x', 0)), int(el.get('y', 0))
        bw, bh = int(el.get('width', W)), int(el.get('height', H))
    else:
        bx, by, bw, bh = 0, 0, W, H
    lay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    if not text:
        if custom and bg[3] > 0:
            ImageDraw.Draw(lay).rounded_rectangle([bx, by, bx + bw, by + bh], radius, fill=bg)
        return lay
    d = ImageDraw.Draw(lay)
    stroke = sh['stroke'] if sh else 0
    padx = 40 if not custom else 16
    maxw = bw - 2 * padx - (120 if not custom else 0)
    # sığdır: gerekirse küçült
    while True:
        f = font(weight, size)
        lines = wrap(d, text, f, maxw, stroke)
        asc, desc = f.getmetrics()
        lh = int((asc + desc) * 1.08)
        th = lh * len(lines)
        widest = max(d.textlength(l, font=f) for l in lines)
        limit_h = bh - 16 if custom else H * 0.45
        if (th <= limit_h and widest <= maxw + 2) or size <= 20: break
        size = int(size * 0.9)
    if custom:
        if bg[3] > 0: d.rounded_rectangle([bx, by, bx + bw, by + bh], radius, fill=bg)
        y0 = by + (bh - th) // 2
    else:
        margin = int(H * 0.06)
        if vpos == 'bottom': y0 = H - margin - th
        elif vpos == 'top': y0 = margin
        else: y0 = (H - th) // 2
        if bg[3] > 0:
            pad_y = int(size * 0.28)
            x1 = (W - widest) // 2 - padx if align == 'center' else bx + padx - padx
            d.rounded_rectangle([x1, y0 - pad_y, x1 + widest + 2 * padx, y0 + th + pad_y], max(radius, 12), fill=bg)
    # yumuşak gölge
    if sh and sh['soft']:
        dx, dy, blur, scol = sh['soft']
        sl = Image.new('RGBA', (W, H), (0, 0, 0, 0)); sd = ImageDraw.Draw(sl)
        for k, l in enumerate(lines):
            lw = d.textlength(l, font=f)
            x = bx + (bw - lw) / 2 if align == 'center' else bx + padx
            sd.text((x + dx, y0 + k * lh + dy), l, font=f, fill=scol)
        sl = sl.filter(ImageFilter.GaussianBlur(max(1, blur // 2)))
        sl.alpha_composite(lay); lay = sl; d = ImageDraw.Draw(lay)
    for k, l in enumerate(lines):
        lw = d.textlength(l, font=f)
        x = bx + (bw - lw) / 2 if align == 'center' else bx + padx
        d.text((x, y0 + k * lh), l, font=f, fill=fcol, stroke_width=stroke, stroke_fill=(0, 0, 0, 255) if stroke else None)
    return lay

# ---------------- kare katmanları ----------------
def to_premul(rgba):
    a = np.asarray(rgba).astype(np.float32)
    alpha = a[..., 3:4] / 255.0
    return a[..., :3] * alpha, alpha

class Layer:
    """Konumu sabit, zamanla sadece opaklığı değişen katman (yazı)."""
    def __init__(self, rgba):
        bbox = rgba.getbbox()
        self.empty = bbox is None
        if self.empty: return
        self.x, self.y = bbox[0], bbox[1]
        self.rgb, self.a = to_premul(rgba.crop(bbox))

def blend_layer(frame, layer, opacity=1.0):
    if layer.empty or opacity <= 0: return
    h, w = layer.a.shape[:2]
    reg = frame[layer.y:layer.y + h, layer.x:layer.x + w]
    a = layer.a * opacity
    reg[:] = layer.rgb * opacity + reg * (1 - a)

def paste_rgb(frame, img, x, y, opacity=1.0, alpha=None):
    """img: HxWx3 float32; x,y kare dışına taşabilir."""
    H, W = frame.shape[:2]
    h, w = img.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0: return
    src = img[y0 - y:y1 - y, x0 - x:x1 - x]
    if alpha is None and opacity >= 1:
        frame[y0:y1, x0:x1] = src; return
    a = (alpha[y0 - y:y1 - y, x0 - x:x1 - x] if alpha is not None else 1.0) * opacity
    frame[y0:y1, x0:x1] = src * a + frame[y0:y1, x0:x1] * (1 - a)

class VideoReader:
    def __init__(self, path, w, h, loop=True, cover=True):
        self.w, self.h = w, h
        vf = (f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}' if cover else f'scale={w}:{h}') + f',fps={FPS},format=rgb24'
        cmd = ['ffmpeg', '-v', 'error'] + (['-stream_loop', '-1'] if loop else []) + ['-i', path, '-an', '-vf', vf, '-f', 'rawvideo', '-']
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=w * h * 3 * 2)
        self.last = np.zeros((h, w, 3), np.float32)
        self.n = 0
    def frame_at(self, idx):
        while self.n <= idx:
            buf = self.p.stdout.read(self.w * self.h * 3)
            if len(buf) < self.w * self.h * 3: self.n = idx + 1; break
            self.last = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3).astype(np.float32)
            self.n += 1
        return self.last
    def close(self):
        try: self.p.kill()
        except Exception: pass

def ease_io(t): t = min(max(t, 0.0), 1.0); return t * t * (3 - 2 * t)

class ImageEl:
    def __init__(self, el, path, W, H):
        self.el = el
        im = Image.open(path).convert('RGBA')
        corr = el.get('correction') or {}
        if corr:
            rgb, a = im.convert('RGB'), im.getchannel('A')
            if 'brightness' in corr: rgb = ImageEnhance.Brightness(rgb).enhance(max(0.0, 1 + float(corr['brightness'])))
            if 'saturation' in corr: rgb = ImageEnhance.Color(rgb).enhance(max(0.0, float(corr['saturation'])))
            if 'contrast' in corr: rgb = ImageEnhance.Contrast(rgb).enhance(max(0.0, 1 + float(corr['contrast'])))
            im = rgb.convert('RGBA'); im.putalpha(a)
        ck = el.get('chroma-key')
        if ck:
            arr = np.asarray(im).astype(np.int16)
            c = color(ck.get('color', '#00FF00'))
            tol = float(ck.get('tolerance', 30)) * 2.55
            dist = np.sqrt(((arr[..., :3] - np.array(c[:3])) ** 2).sum(-1))
            alpha = np.clip((dist - tol) / max(1.0, tol * 0.5), 0, 1) * (arr[..., 3] / 255.0)
            arr = arr.astype(np.uint8); arr[..., 3] = (alpha * 255).astype(np.uint8)
            im = Image.fromarray(arr, 'RGBA')
        self.custom = el.get('position') == 'custom'
        self.zoom = float(el.get('zoom', 0) or 0)
        self.pan = el.get('pan')
        self.pand = float(el.get('pan-distance', 0.08) or 0.08)
        self.W, self.H = W, H
        if self.custom:
            self.x, self.y = int(el.get('x', 0)), int(el.get('y', 0))
            bw, bh = int(el.get('width', im.width)), int(el.get('height', im.height))
            im = im.resize((bw, bh), Image.LANCZOS)
            self.rgb, self.a = to_premul(im)
            self.rgb = self.rgb / np.maximum(self.a, 1e-4)   # düz renk
            self.static = None
        else:
            # cover + hareket payı: kaynak, en büyük yakınlaştırmada bile büyütme gerektirmeyecek boyutta tutulur
            self.pm = 1 + (self.pand if self.pan else 0)
            self.need = (1 + abs(self.zoom) * 0.03) * self.pm
            s = max(W / im.width, H / im.height) * self.need
            self.src = im.convert('RGB').resize((max(W, int(im.width * s) + 1), max(H, int(im.height * s) + 1)), Image.LANCZOS)
            self.static = None if (self.zoom or self.pan) else np.asarray(self.cover_view(0.0)).astype(np.float32)

    def cover_view(self, p):
        W, H = self.W, self.H
        z = 1.0
        if self.zoom:
            amt = abs(self.zoom) * 0.03
            z = 1 + amt * (ease_io(p) if self.zoom > 0 else 1 - ease_io(p))
        zz = z * self.pm
        # src'nin "kapla" ölçeği need; görünen alan = kare * need / zz (src pikseli)
        k = min(self.src.width / W, self.src.height / H)
        cw, ch = W * k / zz, H * k / zz
        cx, cy = self.src.width / 2, self.src.height / 2
        if self.pan:
            d = (self.pm - 1) / 2 * (2 * ease_io(p) - 1)
            if self.pan == 'right': cx += d * cw
            elif self.pan == 'left': cx -= d * cw
            elif self.pan == 'bottom': cy += d * ch
            elif self.pan == 'top': cy -= d * ch
        x0 = min(max(cx - cw / 2, 0), self.src.width - cw)
        y0 = min(max(cy - ch / 2, 0), self.src.height - ch)
        return self.src.resize((W, H), Image.BILINEAR, box=(x0, y0, x0 + cw, y0 + ch))

    def draw(self, frame, p, opacity):
        if self.custom:
            paste_rgb(frame, self.rgb, self.x, self.y, opacity, self.a)
        else:
            img = self.static if self.static is not None else np.asarray(self.cover_view(p)).astype(np.float32)
            paste_rgb(frame, img, 0, 0, opacity)

# ---------------- film ----------------
def build(movie):
    W = int(movie.get('width', 1920)); H = int(movie.get('height', 1080))
    if movie.get('resolution') in ('full-hd',): W, H = 1920, 1080
    scenes = movie.get('scenes', [])
    # 1) tüm kaynakları paralel indir
    urls = set()
    for sc in scenes:
        for el in sc.get('elements', []):
            if el.get('type') in ('video', 'image', 'audio') and el.get('src'): urls.add(el['src'])
    log(f'{len(scenes)} sahne, {len(urls)} kaynak indiriliyor...')
    with ThreadPoolExecutor(12) as ex:
        paths = dict(zip(urls, ex.map(fetch, urls)))
    durs = {}
    for u, p in paths.items():
        if p and re.search(r'\.(mp3|wav|m4a|mp4|mov|webm|aac|ogg)$', p, re.I) or (p and probe_duration(p) > 0 and not re.search(r'\.(jpg|jpeg|png|webp|gif)$', p, re.I)):
            durs[u] = probe_duration(p)
    # 2) sahne süreleri ve başlangıçları
    plan, t = [], 0.0
    for i, sc in enumerate(scenes):
        els = sc.get('elements', [])
        end = 0.0
        for el in els:
            st = float(el.get('start', 0) or 0)
            d = el.get('duration')
            if el.get('type') in ('audio', 'video') and el.get('src') in durs:
                if d is None or float(d) == -1: end = max(end, st + durs[el['src']])
                elif float(d) > 0: end = max(end, st + float(d))
            elif d is not None and float(d) > 0:
                end = max(end, st + float(d))
        if sc.get('duration') and float(sc['duration']) > 0: end = float(sc['duration'])
        if end <= 0: end = 5.0
        tr = sc.get('transition') or {}
        td = float(tr.get('duration', 0) or 0) if i > 0 and tr.get('style') else 0.0
        td = min(td, end / 2)
        start = max(0.0, t - td)
        plan.append(dict(sc=sc, start=start, dur=end, td=td))
        t = start + end
    total = t
    log(f'toplam süre {total:.1f} sn')
    return W, H, plan, paths, durs, total

def mix_audio(plan, paths, durs, total):
    mix = np.zeros((int(total * SR) + SR, 2), np.float32)
    for s in plan:
        for el in s['sc'].get('elements', []):
            typ = el.get('type')
            vol = float(el.get('volume', 1) if el.get('volume') is not None else 1)
            if typ not in ('audio', 'video') or vol <= 0: continue
            p = paths.get(el.get('src'))
            if not p or (typ == 'video' and not has_audio(p)): continue
            a = load_audio(p, vol)
            st = s['start'] + float(el.get('start', 0) or 0)
            d = el.get('duration')
            maxlen = s['dur'] - float(el.get('start', 0) or 0)
            if d is not None and float(d) > 0: maxlen = min(maxlen, float(d))
            a = a[:int(maxlen * SR)]
            i0 = int(st * SR); i1 = min(len(mix), i0 + len(a))
            mix[i0:i1] += a[:i1 - i0]
    mix = mix[:int(total * SR)]
    peak = np.abs(mix).max()
    if peak > 0.99: mix = np.tanh(mix / peak * 1.2) * 0.97
    return mix

class SceneRT:
    """Bir sahnenin çalışma zamanı nesneleri (sahne başlayınca açılır, bitince kapanır)."""
    def __init__(self, s, paths, durs, W, H):
        self.s, self.W, self.H = s, W, H
        sc = s['sc']
        self.bg = np.array(color(sc.get('background-color', '#000000'), (0, 0, 0, 255))[:3], np.float32)
        self.items = []
        for el in sc.get('elements', []):
            typ = el.get('type')
            st = float(el.get('start', 0) or 0)
            d = el.get('duration')
            if typ == 'video':
                p = paths.get(el.get('src'))
                if not p: continue
                if d is None or float(d) == -1: en = st + durs.get(el['src'], s['dur'])
                elif float(d) == -2: en = s['dur']
                else: en = st + float(d)
                if el.get('position') == 'custom':
                    vw, vh, vx, vy = int(el['width']), int(el['height']), int(el.get('x', 0)), int(el.get('y', 0))
                    rd = VideoReader(p, vw, vh, loop=True, cover=False)
                else:
                    vx, vy = 0, 0; rd = VideoReader(p, W, H, loop=True, cover=True)
                self.items.append(dict(kind='video', st=st, en=min(en, s['dur']), rd=rd, x=vx, y=vy, fade=float(el.get('fade-in', 0) or 0)))
            elif typ == 'image':
                p = paths.get(el.get('src'))
                if not p: continue
                try: obj = ImageEl(el, p, W, H)
                except Exception as e: log('  görsel açılamadı', e); continue
                en = s['dur'] if d is None or float(d) < 0 else st + float(d)
                self.items.append(dict(kind='image', st=st, en=min(en, s['dur']), obj=obj, fade=float(el.get('fade-in', 0) or 0)))
            elif typ == 'text':
                lay = Layer(render_text(el, W, H))
                en = s['dur'] if d is None or float(d) < 0 else st + float(d)
                self.items.append(dict(kind='text', st=st, en=min(en, s['dur']), lay=lay, fade=float(el.get('fade-in', 0) or 0)))
    def render(self, u):
        frame = np.empty((self.H, self.W, 3), np.float32); frame[:] = self.bg
        for it in self.items:
            if not (it['st'] <= u < it['en'] or (u >= it['en'] and it['en'] >= self.s['dur'] - 1e-6)): continue
            local = u - it['st']
            op = min(1.0, local / it['fade']) if it['fade'] > 0 else 1.0
            if it['kind'] == 'video':
                img = it['rd'].frame_at(int(local * FPS))
                paste_rgb(frame, img, it['x'], it['y'], op)
            elif it['kind'] == 'image':
                span = max(0.01, it['en'] - it['st'])
                it['obj'].draw(frame, local / span, op)
            else:
                blend_layer(frame, it['lay'], op)
        return frame
    def close(self):
        for it in self.items:
            if it['kind'] == 'video': it['rd'].close()

def render(movie, out):
    W, H, plan, paths, durs, total = build(movie)
    mix = mix_audio(plan, paths, durs, total)
    wav = out + '.f32'
    mix.astype(np.float32).tofile(wav)
    kbps = int(min(6000, max(900, MAX_MB * 8 * 1024 / max(total, 1) - 140)))
    log(f'video bit hızı {kbps} kbps')
    enc = subprocess.Popen(['ffmpeg', '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', str(FPS), '-i', '-',
                            '-f', 'f32le', '-ar', str(SR), '-ac', '2', '-i', wav,
                            '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', f'{kbps}k', '-maxrate', f'{int(kbps * 1.3)}k', '-bufsize', f'{kbps * 2}k',
                            '-pix_fmt', 'yuv420p', '-af', 'loudnorm=I=-14:TP=-1.5:LRA=11', '-ar', '44100', '-c:a', 'aac', '-b:a', '128k', '-shortest', '-movflags', '+faststart', out],
                           stdin=subprocess.PIPE)
    active = {}
    nfr = int(total * FPS)
    for fi in range(nfr):
        t = fi / FPS
        idx = [k for k, s in enumerate(plan) if s['start'] <= t < s['start'] + s['dur']]
        for k in list(active):
            if k not in idx: active.pop(k).close()
        for k in idx:
            if k not in active: active[k] = SceneRT(plan[k], paths, durs, W, H)
        if not idx:
            frame = np.zeros((H, W, 3), np.float32)
        else:
            k = idx[-1]; s = plan[k]
            frame = active[k].render(t - s['start'])
            if len(idx) > 1 and s['td'] > 0:
                a = min(1.0, (t - s['start']) / s['td'])
                prev = active[idx[-2]].render(t - plan[idx[-2]]['start'])
                frame = frame * a + prev * (1 - a)
        enc.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
        if fi % (FPS * 20) == 0: log(f'  {t:.0f}/{total:.0f} sn')
    for v in active.values(): v.close()
    enc.stdin.close(); enc.wait()
    os.remove(wav)
    if enc.returncode != 0: raise RuntimeError('ffmpeg kodlama hatası')
    log(f'BİTTİ {out} {os.path.getsize(out) / 1e6:.1f} MB')
    return total

if __name__ == '__main__':
    job = json.load(open(sys.argv[1], encoding='utf-8'))
    movie = job.get('movie', job)
    render(movie, sys.argv[2])
