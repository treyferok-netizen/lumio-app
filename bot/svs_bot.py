"""Бот-примерочная «Сила в Стиле» (@sila_v_stile_bot): каталог из канала + примерка FASHN.

Настройки (lumio-set ИМЯ ЗНАЧЕНИЕ):
  SVS_BOT_TOKEN     токен бота из @BotFather (без него бот выключен)
  SVS_ADMIN_IDS     id админов через запятую (по умолчанию ADMIN_CHAT_ID)
  SVS_MANAGER       username для заказов, по умолчанию romanov_roman_svs
  SVS_DAILY_FREE    бесплатных примерок в день на человека, по умолчанию 3
  FASHN_API_KEY     ключ FASHN (см. fashn.py)

Каталог пополняется сам:
  • бот — администратор канала: каждый пост с фото и строкой «Цена …» становится вещью;
  • старые посты админ пересылает боту в личку — разбираются так же.
Добавляется каждый пост с фото и ценой (одежда, сумки, обувь — всё). Без цены — пропуск. Убрать: /del N.
Фото покупателя хранится только как file_id Telegram; удалить — /delete_photo.
"""
import base64
import json
import os
import re
import threading
import time

import requests

import fashn
import seedream
import shops

SHOP = globals().get("SHOP", "SVS")          # второй магазин загружается тем же кодом (shops.py)
C = shops.cfg(SHOP)
BRAND = C["name"]


def _env(key, default=""):
    return os.environ.get(f"{SHOP}_{key}", default)


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
            "token": bool(TOKEN), "fashn": fashn.ready(), "face_restore": face, "admins": sorted(ADMINS), "items": len(db["items"]),
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
                try:
                    os.remove(os.path.join(IDIR, f"{int(k)}.jpg"))   # фото могло смениться
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
               "Примерьте любую вещь из нашего канала на себя — по своему фото, за 20 секунд. "
               + ("Соберите образ, узнайте мнение стилиста и закажите с доставкой по России.\n\n" if SHOP == "SVS"
                  else "Соберите образ и закажите с доставкой по России.\n\n") +
               "Нажмите «Старт», чтобы начать.")
SHORT_DESCRIPTION = f"Примерьте вещи «{BRAND}» на себя по своему фото — бесплатно для подписчиков канала."


def welcome(name):
    return (f"Привет{', ' + name if name else ''}! 👋\n\n"
            f"Добро пожаловать в примерочную <b>{BRAND}</b> — здесь можно примерить вещи из нашего канала "
            "на себя, не выходя из дома.\n\n"
            "<b>Как это работает:</b>\n"
            "1️⃣ Откройте витрину и загрузите своё фото в полный рост\n"
            "2️⃣ Нажмите «Примерить» на любой вещи — через 20 секунд увидите себя в ней\n"
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
    if not fashn.ready():
        return send(chat, "Примерочная настраивается, попробуйте чуть позже.")
    send(chat, f"Примеряю <b>{it['title']}</b>… это займёт 15–20 секунд ⏳")
    tg("sendChatAction", {"chat_id": chat, "action": "upload_photo"})

    def run():
        with _tryon_slots:
            try:
                outs, used, secs = fashn.run(*tryon_request(it["title"], data_uri(user_photo(uid)),
                                                            data_uri(item_image(iid))))
                if not outs:
                    raise RuntimeError("пустой ответ")
                send_photo_bytes(chat, finish_tryon(uid, outs[0], [it["title"]]),
                                 f"<b>{it['title']}</b> — {money(it['price'])}"
                                 + (f"\nРазмеры: {it['sizes']}" if it.get("sizes") else ""),
                                 kb([btn("Заказать", f"ord:{iid}", style="success")],
                                    [btn("Примерить другую вещь", "cat", style="primary")]))
                with _lock:
                    db = _load()
                    db["stats"]["tryons"] = db["stats"].get("tryons", 0) + 1
                    _save(db)
                print("svs tryon", uid, iid, f"{secs:.0f}s", flush=True)
            except Exception as e:
                print("svs tryon error", repr(e)[:300], flush=True)
                with _lock:
                    db = _load()
                    user(db, uid)["used"] = max(0, user(db, uid)["used"] - 1)   # неудача не считается
                    _save(db)
                send(chat, "Не получилось примерить 😔 Попробуйте другое фото: в полный рост, лицом к камере, "
                           "без сильного наклона. Если не выйдет — напишите " + ASK)
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


def admin_cmd(chat, text):
    db = _load()
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
        if data == "cat":
            return show_catalog(chat)
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
    if is_admin and (msg.get("forward_origin") or msg.get("forward_from_chat")):
        return on_post(msg, admin_chat=chat)
    if is_admin and text.startswith(("/items", "/del", "/stats")):
        return admin_cmd(chat, text)
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


def poll():
    tg("deleteWebhook", {"drop_pending_updates": False})
    tg("setMyDescription", {"description": DESCRIPTION})
    tg("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})
    tg("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "Витрина", "web_app": {"url": APP_URL}}})
    tg("setMyCommands", {"commands": [{"command": "catalog", "description": "Каталог и примерка"},
                                      {"command": "start", "description": "Как пользоваться"},
                                      {"command": "delete_photo", "description": "Удалить моё фото"}]})
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

