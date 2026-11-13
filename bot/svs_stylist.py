"""ИИ-стилист витрины «Сила в Стиле» на OpenAI API с пониманием фото.

  OPENAI_API_KEY    ключ OpenAI из окружения процесса бота (обязательно)
  SVS_OPENAI_MODEL  модель, по умолчанию gpt-5.4-mini (понимает фото)
  SVS_STYLE_DAY     советов в день на покупателя, по умолчанию 30

YANDEX_API_KEY, FOLDER_ID и SVS_VLM_MODEL этому модулю больше не нужны.
Если они нужны другим частям бота, оставьте их в окружении.
Файл .env должен загружаться основным приложением до импорта этого модуля
или передаваться процессу через systemd/Docker/другой способ запуска.

Что делает:
  describe(iid)        один раз описывает вещь по фото: тип, цвет, материал, стиль, сезон (хранится в каталоге)
  pairs(iid)           «С чем носить» — 2–3 вещи из каталога (кэш до изменения каталога)
  outfit(ids)          «Оценить образ» — сочетается ли, совет и замена из каталога
"""
import json
import os
import re
import threading
import time

import requests

import svs_bot as bot

KEY = os.environ.get("OPENAI_API_KEY", "").strip()
MODEL = os.environ.get("SVS_OPENAI_MODEL", "").strip() or "gpt-5.4-mini"
URL = "https://api.openai.com/v1/chat/completions"
PER_DAY = int(os.environ.get("SVS_STYLE_DAY", "30") or 30)
_pairs_cache = {}
_desc_lock = threading.Lock()

STYLIST = ("Ты — стилист магазина премиальной мужской одежды «Сила в Стиле». Отвечай по-русски, коротко, "
           "по делу и дружелюбно, без канцелярита. Оценивай только одежду — сочетание цветов, фактур, стиля "
           "и сезона, никогда не комментируй внешность человека. Отвечай строго JSON без пояснений вокруг.")


# что занимает вещь на человеке: две вещи в одном «слоте» надеть нельзя
SLOTS = [
    (r"^КОСТЮМ|^КОМПЛЕКТ|^ТРОЙК|^ДВОЙК", {"top", "bottom"}, "костюм"),
    (r"^(БРЮК|ДЖИНС|ШОРТ|ЧИНОС|КАРГО|ЮБК)", {"bottom"}, "брюки"),
    (r"^(ЖИЛЕТ|ПИДЖАК|ЖАКЕТ|БЛЕЙЗЕР|КУРТК|БОМБЕР|ВЕТРОВК|ПАРК|ПУХОВИК|ПАЛЬТО|ПЛАЩ|ДУБЛ[ЁЕ]НК|ШУБ|ОВЕРШОТ|ТРЕНЧ|АНОРАК)",
     {"outer"}, "верхняя одежда"),
    (r"^(КРОССОВ|КЕД|ТУФЛ|ЛОФЕР|МОКАСИН|БОТИН|САПОГ|СЛИП|ДЕРБИ|ОКСФОРД|ЧЕЛСИ|САНДАЛ|ШЛ[ЁЕ]П)", {"shoes"}, "обувь"),
]


def slots(title):
    t = (title or "").strip().upper()
    for rx, sl, name in SLOTS:
        if re.match(rx, t):
            return sl, name
    return set(), ""


def conflict(titles):
    """Текст ошибки, если в образе две вещи на одно место (два костюма, костюм + брюки, две пары обуви…)."""
    seen = {}
    for t in titles:
        sl, name = slots(t)
        for s_ in sl:
            if s_ in seen:
                a, b = seen[s_], t
                if slots(a)[1] == name == "костюм":
                    return f"В образе два костюма — {a} и {b}. Носится один, выберите, какой примерить."
                if "костюм" in (slots(a)[1], name):
                    return f"{a} и {b} не надеть вместе: в костюме уже есть брюки. Оставьте что-то одно."
                return f"{a} и {b} — это две вещи одного типа ({name}). Для образа выберите одну."
            seen[s_] = t
    return None


def ready():
    return bool(KEY)


def _small(raw, side=768):
    return bot.data_uri(raw, side=side)


def chat(content, max_tokens=500):
    if not ready():
        raise RuntimeError("Не задан OPENAI_API_KEY в окружении процесса бота")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    t0 = time.time()
    # JSON mode сохраняет контракт describe/pairs/outfit. Фото передаются как прежде.
    # У gpt-5.4-mini reasoning по умолчанию none; temperature не передаём,
    # чтобы не добавлять параметр, совместимость которого зависит от модели.
    try:
        r = requests.post(
            URL, timeout=(10, 120),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_completion_tokens": max(max_tokens, 1024),
                  "response_format": {"type": "json_object"}, "store": False,
                  "messages": [{"role": "system", "content": STYLIST},
                               {"role": "user", "content": content}]},
        )
    except requests.Timeout:
        raise RuntimeError("OpenAI: превышено время ожидания ответа") from None
    except requests.RequestException:
        raise RuntimeError("OpenAI: ошибка соединения, проверьте доступ сервера к api.openai.com") from None
    try:
        j = r.json()
    except ValueError:
        j = None
    if r.status_code != 200:
        error = j.get("error") if isinstance(j, dict) else None
        detail = error.get("message") if isinstance(error, dict) else None
        # Ошибка авторизации может содержать часть ключа: убираем её из логов/probe.
        detail = str(detail or "API вернул ошибку без JSON-описания").replace(KEY, "[скрыто]")
        detail = re.sub(r"sk-[A-Za-z0-9_.*-]+", "[скрыто]", detail)
        raise RuntimeError(f"OpenAI HTTP {r.status_code}: {detail[:300]}")
    if not isinstance(j, dict) or not isinstance(j.get("choices"), list) or not j["choices"]:
        raise RuntimeError("OpenAI: некорректный ответ API, нет choices")
    choice = j["choices"][0]
    msg = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(msg, dict):
        raise RuntimeError("OpenAI: некорректный ответ API, нет message")
    if msg.get("refusal") or choice.get("finish_reason") == "content_filter":
        raise RuntimeError("OpenAI: модель отклонила запрос")
    if choice.get("finish_reason") == "length":
        raise RuntimeError("OpenAI: ответ обрезан лимитом токенов; увеличьте max_tokens в chat()")
    text = msg.get("content") or ""
    try:
        answer = json.loads(text)
    except (ValueError, TypeError):
        raise RuntimeError(f"OpenAI: модель ответила не JSON за {time.time() - t0:.0f} с") from None
    if not isinstance(answer, dict):
        raise RuntimeError("OpenAI: ожидался JSON-объект")
    return answer


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
    ids = []
    for x in ans.get("ids", []):            # без повторов типа: к костюму не предлагаем брюки и второй костюм
        x = str(x)
        if x in db["items"] and x != iid and not conflict([me["title"]] + [db["items"][i]["title"] for i in ids] + [db["items"][x]["title"]]):
            ids.append(x)
    ids = ids[:3]
    res = {"ok": True, "items": ids, "text": str(ans.get("text", ""))[:300]}
    _pairs_cache[key] = res
    return res


# ---------------------------------------------------------------- «Оценить образ»
def outfit(ids):
    db = bot._load()
    ids = [str(i) for i in ids if str(i) in db["items"]][:4]
    if len(ids) < 2:
        return {"ok": False, "error": "Отметьте хотя бы 2 вещи"}
    clash = conflict([db["items"][i]["title"] for i in ids])
    if clash:                                  # очевидное — без ИИ, бесплатно и мгновенно
        return {"ok": True, "good": False, "verdict": "Так не носят", "tip": clash, "swap": None}
    for i in ids:
        describe(i)
    db = bot._load()
    rest = [_line(k, v) for k, v in db["items"].items() if k not in ids]
    content = [{"type": "text", "text":
                "Оцени образ из этих вещей (фото по порядку):\n" + "\n".join(_line(i, db["items"][i]) for i in ids) +
                "\n\nДругие вещи каталога для возможной замены:\n" + ("\n".join(rest[:120]) or "—") +
                "\n\nНазвание вещи важнее фото: «КОСТЮМ» — это комплект из верха и низа, даже если на фото видно "
                "только кофту. Учитывай тип вещи из названия.\n\nВерни JSON: {\"ok\": true если вещи хорошо сочетаются иначе false, "
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
