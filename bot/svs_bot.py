"""Бот-примерочная «Сила в Стиле» (@sila_v_stile_bot): каталог из канала + примерка GPT Image.

Настройки (lumio-set ИМЯ ЗНАЧЕНИЕ):
  SVS_BOT_TOKEN     токен бота из @BotFather (без него бот выключен)
  SVS_ADMIN_IDS     id админов через запятую (по умолчанию ADMIN_CHAT_ID)
  SVS_MANAGER       username для заказов, по умолчанию romanov_roman_svs
  SVS_DAILY_FREE    бесплатных примерок в день на человека, по умолчанию 3
  OPENAI_API_KEY    ключ OpenAI для примерки GPT Image
  SVS_IMAGE_MODEL  gpt-image-2.5-sunburst по умолчанию
  SVS_TRYON_PROVIDER  основной движок: gpt (по умолчанию) или fashn
  SVS_TRYON_FALLBACK  запасной при любой ошибке основного: fashn (по умолчанию) или off
  FASHN_API_KEY     ключ FASHN (запасной движок)
  (у Women's Line те же настройки с префиксом WL_)

Каталог пополняется сам:
  • бот — администратор канала: каждый пост с фото и строкой «Цена …» становится вещью;
  • старые посты админ пересылает боту в личку — разбираются так же.
Добавляется каждый пост с фото и ценой (одежда, сумки, обувь — всё). Без цены — пропуск. Убрать: /del N.
Фото покупателя хранится только как file_id Telegram; удалить — /delete_photo.
"""
import base64
import html
import json
import os
import re
import threading
import time

import requests

import fashn
import gpt_tryon
import seedream
import shops

SHOP = globals().get("SHOP", "SVS")          # второй магазин загружается тем же кодом (shops.py)
C = shops.cfg(SHOP)
BRAND = C["name"]


def _env(key, default=""):
    return os.environ.get(f"{SHOP}_{key}", default)


def ai_stylist():
    """Подключён настоящий ИИ-стилист (а не заглушка «две вещи одного типа»)."""
    return hasattr(stylist(), "correct_color")


def stylist():
    """ИИ-стилист этого магазина (для «Силы в Стиле» — svs_stylist.py, у женского — заглушка из shops.py)."""
    st = globals().get("_STYLIST")
    if st is None:
        import importlib
        st = importlib.import_module("svs_stylist")
    return st


TOKEN = _env("BOT_TOKEN").strip()
API = f"https://api.telegram.org/bot{TOKEN}/"
MANAGER = (_env("MANAGER") or C["manager"]).lstrip("@")
DAILY = int(_env("DAILY_FREE", "3") or 3)
SUBS_ONLY = _env("SUBSCRIBERS_ONLY", "1").strip() not in ("0", "no", "false", "")
CHANNEL_LINK = _env("CHANNEL_LINK").strip()      # ссылка-приглашение в закрытый канал
AUTO_APPROVE = _env("AUTO_APPROVE", "1").strip() not in ("0", "no", "false", "")
JOIN_WELCOME = _env("JOIN_WELCOME", "1").strip() not in ("0", "no", "false", "")
ADMINS = {int(x) for x in re.findall(r"-?\d+", _env("ADMIN_IDS") or os.environ.get("ADMIN_CHAT_ID") or "")}
BOT_USERNAME = C["bot"]                          # уточняется через getMe при запуске
ASK = f"@{MANAGER}" if MANAGER else "нам в этот чат"   # куда писать с вопросами
STORE = os.path.join(seedream.DIR, f"{C['store']}.json")
UDIR = os.path.join(seedream.DIR, f"{C['store']}_users")          # фото покупателей — не публичные
IDIR = os.path.join(seedream.DIR, f"{C['store']}_items")          # кэш фото вещей для витрины
APP_URL = _env("APP_URL").strip() or (os.environ.get("PUBLIC_BASE", "https://212-87-223-41.sslip.io").rstrip("/")
                                      + C["path"] + "/")
PAGE = 5
_lock = threading.Lock()
_groups = {}            # media_group_id -> {"photo", "caption", "ts", "admin_chat"}
_color_edits = {}       # личный чат администратора -> текущая вещь и шаг исправления
_tryon_slots = threading.Semaphore(2)


# ---------------------------------------------------------------- хранилище
def _load():
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"items": {}, "users": {}, "stats": {"tryons": 0, "orders": 0}}


def _save(db):
    os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(tmp, STORE)


def status():
    db = _load()
    try:
        import svs_face
        face = os.path.basename(svs_face._cascade_path()) if svs_face.ENABLED else "off"
    except Exception as e:
        face = "error: " + repr(e)[:120]
    return {"shop": SHOP, "brand": BRAND, "bot": BOT_USERNAME, "app": APP_URL, "manager": MANAGER,
            "channel": channel_id(), "channel_link": bool(CHANNEL_LINK),
            "token": bool(TOKEN), "fashn": fashn.ready(), "tryon_engine": tryon_provider(), "tryon_order": tryon_engines(),
            "tryon_ready": tryon_ready(), "gpt_image": gpt_tryon.status(SHOP),
            "face_restore": face, "admins": sorted(ADMINS), "items": len(db["items"]),
            "users": len(db["users"]), "stats": db["stats"],
            "last_items": [f'{k}: {v["title"]} — {v["price"]}' for k, v in list(db["items"].items())[-10:]]}


# ---------------------------------------------------------------- telegram
def tg(method, data=None, timeout=(5, 15)):
    try:
        r = requests.post(API + method, json=data or {}, timeout=timeout).json()
    except Exception as e:
        print("svs tg error", method, repr(e)[:200].replace(TOKEN, "<t>"), flush=True)
        return {"ok": False}
    if not r.get("ok") and "style" in json.dumps(data or {}) and "button" in str(r.get("description", "")).lower():
        return tg(method, json.loads(json.dumps(data).replace('"style":', '"_style":')), timeout)  # старый клиент API
    if not r.get("ok"):
        print("svs tg", method, r.get("description"), flush=True)
    return r


def send_photo_bytes(chat, raw, caption="", reply_markup=None):
    """sendPhoto файлом (а не ссылкой) — для результатов примерки, обработанных на сервере."""
    data = {"chat_id": str(chat), "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        r = requests.post(API + "sendPhoto", data=data, files={"photo": ("tryon.jpg", raw, "image/jpeg")},
                          timeout=(5, 60)).json()
    except Exception as e:
        print("svs sendPhoto error", repr(e)[:200].replace(TOKEN, "<t>"), flush=True)
        return {"ok": False}
    if not r.get("ok") and "style" in data.get("reply_markup", ""):
        data["reply_markup"] = data["reply_markup"].replace('"style":', '"_style":')
        r = requests.post(API + "sendPhoto", data=data, files={"photo": ("tryon.jpg", raw, "image/jpeg")},
                          timeout=(5, 60)).json()
    return r


HEADWEAR = re.compile(r"^(ОЧК|КЕПК|ШАПК|БЕЙСБОЛК|ПАНАМ|ШЛЯП|БАНДАН|КАПЮШОН|БАЛАКЛАВ|СЕРЬГ|СЕР[ЁЕ]Ж|КАФФ|ЗАКОЛК|ОБОДОК|ПЛАТОК|"
                      r"КОСЫНК)", re.I)


def finish_tryon(uid, url, titles=()):
    """Скачать результат FASHN и вернуть на него лицо покупателя с исходного фото.
    Очки и головные уборы надеваются на голову — для них лицо не возвращаем, иначе вещь пропадёт."""
    import svs_face
    r = requests.get(url, timeout=(10, 60))
    r.raise_for_status()
    orig = user_photo(uid)
    if not orig:
        return r.content
    raw = svs_face.match_aspect(orig, r.content)        # пропорции как на исходном фото — без «вытягивания»
    if any(HEADWEAR.match((t or "").strip()) for t in titles):
        return raw
    return svs_face.restore(orig, raw)


def btn(text, data=None, url=None, style=None):
    b = {"text": text}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = data
    if style:
        b["style"] = style
    return b


def app_btn(text="🛍 Открыть витрину"):
    return {"text": text, "web_app": {"url": APP_URL}, "style": "success"}


def kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


def send(chat, text, **kw):
    return tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                              **{k: v for k, v in kw.items() if v is not None}})


def file_bytes(file_id):
    info = tg("getFile", {"file_id": file_id}).get("result") or {}
    if not info.get("file_path"):
        raise RuntimeError("Telegram не отдал файл")
    r = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}", timeout=(10, 60))
    r.raise_for_status()
    return r.content


def data_uri(raw, side=2048):
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        k = side / max(h, w)
        if k < 1:
            img = cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ok:
            raw = buf.tobytes()
    except Exception as e:
        print("svs shrink", repr(e), flush=True)
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode()


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        print("svs bg error", fn.__name__, repr(e)[:200].replace(TOKEN, "<t>"), flush=True)


def photo_path(uid):
    return os.path.join(UDIR, f"{int(uid)}.jpg")


def save_user_photo(uid, raw):
    os.makedirs(UDIR, mode=0o700, exist_ok=True)
    with open(photo_path(uid), "wb") as f:
        f.write(raw)


def user_photo(uid):
    """Байты фото покупателя или None."""
    try:
        with open(photo_path(uid), "rb") as f:
            return f.read()
    except OSError:
        return None


def drop_user_photo(uid):
    try:
        os.remove(photo_path(uid))
    except OSError:
        pass


def item_image(iid):
    """Фото вещи для витрины: кэш на диске, при первом запросе — из Telegram."""
    p = os.path.join(IDIR, f"{int(iid)}.jpg")
    if os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read()
    it = _load()["items"].get(str(iid))
    if not it:
        return None
    raw = file_bytes(it["photo"])
    os.makedirs(IDIR, exist_ok=True)
    with open(p, "wb") as f:
        f.write(raw)
    return raw


# ---------------------------------------------------------------- только для подписчиков канала
_subs_cache = {}


def channel_id():
    """Канал берём из настроек, иначе — из постов, которые бот уже получил из канала."""
    env = _env("CHANNEL_ID").strip()
    if env:
        return env
    counts = {}
    for it in _load()["items"].values():
        cid = str(it.get("source", "")).split(":")[0]
        if cid.startswith("-100"):
            counts[cid] = counts.get(cid, 0) + 1
    return max(counts, key=counts.get) if counts else None


def is_subscriber(uid):
    """True — подписан (или проверка выключена/невозможна, чтобы не ломать примерку)."""
    if not SUBS_ONLY or uid in ADMINS:
        return True
    hit = _subs_cache.get(uid)              # помним только «подписан»: отказ проверяем заново каждый раз
    if hit and time.time() - hit < 600:
        return True
    cid = channel_id()
    if not cid:
        return True
    r = tg("getChatMember", {"chat_id": int(cid), "user_id": int(uid)})
    if not r.get("ok"):
        return True                    # бот не админ канала или Telegram недоступен — не мешаем покупателю
    st = r["result"].get("status")
    ok = st in ("creator", "administrator", "member") or (st == "restricted" and r["result"].get("is_member"))
    if ok:
        _subs_cache[uid] = time.time()
    else:
        print("svs not subscribed", uid, "channel", cid, "status", st, flush=True)
    return ok


NOT_SUB = f"Примерка доступна подписчикам канала «{BRAND}». Подпишитесь — и возвращайтесь примерять 🙌"


SUBSCRIBED = "Я подписалась ✅" if C["female"] else "Я подписался ✅"


def sub_markup():
    return kb([btn("Подписаться на канал", url=CHANNEL_LINK, style="success")], [btn(SUBSCRIBED, "cat")]) \
        if CHANNEL_LINK else kb([btn(SUBSCRIBED, "cat")])


# ---------------------------------------------------------------- инструкция для примерки по типу вещи
def tryon_provider():
    # Основной движок: gpt для обоих магазинов; {SHOP}_TRYON_PROVIDER=fashn — вернуть FASHN основным.
    return _env("TRYON_PROVIDER", "gpt").strip().lower()


def _engine_ready(engine):
    return gpt_tryon.ready(SHOP) if engine == "gpt" else (fashn.ready() if engine == "fashn" else False)


def tryon_engines():
    """Порядок попыток: основной движок, затем запасной ({SHOP}_TRYON_FALLBACK, по умолчанию fashn; off — без запасного).
    Движок без ключа пропускается."""
    primary = tryon_provider()
    fallback = _env("TRYON_FALLBACK", "fashn").strip().lower()
    order = [primary] + ([fallback] if fallback not in ("", "off", "none", primary) else [])
    return [e for e in order if _engine_ready(e)]


def tryon_ready():
    return bool(tryon_engines())


def gpt_outfit(uid, items):
    """Оригинальное фото клиента + все выбранные вещи в одном запросе."""
    return gpt_tryon.run(user_photo(uid), [(it["title"], tryon_image(iid)) for iid, it in items], shop=SHOP)


def fashn_outfit(uid, items, on_step=None):
    """FASHN: вещи надеваются по очереди, каждая на предыдущий результат; лицо — с исходного фото."""
    model = data_uri(user_photo(uid))
    for n, (iid, it) in enumerate(items, 1):
        if on_step:
            on_step(n)
        outs, _, _ = fashn.run(*tryon_request(it["title"], model, data_uri(tryon_image(iid))))
        if not outs:
            raise RuntimeError("пустой ответ FASHN")
        model = outs[0]
    return finish_tryon(uid, model, [it["title"] for _, it in items])


def run_tryon(uid, items, on_engine=None, on_step=None):
    """Примерка с запасным движком. Возвращает (jpeg, engine). Если упали все — исключение последнего;
    сообщение для покупателя — из public_tryon_error."""
    engines = tryon_engines()
    if not engines:
        raise RuntimeError("нет готового движка примерки")
    last = None
    for engine in engines:
        if on_engine:
            on_engine(engine)
        try:
            if engine == "gpt":
                if on_step:
                    on_step(1)
                raw, _, _ = gpt_outfit(uid, items)
            else:
                raw = fashn_outfit(uid, items, on_step)
            if last is not None:
                print(f"{SHOP.lower()} tryon fallback ok", engine, flush=True)
            return raw, engine
        except Exception as e:
            last = e
            print(f"{SHOP.lower()} tryon {engine} error", gpt_tryon.safe_error(e, SHOP), flush=True)
    raise last


def public_tryon_error(e):
    # Фото, которое не прошло проверку GPT, а FASHN тоже не справился, — понятная причина для покупателя.
    if isinstance(e, gpt_tryon.GPTImageError):
        return e.public_message
    return ("Не получилось примерить 😔 Попробуйте другое фото: в полный рост, лицом к камере, "
            "без сильного наклона. Если не выйдет — напишите " + ASK)


_KEEP = " Keep the person's face, body, pose, background and all their other clothes unchanged."
_PROMPTS = [
    (r"^(СЕРЬГ|СЕР[ЁЕ]Ж|КАФФ)", "Put the earrings from the product photo on the person's ears at realistic, small scale."),
    (r"^(КОЛЬЕ|ПОДВЕСК|ЦЕПОЧК|ЦЕПЬ|ЧОКЕР|БУСЫ|ОЖЕРЕЛЬ)", "Put the necklace from the product photo on the person's neck at "
                                                        "realistic scale, lying naturally over their clothes or skin."),
    (r"^(КОЛЬЦ)", "Put the ring from the product photo on the person's finger at realistic, small scale."),
    (r"^(КРОССОВ|КЕД|ТУФЛ|ЛОФЕР|МОКАСИН|БОТИН|БОТИЛЬОН|БОТФОРТ|САПОГ|СЛИП|ДЕРБИ|ОКСФОРД|ЧЕЛСИ|САНДАЛ|БОСОНОЖ|"
     r"БАЛЕТК|МЮЛИ|ЭСПАДРИЛ|ШЛ[ЁЕ]П|ОБУВ)",
     "Replace only the shoes the person is wearing with the shoes from the product photo. The new shoes must be a "
     "realistic size: exactly the same length and height as the person's current shoes, proportional to their feet and "
     "body, not oversized, not chunky, not enlarged. Keep the trouser hems falling over the shoes as before."),
    (r"^(ОЧК)", "Put the glasses from the product photo on the person's face at realistic scale, fitted to their "
                "head width, resting on the nose and ears."),
    (r"^(ЧАС[ЫО]|БРАСЛЕТ)", "Put the watch/bracelet from the product photo on the person's wrist at realistic, "
                            "small scale proportional to the wrist."),
    (r"^(СУМК|РЮКЗАК|КЛАТЧ|БАРСЕТК|ПОРТФЕЛ)", "Make the person carry the bag from the product photo naturally, at "
                                              "realistic size relative to their body."),
    (r"^(КЕПК|ШАПК|БЕЙСБОЛК|ПАНАМ)", "Put the hat from the product photo on the person's head at realistic size."),
    (r"^(РЕМ(ЕНЬ|НИ))", "Put the belt from the product photo on the person's waist at realistic width."),
]


# Одежда — классическая модель tryon-v1.6: меняет только вещь, а лицо, позу и фон оставляет как на фото.
# Обувь и аксессуары — tryon-max (v1.6 их не умеет). Вернуть всё на tryon-max: lumio-set SVS_CLOTHES_MODEL max
_CAT = [
    (r"^(КОСТЮМ|КОМПЛЕКТ|ТРОЙК|ДВОЙК|ПЛАТЬ|САРАФАН|КОМБИНЕЗОН)", "one-pieces"),
    (r"^(БРЮК|ДЖИНС|ШОРТ|ЧИНОС|КАРГО|ЮБК|ЛОСИН|ЛЕГГИН|КЮЛОТ|ПАЛАЦЦО)", "bottoms"),
    (r"^(МАЙК|БЛУЗ|ТОП|КОРСЕТ|БОДИ|ФУТБОЛК|ПОЛО|РУБАШК|СОРОЧК|ЛОНГСЛИВ|ВОДОЛАЗК|ДЖЕМПЕР|СВИТЕР|КАРДИГАН|ХУДИ|"
     r"СВИТШОТ|ТОЛСТОВК|ОЛИМПИЙК|КОФТ|ТРИКОТАЖ|ЖИЛЕТ|ПИДЖАК|ЖАКЕТ|БЛЕЙЗЕР|КУРТК|БОМБЕР|ВЕТРОВК|ПАРК|ПУХОВИК|"
     r"ПАЛЬТО|ПЛАЩ|ДУБЛ[ЁЕ]НК|ШУБ|ОВЕРШОТ|ТРЕНЧ|АНОРАК)", "tops"),
]


def tryon_request(title, model_image, product_image):
    """(модель FASHN, входные данные) для вещи с таким названием."""
    t = (title or "").strip().upper()
    if os.environ.get(f"{SHOP}_CLOTHES_MODEL", "v1.6").strip().lower() != "max":
        for rx, cat in _CAT:
            if re.match(rx, t):
                return "tryon-v1.6", {"model_image": model_image, "garment_image": product_image, "category": cat,
                                      "mode": os.environ.get(f"{SHOP}_CLOTHES_MODE", "quality"), "output_format": "jpeg"}
    return "tryon-max", {"model_image": model_image, "product_image": product_image, "prompt": tryon_prompt(title),
                         "resolution": "1k", "generation_mode": "fast", "output_format": "jpeg"}


def tryon_prompt(title):
    t = (title or "").strip().upper()
    for rx, text in _PROMPTS:
        if re.match(rx, t):
            return text + _KEEP
    return (f"Put only the {t.lower()} from the product photo on the person. Ignore the mannequin and anything worn "
            "under the garment on the mannequin. The garment must fit the person's body at realistic size." + _KEEP)


# ---------------------------------------------------------------- чистка фото вещи (/clean N)
CLEAN_PROMPT = ("Edit this product photo of a garment. Remove all hang tags, price tags, paper labels, cardboard "
                "tags and plastic tag fasteners hanging from the garment. Neatly tie any loose strings, drawstrings and "
                "belt ties in a small tidy bow or tuck them in. Keep the garment exactly the same: same shape, cut, "
                "length, color, fabric texture, stitching, buttons, zippers, prints and logos. Keep the same camera "
                "angle, framing and background. Photorealistic product photo, no new text.")


def clean_path(iid):
    return os.path.join(IDIR, f"{int(iid)}_clean.jpg")


def tryon_image(iid):
    """Фото вещи для примерки: очищенное (без бирок), если есть, иначе исходное."""
    p = clean_path(iid)
    if os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read()
    return item_image(iid)


def clean_item(chat, iid):
    it = _load()["items"].get(str(iid))
    if not it:
        return send(chat, "Нет такой вещи. Номера — /items")
    if not seedream.ready():
        return send(chat, "Seedream не подключён на сервере (SEEDREAM_API_KEY).")
    send(chat, f"Чищу фото №{iid} «{it['title']}» — убираю бирки, завязываю шнурки. Около минуты ⏳ (≈4 ₽)")

    def run():
        try:
            raw = item_image(iid)
            import cv2
            import numpy as np
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            h, w = img.shape[:2]
            k = 2048 / max(h, w)
            size = f"{int(w * k) // 16 * 16}x{int(h * k) // 16 * 16}"
            out = _seedream_sized(raw, size)
            seedream._add_used(1)
            os.makedirs(IDIR, exist_ok=True)
            with open(clean_path(iid), "wb") as f:
                f.write(out)
            send_photo_bytes(chat, out, f"Очищенное фото №{iid} «{it['title']}». Теперь оно в витрине и в примерке.",
                             kb([btn("✅ Оставить", f"keepclean:{iid}")], [btn("↩️ Вернуть оригинал", f"unclean:{iid}")]))
        except Exception as e:
            print(f"{SHOP.lower()} clean error", iid, repr(e)[:300], flush=True)
            send(chat, f"Не получилось почистить фото №{iid}: {str(e)[:200]}")
    threading.Thread(target=run, daemon=True).start()


def _seedream_sized(raw, size):
    """Тот же запрос Seedream, но с размером под пропорции фото вещи."""
    body = {"model": seedream.MODEL, "prompt": CLEAN_PROMPT, "size": size, "response_format": "b64_json",
            "output_format": "jpeg", "watermark": False, "image": data_uri(raw)}
    r = requests.post(seedream.BASE + "/images/generations", json=body, timeout=(10, 180),
                      headers={"Authorization": "Bearer " + seedream.KEY, "Content-Type": "application/json"})
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not j.get("data"):
        err = j.get("error") if isinstance(j.get("error"), dict) else {"message": j.get("error") or r.text[:200]}
        raise RuntimeError(f"Seedream {r.status_code}: {err.get('message', '')}")
    return base64.b64decode(j["data"][0]["b64_json"])


# ---------------------------------------------------------------- разбор постов
def parse(caption):
    """Вещь = пост с фото и строкой «Цена …» (любая категория). Без цены — не товар (розыгрыш, новость)."""
    text = (caption or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    m = re.search(r"Цена\s*[-–—:]?\s*([\d][\d\s.,]*)\s*(₽|руб|р\b)?", text, re.I)
    price = int(re.sub(r"\D", "", m.group(1)) or 0) if m else 0
    if not price:
        return None
    title = re.sub(r"[*_`]", "", lines[0])[:80] if lines else "Новинка"
    sz = re.search(r"Размер\w*\s*[-–—:]?\s*([^\n]+)", text, re.I)
    return {"title": title, "price": price, "sizes": (sz.group(1).strip() if sz else "")[:60]}


def money(p):
    return f"{p:,}".replace(",", " ") + " ₽" if p else "цена по запросу"


def add_item(photo_id, caption, source, admin_chat=None):
    info = parse(caption)
    if not info:
        if admin_chat:
            send(admin_chat, "Не добавил: в посте нет строки «Цена …».")
        return None
    with _lock:
        db = _load()
        for k, v in db["items"].items():         # тот же пост ещё раз (правка/повторная пересылка) — обновляем
            if v.get("source") == source or (v["title"] == info["title"] and v["price"] == info["price"]):
                # Цена и размеры не требуют повторного анализа фотографии.
                if v.get("photo") != photo_id or v.get("title") != info["title"]:
                    v.pop("ai", None)
                    v.pop("ai_meta", None)
                v.update(info, photo=photo_id, source=source)
                _save(db)
                iid = k
                for p_ in (os.path.join(IDIR, f"{int(k)}.jpg"), clean_path(k)):   # фото могло смениться
                    try:
                        os.remove(p_)
                    except OSError:
                        pass
                break
        else:
            iid = str(int(max([0] + [int(k) for k in db["items"]])) + 1)
            db["items"][iid] = {**info, "photo": photo_id, "source": source, "ts": int(time.time())}
            _save(db)
    # Сразу кэшируем фото для витрины, чтобы первый покупатель не ждал загрузки из Telegram
    threading.Thread(target=lambda: _safe(item_image, iid), daemon=True).start()
    # Уведомляем после сохранения и вне _lock: запрос OpenAI выполняется фоном.
    stylist().catalog_changed(iid)
    if admin_chat:
        send(admin_chat, f"✅ В каталоге: <b>{info['title']}</b> — {money(info['price'])} (№{iid})")
    print("svs item", iid, info["title"], flush=True)
    return iid


def on_post(msg, admin_chat=None):
    """Пост канала или пересланный админом пост. Альбом приходит частями — собираем по media_group_id."""
    photo = (msg.get("photo") or [{}])[-1].get("file_id")
    caption = msg.get("caption") or msg.get("text") or ""
    origin = msg.get("forward_origin") or {}
    src_chat = (origin.get("chat") or msg.get("chat") or {}).get("id")
    src_id = origin.get("message_id") or msg.get("message_id")
    gid = msg.get("media_group_id")
    if not gid:
        if photo:
            add_item(photo, caption, f"{src_chat}:{src_id}", admin_chat)
        return
    with _lock:
        g = _groups.setdefault(gid, {"photo": None, "first": None, "caption": "", "ts": time.time(),
                                     "admin": admin_chat, "src": f"{src_chat}:{gid}"})
        if photo and (g["first"] is None or src_id < g["first"]):
            g["photo"], g["first"] = photo, src_id     # первое фото альбома — вещь спереди
        if caption:
            g["caption"] = caption
        g["ts"] = time.time()


def flush_groups():
    while True:
        time.sleep(2)
        ready = []
        with _lock:
            for gid, g in list(_groups.items()):
                if time.time() - g["ts"] > 4:
                    ready.append(_groups.pop(gid))
        for g in ready:
            if g["photo"]:
                add_item(g["photo"], g["caption"], g["src"], g["admin"])


# ---------------------------------------------------------------- покупатель
DESCRIPTION = (f"Примерочная магазина «{BRAND}» {C['emoji']}\n\n"
               "Примерьте любую вещь из нашего канала на себя — по своему фото. "
               + ("Соберите образ, узнайте мнение стилиста и закажите с доставкой по России.\n\n" if os.environ.get("OPENAI_API_KEY")
                  else "Соберите образ и закажите с доставкой по России.\n\n") +
               "Нажмите «Старт», чтобы начать.")
SHORT_DESCRIPTION = f"Примерьте вещи «{BRAND}» на себя по своему фото — бесплатно для подписчиков канала."


def welcome(name):
    return (f"Привет{', ' + name if name else ''}! 👋\n\n"
            f"Добро пожаловать в примерочную <b>{BRAND}</b> — здесь можно примерить вещи из нашего канала "
            "на себя, не выходя из дома.\n\n"
            "<b>Как это работает:</b>\n"
            "1️⃣ Откройте витрину и загрузите своё фото в полный рост\n"
            "2️⃣ Нажмите «Примерить» на любой вещи и дождитесь готового изображения\n"
            + ("3️⃣ Соберите образ из нескольких вещей и спросите стилиста, как они сочетаются\n" if stylist().ready()
               else "3️⃣ Соберите образ из нескольких вещей и примерьте его целиком\n") +
            "4️⃣ Понравилось — оформите заказ прямо в витрине\n\n"
            f"{C['delivery']}\n"
            f"Вопросы — {ASK}\n\n"
            "<i>Фото используется только для примерки. Удалить его: /delete_photo</i>")


def user(db, uid):
    u = db["users"].setdefault(str(uid), {"day": "", "used": 0})
    today = time.strftime("%Y-%m-%d")
    if u["day"] != today:
        u["day"], u["used"] = today, 0
    return u


def show_catalog(chat, start=0):
    db = _load()
    items = sorted(db["items"].items(), key=lambda kv: -kv[1].get("ts", 0))
    if not items:
        return send(chat, "Каталог пока пуст — загляните чуть позже.")
    for iid, it in items[start:start + PAGE]:
        cap = f"<b>{it['title']}</b>\n{money(it['price'])}" + (f"\nРазмеры: {it['sizes']}" if it.get("sizes") else "")
        tg("sendPhoto", {"chat_id": chat, "photo": it["photo"], "caption": cap, "parse_mode": "HTML",
                         "reply_markup": kb([btn("👕 Примерить на себя", f"try:{iid}", style="success")],
                                            [btn("Заказать", f"ord:{iid}", style="primary")])})
    if start + PAGE < len(items):
        send(chat, f"Показано {min(start + PAGE, len(items))} из {len(items)}",
             reply_markup=kb([btn("Показать ещё", f"more:{start + PAGE}")]))


def try_on(chat, uid, iid, name):
    if not tryon_ready():
        return send(chat, "Примерочная настраивается, попробуйте чуть позже.")
    with _lock:
        db = _load()
        it = db["items"].get(iid)
        u = user(db, uid)
        if not it:
            return send(chat, "Эта вещь уже снята с продажи.")
        if not is_subscriber(uid):
            return send(chat, NOT_SUB, reply_markup=sub_markup())
        if not user_photo(uid):
            return send(chat, "Сначала пришлите своё фото в полный рост — одним сообщением, как обычное фото.")
        if uid not in ADMINS and u["used"] >= DAILY:
            return send(chat, f"Сегодня вы уже сделали {DAILY} примерки. Новые будут доступны завтра 🙌\n"
                              f"Понравилась вещь? Напишите {ASK}")
        u["used"] += 1
        _save(db)
    send(chat, f"Примеряю <b>{it['title']}</b>… создание изображения может занять пару минут ⏳")
    tg("sendChatAction", {"chat_id": chat, "action": "upload_photo"})

    def run():
        with _tryon_slots:
            try:
                t0 = time.time()
                raw, engine = run_tryon(uid, [(iid, it)])
                secs = time.time() - t0
                delivered = send_photo_bytes(chat, raw,
                                 f"<b>{it['title']}</b> — {money(it['price'])}"
                                 + (f"\nРазмеры: {it['sizes']}" if it.get("sizes") else ""),
                                 kb([btn("Заказать", f"ord:{iid}", style="success")],
                                    [btn("Примерить другую вещь", "cat", style="primary")]))
                if not delivered.get("ok"):
                    raise RuntimeError("Telegram не доставил результат примерки")
                with _lock:
                    db = _load()
                    db["stats"]["tryons"] = db["stats"].get("tryons", 0) + 1
                    _save(db)
                print(f"{SHOP.lower()} tryon", uid, iid, engine, f"{secs:.0f}s", flush=True)
            except Exception as e:
                print(f"{SHOP.lower()} tryon error", gpt_tryon.safe_error(e, SHOP), flush=True)
                with _lock:
                    db = _load()
                    user(db, uid)["used"] = max(0, user(db, uid)["used"] - 1)   # неудача не считается
                    _save(db)
                send(chat, public_tryon_error(e))
    threading.Thread(target=run, daemon=True).start()


def order(chat, frm, iid):
    db = _load()
    it = db["items"].get(iid)
    if not it:
        return send(chat, "Эта вещь уже снята с продажи.")
    who = f"@{frm['username']}" if frm.get("username") else f"<a href='tg://user?id={frm['id']}'>{frm.get('first_name', 'покупатель')}</a>"
    for a in ADMINS:
        send(a, f"🛍 Заявка из примерочной: {who}\n<b>{it['title']}</b> — {money(it['price'])} (№{iid})")
    with _lock:
        db = _load()
        db["stats"]["orders"] = db["stats"].get("orders", 0) + 1
        _save(db)
    send(chat, "Отлично! Передал менеджеру — он скоро напишет вам." + (f"\nИли напишите сами: {ASK}" if MANAGER else ""),
         reply_markup=kb([btn("Написать менеджеру", url=f"https://t.me/{MANAGER}")]) if MANAGER else None)


def _color_session(chat, **values):
    state = {"token": os.urandom(6).hex(), "ts": time.monotonic(), **values}
    _color_edits[chat] = state
    return state


def _color_buttons(state, confirm=False):
    rows = [[btn("Сохранить цвет", "colorsave:" + state["token"], style="success")]] if confirm else []
    return kb(*rows, [btn("Отмена", "colorcancel:" + state["token"])])


def _color_choose(chat):
    state = _color_session(chat, stage="choose")
    return send(chat, "Введите номер вещи из /items — например, 16.\nМожно сразу написать /color 16.",
                reply_markup=_color_buttons(state))


def _color_begin(chat, iid, it):
    state = _color_session(chat, stage="value", iid=iid, source=stylist()._source(it), title=it["title"])
    color = stylist().description(it).get("color") or "неизвестно"
    return send(chat, f"Вещь №{iid}: <b>{html.escape(it['title'])}</b>\n"
                      f"Текущий цвет: {html.escape(str(color))}.\n\n"
                      "Пришлите новый цвет отдельным сообщением, например: тёмно-коричневый.",
                reply_markup=_color_buttons(state))


def _color_reply(chat, text):
    state = _color_edits.get(chat)
    if not state:
        return False
    if time.monotonic() - state["ts"] > 900:
        _color_edits.pop(chat, None)
        send(chat, "Время редактирования истекло. Начните заново: /color номер")
        return True
    if state["stage"] == "choose":
        _description_command(chat, "/color " + text)
        return True
    if state["stage"] == "confirm":
        send(chat, "Подтвердите цвет кнопкой «Сохранить цвет» или выберите другую вещь: /color номер.",
             reply_markup=_color_buttons(state, confirm=True))
        return True
    color = " ".join(text.split())
    if not color or len(color) > 80 or re.search(r"[<>\x00-\x1f]", color) or not any(c.isalpha() for c in color):
        send(chat, "Пришлите название цвета, например: коричневый. Отмена — /cancel.")
        return True
    state.update(stage="confirm", color=color)
    send(chat, f"Вещь №{state['iid']}: <b>{html.escape(state['title'])}</b>\n"
               f"Новый цвет: <b>{html.escape(color)}</b>.", reply_markup=_color_buttons(state, confirm=True))
    return True


def _color_callback(chat, uid, data):
    if uid not in ADMINS or uid != chat or not ai_stylist():
        return
    action, token = data.split(":", 1)
    state = _color_edits.get(chat)
    if not state or state["token"] != token:
        return send(chat, "Это редактирование уже завершено. Начать новое: /color номер")
    if action == "colorcancel":
        _color_edits.pop(chat, None)
        return send(chat, "Редактирование цвета отменено.")
    if time.monotonic() - state["ts"] > 900 or state["stage"] != "confirm":
        _color_edits.pop(chat, None)
        return send(chat, "Начните редактирование заново: /color номер")
    _color_edits.pop(chat, None)           # повторный клик не выполняет повторную запись
    try:
        value = stylist().correct_color(state["iid"], state["color"], expected_source=state["source"])
    except ValueError as e:
        return send(chat, html.escape(str(e)))
    return send(chat, f"✅ Вещь №{state['iid']}: <b>{html.escape(state['title'])}</b>\n"
                      f"Цвет сохранён: {html.escape(value)}.\nОцените образ заново.")


def _description_command(chat, text):
    if not ai_stylist():
        return send(chat, "ИИ-стилист в этом магазине выключен.")
    match = re.fullmatch(r"/(passport|color)(?:@\w+)?(?:\s+([\s\S]+))?", text)
    if not match:
        return send(chat, "Команды: /passport 16 — описание; /color 16 — изменить цвет вещи №16")
    command, query = match.group(1), (match.group(2) or "").strip()
    color = None
    if command == "color":
        if "|" in query:
            query, color = (part.strip() for part in query.split("|", 1))
        elif not query:
            return _color_choose(chat)
    if not query:
        return send(chat, "Укажите номер вещи из /items, например: /passport 16")
    db = _load()
    number = re.fullmatch(r"(?:№|#)?\s*([0-9]+)(?:\s+(.+))?", query)
    if number:
        iid = str(int(number.group(1)))
        ids = [iid] if iid in db["items"] else []
        if ids and number.group(2) and " ".join(number.group(2).casefold().split()) != " ".join(db["items"][iid]["title"].casefold().split()):
            return send(chat, f"Под №{iid} записана вещь {html.escape(db['items'][iid]['title'])}.\n"
                              f"Для выбора именно её отправьте /{command} {iid}.")
    else:
        normalize = lambda value: " ".join(value.casefold().split())
        needle = normalize(query)
        exact = [i for i, it in db["items"].items() if normalize(it["title"]) == needle]
        ids = exact or [i for i, it in db["items"].items() if needle in normalize(it["title"])]
    if not ids:
        return send(chat, "Вещь не найдена. Проверьте название или номер: /items")
    if len(ids) > 1:
        rows = [f"№{i}: {html.escape(db['items'][i]['title'])}" for i in ids[:15]]
        return send(chat, "Найдено несколько вещей. Укажите нужный номер:\n" + "\n".join(rows))
    iid, st = ids[0], stylist()
    it = db["items"][iid]
    if command == "color":
        if color is None:
            return _color_begin(chat, iid, it)
        try:
            value = st.correct_color(iid, color, expected_source=st._source(it))
        except ValueError as e:
            return send(chat, html.escape(str(e)))
        return send(chat, f"✅ №{iid} {html.escape(it['title'])}\nПодтверждённый цвет: {html.escape(value)}.\n"
                          "Оцените образ заново: связанные оценки обновятся. Повторный анализ фото не нужен.")
    ai = st.description(it)
    labels = {"type": "Тип", "color": "Цвет", "material": "Фактура", "style": "Стиль",
              "season": "Сезон", "pattern": "Рисунок", "fit": "Крой", "details": "Детали"}
    rows = [f"{label}: {html.escape(str(ai.get(key) or 'неизвестно'))}" for key, label in labels.items()]
    saved = (it.get("ai") or {}).get("color") or "не определён"
    return send(chat, f"№{iid} <b>{html.escape(it['title'])}</b>\n" + "\n".join(rows) +
                      f"\n\nИсходный цвет из анализа: {html.escape(str(saved))}.\n"
                      f"Исправить цвет: /color {iid}")


def looks_report(db):
    """Образы сезона блоками (каждый — целое сообщение без разрыва HTML-тегов)."""
    import style_looks
    name = lambda i: f'{db["items"][i]["title"]} (№{i})'
    blocks, gaps = ["<b>Образы сезона осень–зима 2026/27</b> — по ним работает стилист."], []
    for lk in style_looks.looks(SHOP):
        present = [i for i in lk["set"] + lk["extra"] if i in db["items"]]   # удалённые вещи просто не показываем
        if any(i in db["items"] for i in lk["set"]):
            items = ", ".join(name(i) for i in present)
            blocks.append(f"<b>{html.escape(lk['name'])}</b> — {html.escape(lk['palette'])}\n{html.escape(items)}")
        if lk.get("missing"):
            gaps.append(f"• {html.escape(lk['name'])}: {html.escape(lk['missing'])}")
    if gaps:
        blocks.append("<b>Чего не хватает в каталоге</b> (для закупки):\n" + "\n".join(gaps))
    messages, cur = [], ""
    for block in blocks:
        if cur and len(cur) + len(block) > 3800:
            messages.append(cur)
            cur = ""
        cur = (cur + "\n\n" + block) if cur else block
    return messages + ([cur] if cur else [])


def admin_cmd(chat, text):
    db = _load()
    if text.startswith("/looks"):
        if not ai_stylist():
            return send(chat, "ИИ-стилист в этом магазине выключен.")
        for message in looks_report(db):                    # лимит сообщения Telegram — 4096 символов
            send(chat, message)
        return
    if text.startswith(("/passport", "/color")):
        return _description_command(chat, text)
    if text.startswith("/items"):
        rows = [f"№{k} {v['title']} — {money(v['price'])}" for k, v in db["items"].items()]
        return send(chat, "\n".join(rows[-50:]) or "Каталог пуст.")
    if text.startswith("/del"):
        n = re.sub(r"\D", "", text)
        with _lock:
            db = _load()
            it = db["items"].pop(n, None)
            _save(db)
        return send(chat, f"Удалил №{n}: {it['title']}" if it else "Нет такой вещи. Номера — /items")
    if text.startswith("/clean"):
        n = re.sub(r"\D", "", text)
        if not n:
            return send(chat, "Напишите номер вещи: /clean 14 — уберу бирки и завяжу шнурки на фото (≈4 ₽, один раз). "
                              "Номера — /items")
        return clean_item(chat, n)
    if text.startswith("/stats"):
        return send(chat, f"Вещей: {len(db['items'])}\nПокупателей: {len(db['users'])}\n"
                          f"Одобрено заявок в канал: {db['stats'].get('joins', 0)}\n"
                          f"Примерок: {db['stats'].get('tryons', 0)}\nЗаявок: {db['stats'].get('orders', 0)}")
    return False


def on_join_request(req):
    """Заявка на вступление в канал: одобряем сразу и шлём приветствие с витриной."""
    chat, frm = req.get("chat") or {}, req.get("from") or {}
    cid = channel_id()
    if not AUTO_APPROVE or (cid and str(chat.get("id")) != str(cid)):
        return
    r = tg("approveChatJoinRequest", {"chat_id": chat["id"], "user_id": frm["id"]})
    if not r.get("ok"):
        print("svs approve failed", r.get("description"), flush=True)
        return
    _subs_cache.pop(frm["id"], None)
    with _lock:
        db = _load()
        db["stats"]["joins"] = db["stats"].get("joins", 0) + 1
        _save(db)
    if JOIN_WELCOME and req.get("user_chat_id"):
        send(req["user_chat_id"], f"Добро пожаловать в <b>{BRAND}</b>, {frm.get('first_name') or 'друг'}! {C['emoji']}\n\n"
                                  "Заявка одобрена. А ещё у нас есть примерочная: загрузите своё фото и примерьте "
                                  "любую вещь из канала на себя — бесплатно для подписчиков.",
             reply_markup=kb([app_btn("👕 Примерить на себя")]))


def on_update(upd):
    if upd.get("chat_join_request"):
        return on_join_request(upd["chat_join_request"])
    if upd.get("channel_post") or upd.get("edited_channel_post"):
        return on_post(upd.get("channel_post") or upd.get("edited_channel_post"))
    cq = upd.get("callback_query")
    if cq:
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
        chat, data = cq["message"]["chat"]["id"], cq.get("data", "")
        if data.startswith(("colorsave:", "colorcancel:")):
            return _color_callback(chat, cq["from"]["id"], data)
        if cq["from"]["id"] in ADMINS:
            _color_edits.pop(chat, None)
        if data == "cat":
            return show_catalog(chat)
        if data == "noop":
            return
        if data.startswith(("keepclean:", "unclean:")) and cq["from"]["id"] in ADMINS:
            n = data.split(":", 1)[1]
            tg("editMessageReplyMarkup", {"chat_id": chat, "message_id": cq["message"]["message_id"],
                                          "reply_markup": {"inline_keyboard": []}})
            if data.startswith("keepclean:"):
                return send(chat, f"✅ Готово: №{n} в витрине и примерке — с очищенным фото.")
            try:
                os.remove(clean_path(n))
            except OSError:
                pass
            return send(chat, f"↩️ Вернул исходное фото для №{n}.")
        if data.startswith("more:"):
            return show_catalog(chat, int(data[5:]))
        if data.startswith("try:"):
            return try_on(chat, cq["from"]["id"], data[4:], cq["from"].get("first_name"))
        if data.startswith("ord:"):
            return order(chat, cq["from"], data[4:])
        return
    msg = upd.get("message")
    if not msg or msg["chat"].get("type") != "private":
        return
    chat, uid, text = msg["chat"]["id"], msg["from"]["id"], (msg.get("text") or "").strip()
    is_admin = uid in ADMINS
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
    forwarded = msg.get("forward_origin") or msg.get("forward_from_chat")
    if is_admin and (text.startswith("/") or forwarded):
        _color_edits.pop(chat, None)
    if is_admin and command == "/cancel":
        return send(chat, "Редактирование отменено.")
    if text == "/" or command == "/help":
        return send(chat, command_help(is_admin))
    if is_admin and (msg.get("forward_origin") or msg.get("forward_from_chat")):
        return on_post(msg, admin_chat=chat)
    if is_admin and text.startswith(("/items", "/del", "/stats", "/clean", "/passport", "/color", "/looks")):
        return admin_cmd(chat, text)
    if is_admin and chat in _color_edits:
        if not text:
            return send(chat, "Пришлите номер или цвет текстом. Отмена — /cancel.")
        if _color_reply(chat, text):
            return
    if text.startswith("/start"):
        return send(chat, welcome((msg.get("from") or {}).get("first_name", "")),
                    reply_markup=kb([app_btn("🛍 Открыть витрину")],
                                    [btn("Каталог в чате", "cat")] + ([btn("Менеджер", url=f"https://t.me/{MANAGER}")] if MANAGER else [])))
    if text.startswith(("/catalog", "Каталог")):
        return show_catalog(chat)
    if text.startswith("/delete_photo"):
        drop_user_photo(uid)
        return send(chat, "Ваше фото удалено.")
    if msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").startswith("image/"):
        fid = (msg["photo"][-1]["file_id"] if msg.get("photo") else msg["document"]["file_id"])
        raw = file_bytes(fid)
        save_user_photo(uid, raw)
        import svs_face
        warn = svs_face.check_photo(raw)
        if warn:
            send(chat, "⚠️ " + warn + "\nФото сохранено, но лучше заменить — просто пришлите новое.")
        with _lock:
            db = _load()
            user(db, uid)
            _save(db)
        return send(chat, "Фото сохранено ✅ Теперь выберите вещь и нажмите «Примерить на себя».",
                    reply_markup=kb([btn("👕 Открыть каталог", "cat", style="success")]))
    send(chat, "Пришлите своё фото в полный рост или откройте каталог.",
         reply_markup=kb([btn("👕 Открыть каталог", "cat", style="success")]))


def command_menu(admin=False):
    commands = [
        {"command": "catalog", "description": "Открыть каталог и примерочную"},
        {"command": "start", "description": "Как пользоваться ботом"},
        {"command": "delete_photo", "description": "Удалить моё фото для примерки"},
        {"command": "help", "description": "Все доступные команды и примеры"},
    ]
    if admin:
        commands.extend([
            {"command": "items", "description": "Список товаров и их номера"},
            {"command": "stats", "description": "Статистика магазина"},
            {"command": "clean", "description": "Очистить фото товара: /clean номер"},
            {"command": "del", "description": "Удалить товар: /del номер"},
        ])
        if ai_stylist():
            commands.extend([
                {"command": "looks", "description": "Образы сезона и чего не хватает в каталоге"},
                {"command": "passport", "description": "Описание вещи по номеру: /passport 16"},
                {"command": "color", "description": "Изменить цвет вещи по номеру: /color 16"},
                {"command": "cancel", "description": "Отменить текущее редактирование"},
            ])
    return commands


def command_help(admin=False):
    rows = [f"/{it['command']} — {html.escape(it['description'])}" for it in command_menu(admin)]
    if admin:
        rows.extend(["", "Примеры:", "/clean 14", "/del 14"])
        if ai_stylist():
            rows.extend(["/looks — образы сезона из нашего каталога", "/passport 16", "/color 16 — выбрать вещь и ввести цвет следующим сообщением",
                         "/color 16 | коричневый — сохранить сразу", "/cancel — отменить редактирование"])
    return "Доступные команды:\n\n" + "\n".join(rows)


def setup_commands():
    # Список при вводе «/» задаётся отдельно для покупателей и личного чата каждого админа.
    scopes = [({"type": "default"}, False), ({"type": "all_private_chats"}, False)]
    scopes.extend(({"type": "chat", "chat_id": uid}, True) for uid in sorted(ADMINS) if uid > 0)
    for scope, admin in scopes:
        # Обновляем и общий, и русскоязычный список, чтобы старый ru-список не перекрывал новый.
        for language in ("", "ru"):
            tg("setMyCommands", {"commands": command_menu(admin), "scope": scope, "language_code": language})


def poll():
    tg("deleteWebhook", {"drop_pending_updates": False})
    tg("setMyDescription", {"description": DESCRIPTION})
    tg("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})
    tg("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "Витрина", "web_app": {"url": APP_URL}}})
    setup_commands()
    me = tg("getMe").get("result") or {}
    global BOT_USERNAME
    BOT_USERNAME = me.get("username") or BOT_USERNAME
    print(f"{SHOP.lower()} polling as", me.get("username"), flush=True)
    offset = None
    while True:
        q = {"timeout": 50, "allowed_updates": ["message", "callback_query", "channel_post", "edited_channel_post",
                                                "chat_join_request"]}
        if offset is not None:
            q["offset"] = offset
        res = tg("getUpdates", q, timeout=(10, 65))
        if not res.get("ok"):
            time.sleep(5)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                on_update(upd)
            except Exception as e:
                print("svs update error", repr(e)[:300], flush=True)


def start():
    if not TOKEN:
        print(f"{SHOP.lower()} bot off ({SHOP}_BOT_TOKEN)", flush=True)
        return
    threading.Thread(target=poll, daemon=True).start()
    threading.Thread(target=flush_groups, daemon=True).start()
    stylist().start()
