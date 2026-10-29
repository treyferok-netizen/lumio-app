"""Seedream (BytePlus ModelArk) из Telegram: генерация кадров на сервере, результат — вам в чат.

Только для администратора:
  1) пришлите боту 1–3 картинки-референса (фото или файлом) — по порядку: image 1, image 2, image 3;
  2) /img 3      — промт шага 3 из bot/img_prompts.json (пакет промтов фильма)
     /img текст  — свой промт
     /img        — список шагов
     /img_clear  — сбросить референсы
Бот присылает 2 варианта файлами (полное качество, без водяного знака).

Настройки (lumio-set ИМЯ ЗНАЧЕНИЕ на сервере):
  SEEDREAM_API_KEY      ключ из консоли BytePlus ModelArk (в чат не присылать)
  SEEDREAM_MODEL        id модели или endpoint из консоли, по умолчанию seedream-5-0-pro-260628
  SEEDREAM_BASE         по умолчанию https://ark.ap-southeast.bytepluses.com/api/v3
  SEEDREAM_SIZE         по умолчанию 2560x1440 (16:9)
  SEEDREAM_VARIANTS     сколько вариантов за раз, по умолчанию 2
  SEEDREAM_DAILY_LIMIT  максимум картинок в сутки, по умолчанию 30
"""
import base64
import json
import os
import threading
import time

import requests

KEY = os.environ.get("SEEDREAM_API_KEY", "").strip()
MODEL = os.environ.get("SEEDREAM_MODEL", "seedream-5-0-pro-260628").strip()
BASE = os.environ.get("SEEDREAM_BASE", "https://ark.ap-southeast.bytepluses.com/api/v3").rstrip("/")
SIZE = os.environ.get("SEEDREAM_SIZE", "2560x1440")
VARIANTS = max(1, min(4, int(os.environ.get("SEEDREAM_VARIANTS", "2"))))
DAILY = int(os.environ.get("SEEDREAM_DAILY_LIMIT", "30"))

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.dirname(os.environ.get("STATE_FILE", "/var/lib/lumio-bot/state.json")) or "."
COUNT_FILE = os.path.join(DIR, "seedream_count.json")
_lock = threading.Lock()
_refs = []          # [(bytes, mime, time)] — референсы администратора
REF_TTL = 3600      # референсы живут час


def ready():
    return bool(KEY)


def steps():
    try:
        with open(os.path.join(HERE, "img_prompts.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"film": "", "steps": {}}


def add_ref(data, mime="image/jpeg"):
    with _lock:
        now = time.time()
        _refs[:] = [r for r in _refs if now - r[2] < REF_TTL][-9:]
        _refs.append((data, mime, now))
        return len(_refs)


def clear_refs():
    with _lock:
        _refs.clear()


def refs_count():
    with _lock:
        now = time.time()
        return len([r for r in _refs if now - r[2] < REF_TTL])


def _used_today():
    day = time.strftime("%Y-%m-%d")
    try:
        with open(COUNT_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return day, d.get(day, 0)


def _add_used(n):
    with _lock:
        day, used = _used_today()
        os.makedirs(DIR, exist_ok=True)
        with open(COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({day: used + n}, f)


def left_today():
    return max(0, DAILY - _used_today()[1])


def _one(prompt, images):
    body = {"model": MODEL, "prompt": prompt, "size": SIZE, "response_format": "b64_json",
            "output_format": "jpeg", "watermark": False}
    if images:
        body["image"] = images if len(images) > 1 else images[0]
    r = requests.post(BASE + "/images/generations", json=body, timeout=(10, 180),
                      headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code != 200 or not j.get("data"):
        err = (j.get("error") or {}) if isinstance(j.get("error"), dict) else {"message": j.get("error") or r.text[:300]}
        raise RuntimeError(f"HTTP {r.status_code}: {err.get('code', '')} {err.get('message', '')}".strip())
    return base64.b64decode(j["data"][0]["b64_json"])


def generate(prompt, on_done, on_error):
    """В отдельном потоке: VARIANTS запросов параллельно; on_done(list[bytes]), on_error(str)."""
    with _lock:
        now = time.time()
        imgs = ["data:%s;base64,%s" % (m, base64.b64encode(d).decode()) for d, m, t in _refs if now - t < REF_TTL]
    n = min(VARIANTS, left_today())
    if n <= 0:
        on_error(f"Дневной лимит {DAILY} картинок исчерпан. Изменить: lumio-set SEEDREAM_DAILY_LIMIT …")
        return

    def run():
        out, errs = [None] * n, []

        def job(i):
            try:
                out[i] = _one(prompt, imgs)
            except Exception as e:  # сеть, ключ, модерация
                errs.append(str(e)[:300])
        ts = [threading.Thread(target=job, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        good = [x for x in out if x]
        if good:
            _add_used(len(good))
            on_done(good)
        if errs:
            on_error("Seedream ответил ошибкой: " + errs[0])
    threading.Thread(target=run, daemon=True).start()
