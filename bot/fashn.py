"""FASHN API — виртуальная примерка одежды (https://docs.fashn.ai).

Ключ на сервере (в чат не присылать):  read -s K; lumio-set FASHN_API_KEY "$K"

Модели:
  tryon-max   — лучшее качество; inputs: model_image, product_image, prompt, resolution (1k/2k/4k),
                generation_mode (fast/balanced/quality). Цена: fast 1k = 1 кредит … quality 4k = 5.
  tryon-v1.6  — быстрее и дешевле; inputs: model_image, garment_image, category (auto/tops/bottoms/one-pieces).
Картинки — публичная ссылка или data URI. Неудачные генерации кредиты не списывают.
"""
import os
import time

import requests

KEY = os.environ.get("FASHN_API_KEY", "").strip()
BASE = os.environ.get("FASHN_BASE", "https://api.fashn.ai/v1").rstrip("/")


def ready():
    return bool(KEY)


def _h():
    return {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}


def credits():
    r = requests.get(BASE + "/credits", headers=_h(), timeout=30)
    r.raise_for_status()
    return r.json()


def run(model_name, inputs, timeout=240):
    """Запустить и дождаться результата. Возвращает (список ссылок/base64, кредиты, секунды)."""
    t0 = time.time()
    r = requests.post(BASE + "/run", json={"model_name": model_name, "inputs": inputs}, headers=_h(), timeout=(15, 120))
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code != 200 or not j.get("id"):
        raise RuntimeError(f"FASHN HTTP {r.status_code}: {j.get('error') or j.get('message') or r.text[:300]}")
    pid = j["id"]
    while time.time() - t0 < timeout:
        time.sleep(2)
        s = requests.get(f"{BASE}/status/{pid}", headers=_h(), timeout=30).json()
        st = s.get("status")
        if st == "completed":
            used = s.get("credits_used")
            return s.get("output") or [], used, time.time() - t0
        if st in ("failed", "canceled", "time_out"):
            err = s.get("error") or {}
            raise RuntimeError(f"FASHN {st}: {err.get('name', '')} {err.get('message', '')}".strip())
    raise RuntimeError(f"FASHN: нет результата за {timeout} с (id={pid})")
