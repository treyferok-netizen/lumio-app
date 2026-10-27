"""Замедление части клипа с дорисовкой кадров по движению (например, раскачивающийся шаг малыша).

    python3 studio/slowmo.py in.mp4 out.mp4 t_start t_end speed [noin] [noout]

speed 0.75 = замедлить до 0.75x. Края по 0.4 с плавные; noin/noout — без плавного края
(нужно, когда замедляешь конец перехода и начало сцены: темп по обе стороны стыка должен быть ОДИНАКОВЫМ).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lumio_lib as L  # noqa: E402

src, out, ta, tb, speed = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
ease_in, ease_out = "noin" not in sys.argv[6:], "noout" not in sys.argv[6:]
fr = L.read_all(src)


def rate(ts):
    if ts < ta or ts > tb:
        return 1.0
    k = min(1.0, (ts - ta) / 0.4 if ease_in else 1.0, (tb - ts) / 0.4 if ease_out else 1.0)
    return 1.0 + (speed - 1.0) * L.smooth(k)


w = L.Writer(out, crf=14)
t = 0.0
while t <= len(fr) - 1 + 1e-6:
    p = round(t, 6)
    i = int(p)
    u = p - i
    w.emit(fr[min(i, len(fr) - 1)] if (u < 1e-3 or i >= len(fr) - 1) else L.flow_mid(fr[i], fr[i + 1], u))
    t += rate(t / L.FPS)
w.close()
print("кадров было", len(fr), "стало", w.n)
