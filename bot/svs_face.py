"""Сохранение лица при примерке: FASHN иногда «перерисовывает» лицо. После примерки возвращаем на результат
голову с исходного фото покупателя — поза и фон при примерке не меняются, поэтому голова встаёт на место.

restore(original_bytes, result_bytes) -> bytes JPEG (или result_bytes без изменений, если вернуть нельзя).
Отключить: lumio-set SVS_FACE_RESTORE 0
"""
import os

import cv2
import numpy as np

ENABLED = os.environ.get("SVS_FACE_RESTORE", "1").strip() not in ("0", "no", "false", "")
_cascade = None


CASCADE = "haarcascade_frontalface_default.xml"
CASCADE_URL = "https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/" + CASCADE


def _cascade_path():
    """Файл детектора лиц: из OpenCV, а если его нет (в OpenCV 5 не кладут) — один раз скачиваем с GitHub."""
    p = os.path.join(getattr(getattr(cv2, "data", None), "haarcascades", ""), CASCADE)
    if os.path.isfile(p):
        return p
    import requests
    import seedream
    p = os.path.join(seedream.DIR, CASCADE)
    if not os.path.isfile(p):
        r = requests.get(CASCADE_URL, timeout=(10, 60))
        r.raise_for_status()
        with open(p + ".tmp", "wb") as f:
            f.write(r.content)
        os.replace(p + ".tmp", p)
    return p


def _faces(gray):
    global _cascade
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(_cascade_path())
    h = gray.shape[0]
    found = _cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=6, minSize=(max(24, h // 40),) * 2)
    return sorted(found, key=lambda f: -f[2] * f[3])


def match_aspect(original, result):
    """FASHN приводит кадр к 3:4 растяжением, а не обрезкой, поэтому человек получается чуть вытянутым или
    приплюснутым. Возвращаем результату пропорции исходного фото (сжимаем/растягиваем по ширине)."""
    try:
        src = cv2.imdecode(np.frombuffer(original, np.uint8), cv2.IMREAD_COLOR)
        dst = cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR)
        if src is None or dst is None:
            return result
        sh, sw = src.shape[:2]
        dh, dw = dst.shape[:2]
        want_w = int(round(dh * sw / sh))
        if abs(want_w - dw) <= 2 or not (0.5 < want_w / dw < 2.0):
            return result
        out = cv2.resize(dst, (want_w, dh), interpolation=cv2.INTER_LANCZOS4 if want_w > dw else cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 93])
        print(f"svs aspect: {dw}x{dh} -> {want_w}x{dh}", flush=True)
        return buf.tobytes() if ok else result
    except Exception as e:
        print("svs aspect error", repr(e)[:200], flush=True)
        return result


def restore(original, result):
    if not ENABLED:
        return result
    try:
        src = cv2.imdecode(np.frombuffer(original, np.uint8), cv2.IMREAD_COLOR)
        dst = cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR)
        if src is None or dst is None:
            return result
        sh, sw = src.shape[:2]
        dh, dw = dst.shape[:2]
        if abs(sw / sh - dw / dh) > 0.03:          # кадр обрезан иначе — не рискуем
            print("svs face: aspect differs, skip", flush=True)
            return result
        src = cv2.resize(src, (dw, dh), interpolation=cv2.INTER_AREA)
        g_src = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
        faces = _faces(g_src)
        if not len(faces):
            print("svs face: no face found, skip", flush=True)
            return result
        x, y, w, h = faces[0]
        # голова с волосами, без шеи (на шее может быть новый воротник)
        x0, x1 = max(0, int(x - 0.28 * w)), min(dw, int(x + 1.28 * w))
        y0, y1 = max(0, int(y - 0.55 * h)), min(dh, int(y + 1.08 * h))
        patch = src[y0:y1, x0:x1]
        # где голова на результате: ищем в окрестности (лёгкий сдвиг после примерки)
        g_dst = cv2.cvtColor(dst, cv2.COLOR_BGR2GRAY)
        pad = int(0.25 * max(w, h))
        sx0, sy0 = max(0, x0 - pad), max(0, y0 - pad)
        sx1, sy1 = min(dw, x1 + pad), min(dh, y1 + pad)
        face_tpl = g_src[y:y + h, x:x + w]
        res = cv2.matchTemplate(g_dst[sy0:sy1, sx0:sx1], face_tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score < 0.35:                            # поза/кадр изменились сильно — вклеивать опасно
            print(f"svs face: match {score:.2f} too low, skip", flush=True)
            return result
        dx, dy = sx0 + loc[0] - x, sy0 + loc[1] - y
        tx0, ty0 = x0 + dx, y0 + dy
        if tx0 < 0 or ty0 < 0 or tx0 + patch.shape[1] > dw or ty0 + patch.shape[0] > dh:
            return result
        mask = np.zeros(patch.shape[:2], np.uint8)
        ph, pw = mask.shape
        cv2.ellipse(mask, (pw // 2, int(ph * 0.47)), (int(pw * 0.44), int(ph * 0.46)), 0, 0, 360, 255, -1)
        # мягкий край: исходное лицо внутри, плавный переход к результату по краю овала
        k = max(3, int(min(pw, ph) * 0.12) | 1)
        alpha = cv2.GaussianBlur(mask, (k, k), 0).astype(np.float32)[..., None] / 255.0
        out = dst.copy()
        roi = out[ty0:ty0 + ph, tx0:tx0 + pw].astype(np.float32)
        out[ty0:ty0 + ph, tx0:tx0 + pw] = (patch.astype(np.float32) * alpha + roi * (1 - alpha)).astype(np.uint8)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"svs face: restored (match {score:.2f}, shift {dx},{dy})", flush=True)
        return buf.tobytes() if ok else result
    except Exception as e:
        print("svs face error", repr(e)[:200], flush=True)
        return result


def check_photo(raw):
    """Подсказка по фото покупателя (или None, если всё хорошо). Фото всё равно сохраняется."""
    try:
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return "Не получилось прочитать фото — попробуйте другое."
        h, w = img.shape[:2]
        if min(h, w) < 500:
            return "Фото маленькое — примерка выйдет нечёткой. Лучше загрузить фото в обычном качестве."
        if w > h * 1.05:
            return "Фото горизонтальное — для примерки лучше вертикальное фото в полный рост."
        faces = _faces(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        big = [f for f in faces if f[3] > h * 0.03]
        if len(big) > 1 and big[1][3] > big[0][3] * 0.6:
            return "Похоже, на фото несколько человек — для примерки нужен только вы."
        if not big:
            return "Не видно лица — встаньте лицом к камере, так примерка получится точнее."
        if big[0][3] / h > 0.16:
            return "Похоже, фото не в полный рост. Чтобы примерять брюки и обувь, нужно фото с головы до ног."
        return None
    except Exception as e:
        print("svs check_photo error", repr(e)[:200], flush=True)
        return None
