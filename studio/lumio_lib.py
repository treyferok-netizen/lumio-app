"""Общие функции монтажа Lumio: чтение клипов, дорисовка кадров по движению, свет, текст, проверки стыков.

Все скрипты студии импортируют этот модуль. Кадры храним в uint8 (float на 1080p быстро съедает память).
"""
import math
import subprocess

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 24
LORA = "/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf"

# ----------------------------------------------------------------- видео I/O
def read_all(path):
    cap = cv2.VideoCapture(path)
    out = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        if f.shape[1] != W or f.shape[0] != H:
            f = cv2.resize(f, (W, H), interpolation=cv2.INTER_AREA)
        out.append(f)
    return out


class Writer:
    """Пишет кадры (uint8 BGR или float 0..1) в mp4 через ffmpeg."""

    def __init__(self, out, crf=16, preset="slow", fps=FPS):
        self.p = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(fps),
             "-i", "-", "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", out],
            stdin=subprocess.PIPE)
        self.n = 0

    def emit(self, f):
        if f.dtype != np.uint8:
            f = (np.clip(f, 0, 1) * 255).astype(np.uint8)
        self.p.stdin.write(f.tobytes())
        self.n += 1

    @property
    def t(self):
        return self.n / FPS

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def F32(f):
    return f.astype(np.float32) / 255 if f.dtype == np.uint8 else f


def smooth(t):
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


# ------------------------------------------------- дорисовка кадров по движению
_dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
_gx, _gy = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))


def _flows(a, b):
    sa = cv2.cvtColor(cv2.resize(a, (W // 2, H // 2)), cv2.COLOR_BGR2GRAY)
    sb = cv2.cvtColor(cv2.resize(b, (W // 2, H // 2)), cv2.COLOR_BGR2GRAY)
    return cv2.resize(_dis.calc(sa, sb, None), (W, H)) * 2, cv2.resize(_dis.calc(sb, sa, None), (W, H)) * 2


def flow_mid(a, b, u, sharp=0.5):
    """Промежуточный кадр между a и b (u=0..1) по оптическому потоку, без двоения.
    sharp возвращает резкость дорисованным кадрам — иначе каждый «настоящий» кадр резче и виден пульс."""
    f01, f10 = _flows(a, b)
    w0 = cv2.remap(a, _gx - u * f01[..., 0], _gy - u * f01[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    w1 = cv2.remap(b, _gx - (1 - u) * f10[..., 0], _gy - (1 - u) * f10[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    mix = cv2.addWeighted(w0, 1 - u, w1, u, 0)
    amt = sharp * 4 * u * (1 - u)
    if amt > 0:
        soft = cv2.GaussianBlur(mix, (0, 0), 1.2)
        mix = cv2.addWeighted(mix, 1 + amt, soft, -amt, 0)
    return mix


def morph(a, b, u):
    """Наплыв между двумя похожими, но разными кадрами: оба тянутся друг к другу по потоку."""
    a = a if a.dtype == np.uint8 else (np.clip(a, 0, 1) * 255).astype(np.uint8)
    b = b if b.dtype == np.uint8 else (np.clip(b, 0, 1) * 255).astype(np.uint8)
    return flow_mid(a, b, u, sharp=0.0)


def motion(frs):
    """Средняя скорость движения (разница соседних кадров на 480x270)."""
    s = [cv2.resize(f, (480, 270)).astype(np.int16) for f in frs]
    return float(np.mean([np.abs(s[k + 1] - s[k]).mean() for k in range(len(s) - 1)]))


def retime_tail(frs, r, L):
    """Kling тормозит к последнему кадру. Разгоняем последние L кадров от 1x до r x,
    чтобы движение влилось в следующую сцену. Только поток, никаких смешанных кадров."""
    if r <= 1.05:
        return frs
    head, tail = frs[:-L], frs[-L:]
    out, pos = [], 0.0
    while pos < L - 1:
        p = round(pos, 6)
        i = int(p)
        u = p - i
        out.append(tail[i] if u < 1e-3 else flow_mid(tail[i], tail[i + 1], u))
        pos += 1 + (r - 1) * (pos / (L - 1)) ** 1.5
    out.append(tail[-1])
    return head + out


# ----------------------------------------------------------- свет и титры
def bloom(frame, c, k):
    """Золотой свет из точки c; k=0..1 (1 — почти весь экран в свете)."""
    yy, xx = _gy, _gx
    frame = F32(frame)
    gold = np.array([150, 215, 255], np.float32) / 255
    cream = np.array([225, 245, 255], np.float32) / 255
    r = 60 + k ** 1.6 * 2600
    g = np.exp(-(np.sqrt((xx - c[0]) ** 2 + (yy - c[1]) ** 2) / r) ** 2)[..., None]
    col = gold * (1 - k) + cream * k
    a = np.clip(g * (0.35 + 0.65 * k) * 1.15, 0, 1) * min(1, k * 3)
    out = 1 - (1 - frame) * (1 - a * col)
    return out * (1 - 0.55 * k * g) + col * (0.55 * k * g)


def night_bg():
    """Тёмно-синий фон, как у фирменной концовки."""
    small = np.zeros((108, 192, 3), np.float32)
    center, edge = np.array([62, 25, 26], np.float32), np.array([32, 10, 9], np.float32)
    for y in range(108):
        for x in range(192):
            d = math.hypot((x - 96) / 96, (y - 54) / 54) / math.sqrt(2)
            small[y, x] = center + (edge - center) * min(1.0, d ** 1.2)
    return cv2.GaussianBlur(cv2.resize(small, (W, H), interpolation=cv2.INTER_CUBIC), (0, 0), 8) / 255


def star(t_gather, k_star, lights, pos, R=62):
    """Огоньки слетаются в pos, затем там загорается четырёхлучевая звезда. Возвращает (halo, core)."""
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    e = smooth(t_gather)
    for sx, sy in lights:
        x = sx + (pos[0] - sx) * e
        y = sy + (pos[1] - sy) * e + math.sin(t_gather * 6 + sx) * 30 * (1 - e)
        r = 8 * (1 - 0.4 * e)
        d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    if k_star > 0:
        rr = R * k_star
        d.polygon([(pos[0] + rr * math.cos(t) ** 3, pos[1] + rr * math.sin(t) ** 3) for t in np.linspace(0, 2 * math.pi, 240)], fill=255)
    core = cv2.GaussianBlur(np.asarray(img).astype(np.float32) / 255, (0, 0), 1.5)
    halo = np.clip(cv2.GaussianBlur(core, (0, 0), 45) * 3.0 + cv2.GaussianBlur(core, (0, 0), 14) * 1.4, 0, 1)
    return halo[..., None], core[..., None]


def text_layer(lines):
    """lines: [(текст, размер, y)] — золотой текст Lora по центру с мягким свечением. -> (rgb, alpha)"""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for txt, size, y in lines:
        f = ImageFont.truetype(LORA, size)
        try:
            f.set_variation_by_name("SemiBold")
        except Exception:
            pass
        bb = ImageDraw.Draw(img).textbbox((0, 0), txt, font=f)
        x = (W - (bb[2] - bb[0])) / 2 - bb[0]
        g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((x, y), txt, font=f, fill=(255, 190, 90, 160))
        img = Image.alpha_composite(img, g.filter(ImageFilter.GaussianBlur(16)))
        ImageDraw.Draw(img).text((x, y), txt, font=f, fill=(255, 232, 180, 255))
    a = np.asarray(img).astype(np.float32) / 255
    return a[..., [2, 1, 0]], a[..., 3:4]


def over(frame, layer, alpha):
    rgb, a = layer
    a = a * alpha
    return F32(frame) * (1 - a) + rgb * a


# ------------------------------------------------------------- проверки
def sharpness(f):
    return cv2.Laplacian(cv2.cvtColor(cv2.resize(f, (960, 540)), cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
