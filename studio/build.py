"""Сборка фильма Lumio из сцен и переходов + фирменная концовка.

    python3 studio/build.py film.json out_video.mp4 [--draft]

film.json:
{
  "dir": "/путь/к/папке/с/клипами",
  "clips": [
    {"file": "s1.mp4", "kind": "scene"},
    {"file": "t1.mp4", "kind": "trans"},            # переход Kling: конец разгоняется под темп сцены
    {"file": "s2.mp4", "kind": "scene"},
    {"file": "t2.mp4", "kind": "trans", "retime": false}   # если персонаж в конце стоит — не разгонять
  ],
  "pre_outro_title": "С Днём матери",      # необязательно: огоньки -> звезда + надпись перед концовкой
  "outro": "studio/assets/lumio_outro.mp4"  # фирменная концовка (огонёк -> логотип -> «Мультфильм по вашей истории»)
}

Правила стыков (см. навык lumio-cartoon-pipeline):
- первый кадр каждого клипа повторяет последний кадр предыдущего — выкидываем его;
- на стыке короткий наплыв 4–6 кадров (кадры почти одинаковые);
- хвост перехода Kling разгоняется ПОТОКОМ (не смешиванием кадров), чтобы не было «стоп — рывок».
Рядом с видео пишется out_video.mp4.json с таймингами (для музыки и голоса).
"""
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lumio_lib as L  # noqa: E402

cfg = json.load(open(sys.argv[1]))
OUT = sys.argv[2]
DRAFT = "--draft" in sys.argv
D = cfg.get("dir", ".")
w = L.Writer(OUT, crf=23 if DRAFT else 16, preset="veryfast" if DRAFT else "slow")
XF_IN, XF_OUT = 6, 4      # кадров наплыва: переход->сцена, сцена->переход
marks, pending, last_frames = [], [], None
clips = cfg["clips"]

for i, c in enumerate(clips):
    fr = L.read_all(os.path.join(D, c["file"]))
    if i > 0:
        fr = fr[1:]
    start = w.n - len(pending) // 2
    marks.append((round(start / L.FPS, 3), c["kind"], c["file"]))
    n = len(pending)
    for j in range(n):
        u = L.smooth((j + 1) / (n + 1))
        w.emit(L.F32(pending[j]) * (1 - u) + L.F32(fr[j]) * u)
    fr = fr[n:]
    if c["kind"] == "trans" and c.get("retime", True) and i + 1 < len(clips):
        nxt = L.read_all(os.path.join(D, clips[i + 1]["file"]))[1:9]
        r = min(2.4, max(1.0, L.motion(nxt) / max(0.3, L.motion(fr[-8:]))))
        fr = L.retime_tail(fr, r, int(1.2 * L.FPS))
        print(f"  {c['file']}: хвост ускорен x{r:.2f}", flush=True)
    last = i == len(clips) - 1
    hold = 0 if last else (XF_IN if c["kind"] != "scene" else XF_OUT)
    body, pending = (fr, []) if hold == 0 else (fr[:-hold], fr[-hold:])
    for f in body:
        w.emit(f)
    last_frames = fr
    print(c["file"], "->", round(w.t, 2), "с", flush=True)

# ---------------------------------------------------------------- финал
bg = L.night_bg()
last = last_frames[-1]
for j in range(L.FPS):                       # вспышка света из последней сцены в ночное небо
    k = (j + 0.5) / L.FPS
    a = L.bloom(last, (L.W / 2, L.H / 2), L.smooth(k * 1.6))
    x = L.smooth((k - 0.45) / 0.55)
    w.emit(a * (1 - x) + bg * x)
fin = w.t

outro = L.read_all(cfg.get("outro", os.path.join(os.path.dirname(__file__), "assets", "lumio_outro.mp4")))
title = cfg.get("pre_outro_title")
ORB = (959, 539)
if title:
    lights = [(160, 200), (1760, 160), (300, 900), (1650, 920), (960, 1040), (80, 560), (1860, 560)]
    lights = [(float(a), float(b)) for a, b in lights]
    layer = L.text_layer([(title, 130, 540)])
    STAR_COL = np.array([200, 235, 255], np.float32) / 255
    HALO = np.array([110, 190, 255], np.float32) / 255
    XF0, XF1 = 6.0, 6.8
    for j in range(int((XF0 + len(outro) / L.FPS) * L.FPS)):
        t = j / L.FPS
        if t >= XF1:
            w.emit(outro[min(len(outro) - 1, j - int(XF0 * L.FPS))])
            continue
        mv = L.smooth((t - 5.0) / 1.2)
        pos = (960 + (ORB[0] - 960) * mv, 420 + (ORB[1] - 420) * mv)
        halo, core = L.star(min(1, t / 2.6), L.smooth((t - 2.3) / 0.8) * (1 + 0.06 * math.sin(t * 3)),
                            lights if t < 2.7 else [], pos)
        f = 1 - (1 - bg) * (1 - halo * 0.85 * HALO)
        f = f * (1 - core) + STAR_COL * core
        f = L.over(f, layer, L.smooth((t - 3.0) / 0.9) * (1 - L.smooth((t - 4.9) / 0.8)))
        x = L.smooth((t - XF0) / (XF1 - XF0))
        if x > 0:
            f = L.F32(f) * (1 - x) + L.F32(outro[j - int(XF0 * L.FPS)]) * x
        w.emit(f)
    logo_at = fin + XF0 + 0.9
else:
    for f in outro:
        w.emit(f)
    logo_at = fin + 0.9
w.close()
info = {"marks": marks, "finale": round(fin, 3), "logo_burst": round(logo_at, 3), "total": round(w.t, 3)}
json.dump(info, open(OUT + ".json", "w"), ensure_ascii=False, indent=1)
print(json.dumps(info, ensure_ascii=False))
