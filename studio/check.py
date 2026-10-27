"""Быстрые проверки присланных файлов.

    python3 studio/check.py image  frame.jpg            # обрезан ли JPEG (серая полоса) -> чинит в frame_fixed.jpg
    python3 studio/check.py clip   clip.mp4 [prev_last.jpg next_first.jpg]
                                                         # длительность, лист кадров, водяной знак, внутренние склейки,
                                                         # совпадение концов с соседями
    python3 studio/check.py joins  film.mp4 film.mp4.json   # рывки и мерцание резкости на каждом стыке
"""
import json
import os
import subprocess
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lumio_lib as L  # noqa: E402

mode = sys.argv[1]
small = lambda f: cv2.resize(f, (480, 270)).astype(np.int16)  # noqa: E731

if mode == "image":
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    p = sys.argv[2]
    im = Image.open(p).convert("RGB")
    a = np.asarray(im).astype(int)
    Wd, Hd = im.size
    cut = Hd
    for y in range(Hd):
        for part in (a[y, :Wd // 4], a[y, -Wd // 4:]):
            if part.std() < 2 and abs(part.mean() - 128) < 12:
                cut = min(cut, y)
    if cut == Hd:
        print("OK, не обрезан", im.size)
    else:
        h = cut - 16
        w = int(h * 16 / 9)
        x = (Wd - w) // 2
        out = os.path.splitext(p)[0] + "_fixed.jpg"
        im.crop((x, 0, x + w, h)).resize((2560, 1440), Image.LANCZOS).save(out, quality=95)
        print(f"ОБРЕЗАН с y={cut}; исправлено -> {out}")

elif mode == "clip":
    p = sys.argv[2]
    fr = L.read_all(p)
    print("кадров", len(fr), "длительность", round(len(fr) / L.FPS, 2), "с")
    d = np.array([np.abs(small(fr[k + 1]) - small(fr[k])).mean() for k in range(len(fr) - 1)])
    med = np.median(d)
    cuts = [(round((k + 1) / L.FPS, 2), round(v, 1)) for k, v in enumerate(d) if v > max(15, 4 * med)]
    print("внутренние склейки:", cuts or "нет")
    corner = fr[len(fr) // 2][920:1080, 1520:1920]
    cv2.imwrite(os.path.splitext(p)[0] + "_corner.jpg", corner)
    print("угол для проверки водяного знака:", os.path.splitext(p)[0] + "_corner.jpg")
    idx = np.linspace(0, len(fr) - 1, 12).astype(int)
    sheet = np.vstack([np.hstack([cv2.resize(fr[i], (480, 270)) for i in idx[r * 4:(r + 1) * 4]]) for r in range(3)])
    cv2.imwrite(os.path.splitext(p)[0] + "_sheet.jpg", sheet)
    if len(sys.argv) > 4:
        a, b = cv2.imread(sys.argv[3]), cv2.imread(sys.argv[4])
        print("начало vs кадр A:", round(np.abs(small(fr[0]) - small(a)).mean(), 2),
              "| конец vs кадр B:", round(np.abs(small(fr[-1]) - small(b)).mean(), 2), "(норма < 3)")

elif mode == "joins":
    fr = L.read_all(sys.argv[2])
    info = json.load(open(sys.argv[3]))
    d = np.array([np.abs(small(fr[k + 1]) - small(fr[k])).mean() for k in range(len(fr) - 1)])
    sh = np.array([L.sharpness(f) for f in fr])
    for t, kind, name in info["marks"][1:]:
        i = int(t * L.FPS)
        win = d[max(0, i - 24):i + 24]
        s = sh[max(0, i - 24):i + 24]
        jit = np.abs(np.diff(s)).mean() / s.mean()
        flag = "  <-- проверь" if (win.max() > 2.5 * np.median(win) + 1 or jit > 0.08) else ""
        print(f"{t:7.2f} {name:22s} скачок {win.max():5.1f} (медиана {np.median(win):4.1f}) "
              f"мерцание резкости {jit:.3f}{flag}")
