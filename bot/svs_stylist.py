"""ИИ-стилист витрины «Сила в Стиле» на Yandex AI Studio (OpenAI-совместимый API, модель с пониманием фото).

Ключ и папка — те же, что у бота Lumio: YANDEX_API_KEY, FOLDER_ID.
  SVS_VLM_MODEL   модель, по умолчанию qwen3.6-35b-a3b (понимает фото; Gemma 3 в AI Studio больше нет)
  SVS_STYLE_DAY   советов в день на покупателя, по умолчанию 30

Что делает:
  describe(iid)        один раз описывает вещь по фото: тип, цвет, материал, стиль, сезон (хранится в каталоге)
  pairs(iid)           «С чем носить» — 2–3 вещи из каталога (кэш до изменения каталога)
  outfit(ids)          «Оценить образ» — сочетается ли, совет и замена из каталога
"""
import base64
import json
import os
import re
import threading
import time

import requests

import svs_bot as bot

KEY = os.environ.get("YANDEX_API_KEY", "").strip()
FOLDER = os.environ.get("FOLDER_ID", "").strip()
MODEL = os.environ.get("SVS_VLM_MODEL", "qwen3.6-35b-a3b").strip()
URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"
PER_DAY = int(os.environ.get("SVS_STYLE_DAY", "30") or 30)
_pairs_cache = {}
_desc_lock = threading.Lock()

STYLIST = ("Ты — стилист магазина премиальной мужской одежды «Сила в Стиле». Отвечай по-русски, коротко, "
           "по делу и дружелюбно, без канцелярита. Оценивай только одежду — сочетание цветов, фактур, стиля "
           "и сезона, никогда не комментируй внешность человека. Отвечай строго JSON без пояснений вокруг.")


def ready():
    return bool(KEY and FOLDER)


def _small(raw, side=768):
    return bot.data_uri(raw, side=side)


def chat(content, max_tokens=500):
    r = requests.post(URL, timeout=(10, 90), headers={"Authorization": f"Api-Key {KEY}"},
                      json={"model": f"gpt://{FOLDER}/{MODEL}", "temperature": 0.3, "max_tokens": max_tokens,
                            "messages": [{"role": "system", "content": STYLIST},
                                         {"role": "user", "content": content}]})
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        raise RuntimeError(f"Yandex AI {r.status_code}: {(j.get('error') or {}).get('message') or r.text[:300]}")
    text = j["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("модель ответила не JSON: " + text[:200])
    return json.loads(m.group(0))


def probe():
    """Проверка доступа: короткий запрос без картинки."""
    try:
        return {"ok": True, "model": MODEL, "answer": chat('Ответь JSON {"ok": true}', 20)}
    except Exception as e:
        return {"ok": False, "model": MODEL, "error": str(e)[:300]}


# ---------------------------------------------------------------- описания вещей
def describe(iid):
    it = bot._load()["items"].get(str(iid))
    if not it:
        return None
    if it.get("ai"):
        return it["ai"]
    with _desc_lock:
        it = bot._load()["items"].get(str(iid))
        if it.get("ai"):
            return it["ai"]
        ai = chat([{"type": "text", "text": f"Вещь из каталога: «{it['title']}». Посмотри на фото и опиши её. Верни JSON: "
                                            '{"type": "тип вещи", "color": "основной цвет", "material": "материал/фактура", '
                                            '"style": "классика|кэжуал|спорт-шик|streetwear|smart casual", '
                                            '"season": "лето|демисезон|зима|всесезон"}'},
                    {"type": "image_url", "image_url": {"url": _small(bot.item_image(iid))}}], 200)
        ai = {k: str(ai.get(k, ""))[:40] for k in ("type", "color", "material", "style", "season")}
        with bot._lock:
            db = bot._load()
            if str(iid) in db["items"]:
                db["items"][str(iid)]["ai"] = ai
                bot._save(db)
        return ai


def describe_missing():
    """Фоном описывает вещи без описания (раз в 10 минут)."""
    while True:
        if ready():
            for iid, it in list(bot._load()["items"].items()):
                if not it.get("ai"):
                    try:
                        describe(iid)
                    except Exception as e:
                        print("svs describe error", iid, repr(e)[:200], flush=True)
                        break
        time.sleep(600)


def _line(iid, it):
    ai = it.get("ai") or {}
    tags = ", ".join(v for v in (ai.get("color"), ai.get("material"), ai.get("style"), ai.get("season")) if v)
    return f"{iid}: {it['title']} — {bot.money(it['price'])}" + (f" ({tags})" if tags else "")


def _version(db):
    return f"{len(db['items'])}:{max([0] + [int(k) for k in db['items']])}"


# ---------------------------------------------------------------- «С чем носить»
def pairs(iid):
    iid = str(iid)
    db = bot._load()
    if iid not in db["items"]:
        return {"ok": False, "error": "Вещь не найдена"}
    key = (iid, _version(db))
    if key in _pairs_cache:
        return _pairs_cache[key]
    describe(iid)
    db = bot._load()
    me = db["items"][iid]
    others = [_line(k, v) for k, v in db["items"].items() if k != iid]
    if not others:
        return {"ok": True, "items": [], "text": ""}
    ans = chat([{"type": "text", "text":
                 f"Покупатель смотрит вещь:\n{_line(iid, me)}\n\nКаталог магазина:\n" + "\n".join(others[:120]) +
                 "\n\nПодбери 2–3 вещи из каталога, которые лучше всего дополнят её в один образ (разные типы вещей, "
                 "не две куртки). Верни JSON: {\"ids\": [\"номер\", …], \"text\": \"одно предложение, почему это "
                 "сочетается\"}"}], 300)
    ids = [str(x) for x in ans.get("ids", []) if str(x) in db["items"] and str(x) != iid][:3]
    res = {"ok": True, "items": ids, "text": str(ans.get("text", ""))[:300]}
    _pairs_cache[key] = res
    return res


# ---------------------------------------------------------------- «Оценить образ»
def outfit(ids):
    db = bot._load()
    ids = [str(i) for i in ids if str(i) in db["items"]][:4]
    if len(ids) < 2:
        return {"ok": False, "error": "Отметьте хотя бы 2 вещи"}
    for i in ids:
        describe(i)
    db = bot._load()
    rest = [_line(k, v) for k, v in db["items"].items() if k not in ids]
    content = [{"type": "text", "text":
                "Оцени образ из этих вещей (фото по порядку):\n" + "\n".join(_line(i, db["items"][i]) for i in ids) +
                "\n\nДругие вещи каталога для возможной замены:\n" + ("\n".join(rest[:120]) or "—") +
                "\n\nВерни JSON: {\"ok\": true если вещи хорошо сочетаются иначе false, "
                "\"verdict\": \"короткий итог, до 8 слов\", \"tip\": \"1–2 предложения: почему и как носить\", "
                "\"swap_out\": \"номер вещи, которую лучше заменить, или null\", "
                "\"swap_in\": \"номер вещи из каталога на замену, или null\"}"}]
    for i in ids:
        content.append({"type": "image_url", "image_url": {"url": _small(bot.item_image(i), 640)}})
    ans = chat(content, 400)
    out, inn = str(ans.get("swap_out") or ""), str(ans.get("swap_in") or "")
    swap = {"out": out, "in": inn} if out in ids and inn in db["items"] and inn not in ids else None
    return {"ok": True, "good": bool(ans.get("ok")), "verdict": str(ans.get("verdict", ""))[:80],
            "tip": str(ans.get("tip", ""))[:400], "swap": swap}


def allowed(uid):
    """Лимит советов на покупателя в день."""
    if uid in bot.ADMINS:
        return True
    with bot._lock:
        db = bot._load()
        u = bot.user(db, uid)
        if u.get("style_day") != u["day"]:
            u["style_day"], u["style"] = u["day"], 0
        if u.get("style", 0) >= PER_DAY:
            return False
        u["style"] = u.get("style", 0) + 1
        bot._save(db)
    return True


def start():
    if ready():
        threading.Thread(target=describe_missing, daemon=True).start()
