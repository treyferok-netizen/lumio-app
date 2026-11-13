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
Пост без цены или с аксессуаром (сумка, ремень, обувь…) пропускается.
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

TOKEN = os.environ.get("SVS_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}/"
MANAGER = os.environ.get("SVS_MANAGER", "romanov_roman_svs").lstrip("@")
DAILY = int(os.environ.get("SVS_DAILY_FREE", "3") or 3)
ADMINS = {int(x) for x in re.findall(r"-?\d+", os.environ.get("SVS_ADMIN_IDS") or os.environ.get("ADMIN_CHAT_ID") or "")}
STORE = os.path.join(seedream.DIR, "svs.json")
PAGE = 5
SKIP = re.compile(r"^(СУМК|РЮКЗАК|РЕМ(ЕНЬ|НИ)|КОШЕЛ|ПОРТМОНЕ|ОЧК|ЧАС[ЫО]|ДУХИ|ПАРФЮМ|ОБУВЬ|КРОССОВ|КЕД|ТУФЛ|ЛОФЕР|"
                  r"МОКАСИН|БОТИН|САПОГ|СЛИП|ШЛЁП|ШЛЕП|ТАПК|КЕПК|ШАПК|БЕЙСБОЛК|ПАНАМ|ШАРФ|ПЕРЧАТ|ГАЛСТУК|ЗАПОНК|БРАСЛЕТ|"
                  r"КОЛЬЦ|ЦЕП|ЗОНТ|ЧЕМОДАН|КЛАТЧ|БАРСЕТК|ВИЗИТНИЦ|ОБЛОЖК|БРЕЛОК|НОСК)", re.I)

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
    return {"token": bool(TOKEN), "fashn": fashn.ready(), "admins": sorted(ADMINS), "items": len(db["items"]),
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


def btn(text, data=None, url=None, style=None):
    b = {"text": text}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = data
    if style:
        b["style"] = style
    return b


def kb(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


def send(chat, text, **kw):
    return tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", **kw})


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


# ---------------------------------------------------------------- разбор постов
def parse(caption):
    text = (caption or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    m = re.search(r"Цена\s*[-–—:]?\s*([\d][\d\s.,]*)\s*(₽|руб|р\b)?", text, re.I)
    if not m:
        return None
    price = int(re.sub(r"\D", "", m.group(1)) or 0)
    if not price:
        return None
    title = re.sub(r"[*_`]", "", lines[0])[:80]
    if SKIP.match(title):
        return {"skip": "аксессуар", "title": title}
    sz = re.search(r"Размер\w*\s*[-–—:]?\s*([^\n]+)", text, re.I)
    return {"title": title, "price": price, "sizes": (sz.group(1).strip() if sz else "")[:60]}


def money(p):
    return f"{p:,}".replace(",", " ") + " ₽"


def add_item(photo_id, caption, source, admin_chat=None):
    info = parse(caption)
    if not info or info.get("skip"):
        if admin_chat:
            why = "это аксессуар — примерка только для одежды" if info else "в тексте нет строки «Цена …»"
            send(admin_chat, f"Пропустил{(': ' + info['title']) if info else ''} — {why}.")
        return None
    with _lock:
        db = _load()
        for k, v in db["items"].items():         # тот же пост ещё раз (правка/повторная пересылка) — обновляем
            if v.get("source") == source or (v["title"] == info["title"] and v["price"] == info["price"]):
                v.update(info, photo=photo_id, source=source)
                _save(db)
                iid = k
                break
        else:
            iid = str(int(max([0] + [int(k) for k in db["items"]])) + 1)
            db["items"][iid] = {**info, "photo": photo_id, "source": source, "ts": int(time.time())}
            _save(db)
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
WELCOME = ("👋 Это примерочная <b>Сила в Стиле</b>.\n\n"
           "1. Пришлите своё фото в полный рост — лучше при хорошем свете, руки чуть в стороны.\n"
           "2. Откройте каталог и нажмите «Примерить» на любой вещи.\n"
           "Через 15–20 секунд увидите себя в ней.\n\n"
           "Фото используется только для примерки. Удалить его: /delete_photo")


def user(db, uid):
    u = db["users"].setdefault(str(uid), {"photo": None, "day": "", "used": 0})
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
        if not u["photo"]:
            return send(chat, "Сначала пришлите своё фото в полный рост — одним сообщением, как обычное фото.")
        if uid not in ADMINS and u["used"] >= DAILY:
            return send(chat, f"Сегодня вы уже сделали {DAILY} примерки. Новые будут доступны завтра 🙌\n"
                              f"Понравилась вещь? Напишите @{MANAGER}")
        u["used"] += 1
        _save(db)
        photo = u["photo"]
    if not fashn.ready():
        return send(chat, "Примерочная настраивается, попробуйте чуть позже.")
    send(chat, f"Примеряю <b>{it['title']}</b>… это займёт 15–20 секунд ⏳")
    tg("sendChatAction", {"chat_id": chat, "action": "upload_photo"})

    def run():
        with _tryon_slots:
            try:
                prompt = (f"Put only the {it['title'].lower()} from the product photo on the person. Ignore the mannequin "
                          "and anything worn under the garment on the mannequin. Keep the person's face, body, pose, "
                          "background and their other clothes unchanged.")
                outs, used, secs = fashn.run("tryon-max", {
                    "model_image": data_uri(file_bytes(photo)), "product_image": data_uri(file_bytes(it["photo"])),
                    "prompt": prompt, "resolution": "1k", "generation_mode": "fast", "output_format": "jpeg"})
                if not outs:
                    raise RuntimeError("пустой ответ")
                tg("sendPhoto", {"chat_id": chat, "photo": outs[0], "parse_mode": "HTML",
                                 "caption": f"<b>{it['title']}</b> — {money(it['price'])}"
                                            + (f"\nРазмеры: {it['sizes']}" if it.get("sizes") else ""),
                                 "reply_markup": kb([btn("Заказать", f"ord:{iid}", style="success")],
                                                    [btn("Примерить другую вещь", "cat", style="primary")])},
                   timeout=(5, 60))
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
                           "без сильного наклона. Если не выйдет — напишите @" + MANAGER)
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
    send(chat, f"Отлично! Передал менеджеру — он скоро напишет вам.\nИли напишите сами: @{MANAGER}",
         reply_markup=kb([btn("Написать менеджеру", url=f"https://t.me/{MANAGER}")]))


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
                          f"Примерок: {db['stats'].get('tryons', 0)}\nЗаявок: {db['stats'].get('orders', 0)}")
    return False


def on_update(upd):
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
        return send(chat, WELCOME, reply_markup=kb([btn("👕 Открыть каталог", "cat", style="success")]))
    if text.startswith(("/catalog", "Каталог")):
        return show_catalog(chat)
    if text.startswith("/delete_photo"):
        with _lock:
            db = _load()
            user(db, uid)["photo"] = None
            _save(db)
        return send(chat, "Ваше фото удалено.")
    if msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").startswith("image/"):
        fid = (msg["photo"][-1]["file_id"] if msg.get("photo") else msg["document"]["file_id"])
        with _lock:
            db = _load()
            user(db, uid)["photo"] = fid
            _save(db)
        return send(chat, "Фото сохранено ✅ Теперь выберите вещь и нажмите «Примерить на себя».",
                    reply_markup=kb([btn("👕 Открыть каталог", "cat", style="success")]))
    send(chat, "Пришлите своё фото в полный рост или откройте каталог.",
         reply_markup=kb([btn("👕 Открыть каталог", "cat", style="success")]))


def poll():
    tg("deleteWebhook", {"drop_pending_updates": False})
    tg("setMyCommands", {"commands": [{"command": "catalog", "description": "Каталог и примерка"},
                                      {"command": "start", "description": "Как пользоваться"},
                                      {"command": "delete_photo", "description": "Удалить моё фото"}]})
    me = tg("getMe").get("result") or {}
    print("svs polling as", me.get("username"), flush=True)
    offset = None
    while True:
        q = {"timeout": 50, "allowed_updates": ["message", "callback_query", "channel_post", "edited_channel_post"]}
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
        print("svs bot off (SVS_BOT_TOKEN)", flush=True)
        return
    threading.Thread(target=poll, daemon=True).start()
    threading.Thread(target=flush_groups, daemon=True).start()
