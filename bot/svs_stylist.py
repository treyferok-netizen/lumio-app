"""ИИ-стилист витрины «Сила в Стиле» на OpenAI API с пониманием фото.

  OPENAI_API_KEY    ключ OpenAI из окружения процесса бота (обязательно)
  SVS_OPENAI_MODEL  модель, по умолчанию gpt-5.4-mini (понимает фото)
  SVS_STYLE_DAY     советов в день на покупателя, по умолчанию 30

YANDEX_API_KEY, FOLDER_ID и SVS_VLM_MODEL этому модулю больше не нужны.
Если они нужны другим частям бота, оставьте их в окружении.
Файл .env должен загружаться основным приложением до импорта этого модуля
или передаваться процессу через systemd/Docker/другой способ запуска.

Что делает:
  describe(iid)        один раз описывает вещь по фото; ai и ai_meta сохраняются в svs.json
  catalog_changed(iid) ставит новую/изменённую вещь в приоритет фонового анализа
  pairs(iid)           «С чем носить» — локальный поиск по всему каталогу + текстовый запрос GPT
  outfit(ids)          «Оценить образ» — по сохранённым описаниям, без повторной отправки фото

Старые описания используются без повторного анализа. Только изменение фото
или названия сбрасывает описание; цена и размеры его не сбрасывают.
Файлы svs_bot.py и svs_stylist.py обновляются вместе.
"""
import hashlib
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
_worker_lock = threading.Lock()
_catalog_changed = threading.Event()
_worker_started = False
_retry_after = 0.0
_pending_ids = set()
_failed_items = {}
_candidate_limit = 36
_ai_fields = ("type", "color", "material", "style", "season", "pattern", "fit", "details")

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


# ---------------------------------------------------------------- постоянные описания вещей
def _source(it):
    """Описание меняется только вместе с фото или названием, а не с ценой."""
    raw = json.dumps([it.get("photo", ""), it.get("title", "")], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _has_description(it):
    ai = it.get("ai")
    if not isinstance(ai, dict) or not any(ai.get(k) for k in _ai_fields):
        return False
    meta = it.get("ai_meta")
    source = meta.get("source") if isinstance(meta, dict) else None
    # Старые описания сохраняем: миграция не должна заново оплачивать весь каталог.
    return source is None or source == _source(it)


def catalog_changed(iid=None):
    """Разбудить единственный обработчик после добавления/редактирования товара."""
    if iid is not None:
        with _worker_lock:
            _pending_ids.add(str(iid))
    _catalog_changed.set()


def describe(iid):
    iid = str(iid)
    it = bot._load()["items"].get(str(iid))
    if not it:
        return None
    if _has_description(it):
        return it["ai"]
    with _desc_lock:
        for _ in range(2):
            it = bot._load()["items"].get(iid)
            if not it:
                return None
            if _has_description(it):
                return it["ai"]
            source = _source(it)
            # Берём конкретный file_id, чтобы правка поста во время запроса
            # не привязала старое фото к новому товару через дисковый кэш.
            raw = bot.file_bytes(it["photo"])
            ai = chat([{"type": "text", "text":
                       f"Вещь из каталога: «{it['title']}». Опиши именно эту вещь на фото, "
                       "игнорируя остальные вещи на модели. Сохрани достаточно деталей для последующего "
                       "подбора образа без повторного просмотра фото. Не угадывай состав ткани по фото: "
                       "укажи видимую фактуру, а неизвестные признаки — «неизвестно». Верни JSON: "
                       '{"type":"тип вещи", "color":"основной и дополнительные цвета", '
                       '"material":"видимая фактура/материал", '
                       '"style":"классика|кэжуал|спорт-шик|streetwear|smart casual", '
                       '"season":"лето|демисезон|зима|всесезон", '
                       '"pattern":"однотонная/принт/полоска/клетка и т.п.", '
                       '"fit":"крой и силуэт", "details":"важные детали для сочетаний"}'},
                       {"type": "image_url", "image_url": {"url": _small(raw)}}], 450)
            ai = {k: str(ai.get(k) or "")[:(160 if k == "details" else 80)] for k in _ai_fields}
            if not any(ai.values()):
                raise RuntimeError("OpenAI: пустое описание вещи")
            with bot._lock:
                db = bot._load()
                current = db["items"].get(iid)
                if not current:
                    return None
                if _source(current) != source:
                    continue
                current["ai"] = ai
                current["ai_meta"] = {"source": source, "model": MODEL, "created_at": int(time.time())}
                bot._save(db)
                return ai
        catalog_changed(iid)
        raise RuntimeError("Вещь изменилась во время анализа; описание будет обновлено фоном")


def _describe_batch():
    """Один проход. При ошибке API делаем паузу вместо запросов по всему каталогу."""
    global _retry_after
    _catalog_changed.clear()
    if not ready() or time.monotonic() < _retry_after:
        return
    items = bot._load()["items"]
    with _worker_lock:
        priority = _pending_ids.intersection(items)
        _pending_ids.intersection_update(items)
    for removed in set(_failed_items).difference(items):
        _failed_items.pop(removed, None)
    ordered = [(i, items[i]) for i in sorted(priority)] + [(i, it) for i, it in items.items() if i not in priority]
    for iid, it in ordered:
        if _has_description(it):
            with _worker_lock:
                _pending_ids.discard(iid)
            continue
        if not _has_description(it):
            failed = _failed_items.get(iid)
            if failed and failed[0] == _source(it) and time.monotonic() < failed[1]:
                continue
            with _worker_lock:
                _pending_ids.discard(iid)
            try:
                describe(iid)
                _failed_items.pop(iid, None)
            except Exception as e:
                message = repr(e)
                for secret in (KEY, getattr(bot, "TOKEN", "")):
                    if secret:
                        message = message.replace(secret, "[скрыто]")
                print("svs describe error", iid, message[:200], flush=True)
                if str(e).startswith("OpenAI"):
                    _retry_after = time.monotonic() + 600
                    return
                # Неисправное фото одного товара не блокирует остальные.
                _failed_items[iid] = (_source(it), time.monotonic() + 600)
            if _catalog_changed.is_set():
                # Новая вещь приоритетнее долгого первоначального заполнения базы.
                return


def describe_missing():
    """Новые товары — сразу после сигнала; недописанные после перезапуска — автоматически."""
    while True:
        _describe_batch()
        remaining = _retry_after - time.monotonic()
        _catalog_changed.wait(timeout=max(1, remaining) if remaining > 0 else 600)


def _line(iid, it):
    ai = (it.get("ai") or {}) if _has_description(it) else {}
    tags = "; ".join(f"{k}={ai[k]}" for k in _ai_fields if ai.get(k))
    return f"{iid}: {it['title']} — {bot.money(it['price'])}" + (f" ({tags})" if tags else "")


def _version(db):
    # Важны также изменение фото, цены, описания и удаление — не только число вещей.
    rows = [(str(i), {k: it.get(k) for k in ("title", "price", "sizes", "photo", "ai", "ai_meta")})
            for i, it in sorted(db["items"].items())]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _group(it):
    title = it.get("title", "")
    ai = it.get("ai") or {}
    for text in (title, str(ai.get("type", ""))):
        sl, _ = slots(text)
        if sl:
            return "+".join(sorted(sl))
        if re.match(r"^(ФУТБОЛК|ПОЛО|РУБАШК|СОРОЧК|ЛОНГСЛИВ|ВОДОЛАЗК|ДЖЕМПЕР|СВИТЕР|КАРДИГАН|ХУДИ|СВИТШОТ|ТОЛСТОВК|ОЛИМПИЙК|КОФТ|ТРИКОТАЖ)", text.upper()):
            return "top"
    return "accessory:" + re.split(r"\s+", str(ai.get("type") or title).lower())[0]


def _candidate_ids(db, selected, mode):
    """Локальный поиск по ВСЕМУ каталогу; GPT получает только короткую подборку."""
    selected = set(selected)
    anchors = [db["items"][i] for i in selected]
    occupied = set().union(*(slots(it["title"])[0] for it in anchors))
    used_groups = {_group(it) for it in anchors}
    groups = {}
    for iid, it in db["items"].items():
        if iid in selected:
            continue
        group = _group(it)
        sl, _ = slots(it["title"])
        if mode == "pairs" and (sl & occupied or group in used_groups or ("bottom" in occupied and group == "bottom+top")):
            continue
        if mode == "outfit" and group not in used_groups:
            continue
        ai = (it.get("ai") or {}) if _has_description(it) else {}
        score = 1 if ai else 0
        for anchor in anchors:
            other = anchor.get("ai") or {}
            for field, weight in (("style", 4), ("season", 3), ("color", 1)):
                a, b = str(ai.get(field) or "").lower(), str(other.get(field) or "").lower()
                if a and b and a != "неизвестно" and b != "неизвестно":
                    score += weight if a == b else 0
                    if field == "season":
                        score += 1 if "всесезон" in (a, b) else 0
                        score -= 3 if {a, b} == {"лето", "зима"} else 0
        groups.setdefault(group, []).append((score, str(iid)))
    # Кандидаты разных типов представлены равномерно, а не только первыми ID.
    buckets = [sorted(rows, key=lambda x: (-x[0], x[1])) for rows in groups.values()]
    buckets.sort(key=lambda rows: (-rows[0][0], rows[0][1]))
    result = []
    for rank in range(_candidate_limit):
        for rows in buckets:
            if rank < len(rows):
                result.append(rows[rank][1])
                if len(result) == _candidate_limit:
                    return result
    return result


# ---------------------------------------------------------------- «С чем носить»
def pairs(iid):
    iid = str(iid)
    db = bot._load()
    if iid not in db["items"]:
        return {"ok": False, "error": "Вещь не найдена"}
    describe(iid)
    db = bot._load()
    if iid not in db["items"]:
        return {"ok": False, "error": "Вещь не найдена"}
    key = (iid, _version(db))
    if key in _pairs_cache:
        return _pairs_cache[key]
    me = db["items"][iid]
    candidates = _candidate_ids(db, [iid], "pairs")
    others = [_line(k, db["items"][k]) for k in candidates]
    if not others:
        return {"ok": True, "items": [], "text": ""}
    ans = chat([{"type": "text", "text":
                 f"Покупатель смотрит вещь:\n{_line(iid, me)}\n\nПодходящие кандидаты из каталога:\n" + "\n".join(others) +
                 "\n\nПодбери 2–3 вещи из каталога, которые лучше всего дополнят её в один образ (разные типы вещей, "
                 "не две куртки). Верни JSON: {\"ids\": [\"номер\", …], \"text\": \"одно предложение, почему это "
                 "сочетается\"}"}], 300)
    ids = []
    for x in ans.get("ids", []):            # без повторов типа: к костюму не предлагаем брюки и второй костюм
        x = str(x)
        if x in candidates and x not in ids and _group(db["items"][x]) not in {_group(db["items"][i]) for i in ids} and not conflict([me["title"]] + [db["items"][i]["title"] for i in ids] + [db["items"][x]["title"]]):
            ids.append(x)
    ids = ids[:3]
    res = {"ok": True, "items": ids, "text": str(ans.get("text", ""))[:300]}
    _pairs_cache[key] = res
    while len(_pairs_cache) > 256:
        _pairs_cache.pop(next(iter(_pairs_cache)), None)
    return res


# ---------------------------------------------------------------- «Оценить образ»
def outfit(ids):
    db = bot._load()
    ids = list(dict.fromkeys(str(i) for i in ids if str(i) in db["items"]))[:4]
    if len(ids) < 2:
        return {"ok": False, "error": "Отметьте хотя бы 2 вещи"}
    clash = conflict([db["items"][i]["title"] for i in ids])
    if clash:                                  # очевидное — без ИИ, бесплатно и мгновенно
        return {"ok": True, "good": False, "verdict": "Так не носят", "tip": clash, "swap": None}
    for i in ids:
        describe(i)
    db = bot._load()
    if any(i not in db["items"] for i in ids):
        return {"ok": False, "error": "Каталог изменился; выберите вещи заново"}
    candidates = _candidate_ids(db, ids, "outfit")
    rest = [_line(k, db["items"][k]) for k in candidates]
    content = [{"type": "text", "text":
                "Оцени образ по сохранённым описаниям вещей. Фотографии уже проанализированы при добавлении "
                "товаров. Не выдумывай отсутствующие признаки, посадку на конкретном человеке или точный состав ткани.\n" +
                "\n".join(_line(i, db["items"][i]) for i in ids) +
                "\n\nКандидаты из всего каталога для возможной замены:\n" + ("\n".join(rest) or "—") +
                "\n\nНазвание вещи важнее фото: «КОСТЮМ» — это комплект из верха и низа, даже если на фото видно "
                "только кофту. Учитывай тип вещи из названия.\n\nВерни JSON: {\"ok\": true если вещи хорошо сочетаются иначе false, "
                "\"verdict\": \"короткий итог, до 8 слов\", \"tip\": \"1–2 предложения: почему и как носить\", "
                "\"swap_out\": \"номер вещи, которую лучше заменить, или null\", "
                "\"swap_in\": \"номер вещи из каталога на замену, или null\"}"}]
    ans = chat(content, 400)
    out, inn = str(ans.get("swap_out") or ""), str(ans.get("swap_in") or "")
    swap = None
    if out in ids and inn in candidates:
        titles = [db["items"][i]["title"] for i in ids if i != out] + [db["items"][inn]["title"]]
        if not conflict(titles) and _group(db["items"][out]) == _group(db["items"][inn]):
            swap = {"out": out, "in": inn}
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
    global _worker_started
    if not ready():
        return
    with _worker_lock:
        if not _worker_started:
            _worker_started = True
            threading.Thread(target=describe_missing, daemon=True).start()

