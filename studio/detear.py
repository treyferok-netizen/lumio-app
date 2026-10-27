"""Убрать дорожку слезы со щеки (на ресницах остаётся маленькая капля).

    python3 studio/detear.py in.mp4 out.mp4 t_from t_to X0 X1 Y0 Y1 [keep_top_px]

X0..Y1 — рамка вокруг щеки в координатах 1920x1080 (верх рамки — сразу под ресницами).
Рамка НЕ должна заходить на подбородок, губы и шею: края маски обнуляются на 8–10 px,
иначе на подбородке появляются «квадратики». После обработки проверь крупные кропы лица.
Для неподвижного кадра (стартовый кадр перехода) удобно сначала сделать 1-кадровое видео
или вызвать clean() из Python.
"""
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lumio_lib as L  # noqa: E402

K = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))


def clean(f, box, keep_top=28):
    X0, X1, Y0, Y1 = box
    for thr in (12, 10):
        roi = f[Y0:Y1, X0:X1].copy()
        th = cv2.morphologyEx(cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)[..., 0], cv2.MORPH_TOPHAT, K)
        m = (th > thr).astype(np.uint8) * 255
        m[:keep_top] = 0
        m[:, :8] = 0
        m[:, -8:] = 0
        m[-10:] = 0
        m = cv2.dilate(m, np.ones((7, 7), np.uint8))
        fixed = cv2.inpaint(roi, m, 7, cv2.INPAINT_TELEA)
        a = cv2.GaussianBlur(m, (0, 0), 3).astype(np.float32)[..., None] / 255
        f[Y0:Y1, X0:X1] = (roi * (1 - a) + fixed * a).astype(np.uint8)
    return f


if __name__ == "__main__":
    src, out, t0, t1 = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    box = tuple(int(v) for v in sys.argv[5:9])
    keep = int(sys.argv[9]) if len(sys.argv) > 9 else 28
    fr = L.read_all(src)
    w = L.Writer(out, crf=14)
    for n, f in enumerate(fr):
        w.emit(clean(f, box, keep) if t0 <= n / L.FPS <= t1 else f)
    w.close()
    print("готово", w.n, "кадров")
