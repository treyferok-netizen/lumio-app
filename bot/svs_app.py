"""Мини-приложение «Сила в Стиле»: витрина, корзина, примерка вещи или образа (цепочка FASHN), заказ.

Адрес: https://<сервер>/svs/   (кнопка «Витрина» в @sila_v_stile_bot)
API (всё, что про покупателя, — с заголовком X-Init-Data = Telegram.WebApp.initData):
  GET  /svs/api/items            каталог
  GET  /svs/img/<id>.jpg         фото вещи
  GET  /svs/api/me               есть ли фото, сколько примерок осталось
  GET  /svs/api/myphoto?init=…   своё фото (только владельцу)
  POST /svs/api/photo            загрузить фото (тело — JPEG)
  DELETE /svs/api/photo          удалить фото
  POST /svs/api/tryon            {"items": [id, …]} — до 3 вещей, примерка по цепочке
  GET  /svs/api/job/<id>         статус примерки
  POST /svs/api/order            {"items": [{"id", "size"}], "comment"}
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse

import fashn
import svs_bot as bot

HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "svs_app.html")
MAX_ITEMS = 3
_jobs = {}

# порядок надевания: низ → верх → верхняя одежда → обувь/аксессуары
ORDER = [
    r"^(БРЮК|ДЖИНС|ШОРТ|ЧИНОС|КАРГО|ЮБК)",
    r"^(ФУТБОЛК|ПОЛО|РУБАШК|СОРОЧК|ЛОНГСЛИВ|ВОДОЛАЗК|ДЖЕМПЕР|СВИТЕР|КАРДИГАН|ХУДИ|СВИТШОТ|ТОЛСТОВК|ОЛИМПИЙК|КОФТ|ТРИКОТАЖ|КОСТЮМ)",
    r"^(ЖИЛЕТ|ПИДЖАК|ЖАКЕТ|БЛЕЙЗЕР|КУРТК|БОМБЕР|ВЕТРОВК|ПАРК|ПУХОВИК|ПАЛЬТО|ПЛАЩ|ДУБЛЁНК|ДУБЛЕНК|ШУБ|ОВЕРШОТ|ТРЕНЧ|АНОРАК)",
]


def layer(title):
    t = title.upper()
    for i, rx in enumerate(ORDER):
        if re.match(rx, t):
            return i
    return len(ORDER)          # обувь, сумки, аксессуары — последними


def category(title):
    w = re.split(r"[\s,]+", title.strip().upper())[0] if title.strip() else ""
    return w.capitalize() if w else "Другое"


def size_list(s):
    m = re.match(r"^\s*(\d{2})\s*[-–—]\s*(\d{2})\s*$", s or "")
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 30 <= a < b <= 70:
            return [str(x) for x in range(a, b + 1, 2)]
    parts = [p.strip() for p in re.split(r"[,/;]", s or "") if p.strip()]
    if 1 < len(parts) <= 12 and all(len(p) <= 5 for p in parts):
        return parts
    return []


def auth(init_data):
    if not init_data or not bot.TOKEN:
        return None
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hmac.new(b"WebAppData", bot.TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    if time.time() - int(pairs.get("auth_date", "0") or 0) > 7 * 86400:
        return None
    try:
        return json.loads(pairs.get("user", "{}")) or None
    except ValueError:
        return None


def _js(code, obj):
    return code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode()


def left_today(uid):
    if uid in bot.ADMINS:
        return 99
    with bot._lock:
        db = bot._load()
        u = bot.user(db, uid)
        bot._save(db)
    return max(0, bot.DAILY - u["used"])


def items():
    db = bot._load()
    out = []
    for iid, it in sorted(db["items"].items(), key=lambda kv: -kv[1].get("ts", 0)):
        out.append({"id": iid, "title": it["title"], "price": it["price"], "sizes": it.get("sizes", ""),
                    "size_list": size_list(it.get("sizes", "")), "cat": category(it["title"]),
                    "img": f"/svs/img/{iid}.jpg"})
    return out


def start_tryon(u, ids):
    uid = int(u["id"])
    db = bot._load()
    ids = [str(i) for i in ids if str(i) in db["items"]][:MAX_ITEMS]
    if not ids:
        return _js(400, {"ok": False, "error": "Выберите вещи для примерки"})
    if not bot.user_photo(uid):
        return _js(400, {"ok": False, "error": "nophoto"})
    if not fashn.ready():
        return _js(503, {"ok": False, "error": "Примерочная настраивается, попробуйте позже"})
    with bot._lock:
        db = bot._load()
        usr = bot.user(db, uid)
        if uid not in bot.ADMINS and usr["used"] + len(ids) > bot.DAILY:
            left = max(0, bot.DAILY - usr["used"])
            return _js(429, {"ok": False, "error": f"На сегодня осталось примерок: {left}. Новые — завтра."})
        usr["used"] += len(ids)
        bot._save(db)
    its = sorted(((i, db["items"][i]) for i in ids), key=lambda kv: layer(kv[1]["title"]))
    jid = secrets.token_hex(8)
    _jobs[jid] = {"uid": uid, "status": "running", "step": 0, "total": len(its), "ts": time.time(),
                  "titles": [it["title"] for _, it in its]}
    threading.Thread(target=_run, args=(jid, uid, its), daemon=True).start()
    return _js(200, {"ok": True, "job": jid, "total": len(its)})


def _run(jid, uid, its):
    job = _jobs[jid]
    done = 0
    try:
        with bot._tryon_slots:
            model = bot.data_uri(bot.user_photo(uid))
            for n, (iid, it) in enumerate(its, 1):
                job["step"] = n
                prompt = (f"Put only the {it['title'].lower()} from the product photo on the person. Ignore the mannequin "
                          "and anything worn under the garment on the mannequin. Keep the person's face, body, pose, "
                          "background and all other clothes they are wearing unchanged.")
                outs, _, _ = fashn.run("tryon-max", {"model_image": model,
                                                     "product_image": bot.data_uri(bot.item_image(iid)),
                                                     "prompt": prompt, "resolution": "1k",
                                                     "generation_mode": "fast", "output_format": "jpeg"})
                if not outs:
                    raise RuntimeError("пустой ответ FASHN")
                model = outs[0]          # следующая вещь надевается на результат
                done += 1
        job.update(status="done", result=model)
        total = sum(it["price"] for _, it in its)
        names = "\n".join(f"• {it['title']} — {bot.money(it['price'])}" for _, it in its)
        bot.tg("sendPhoto", {"chat_id": uid, "photo": model, "parse_mode": "HTML",
                             "caption": f"Ваша примерка ✨\n{names}" + (f"\n\nИтого: {bot.money(total)}" if len(its) > 1 else ""),
                             "reply_markup": bot.kb([bot.app_btn("🛍 Вернуться в витрину")])}, timeout=(5, 60))
        with bot._lock:
            db = bot._load()
            db["stats"]["tryons"] = db["stats"].get("tryons", 0) + done
            _bump_outfits(db, len(its))
            bot._save(db)
    except Exception as e:
        print("svs app tryon error", repr(e)[:300], flush=True)
        job.update(status="error", error="Не получилось примерить. Попробуйте другое фото: в полный рост, лицом к камере.")
        with bot._lock:                      # неудачные шаги не списываем
            db = bot._load()
            usr = bot.user(db, uid)
            usr["used"] = max(0, usr["used"] - (len(its) - done))
            bot._save(db)


def _bump_outfits(db, n):
    if n > 1:
        db["stats"]["outfits"] = db["stats"].get("outfits", 0) + 1


def order(u, data):
    db = bot._load()
    rows, total = [], 0
    for x in (data.get("items") or [])[:20]:
        it = db["items"].get(str(x.get("id")))
        if not it:
            continue
        size = str(x.get("size") or "")[:10]
        rows.append(f"• {it['title']}" + (f", размер {size}" if size else "") + f" — {bot.money(it['price'])}")
        total += it["price"]
    if not rows:
        return _js(400, {"ok": False, "error": "Корзина пуста"})
    who = f"@{u['username']}" if u.get("username") else f"<a href='tg://user?id={u['id']}'>{u.get('first_name', 'покупатель')}</a>"
    comment = str(data.get("comment") or "").strip()[:500]
    text = (f"🛍 Заказ из витрины: {who}\n" + "\n".join(rows) + f"\n\nИтого: {bot.money(total)}"
            + (f"\nКомментарий: {comment}" if comment else ""))
    for a in bot.ADMINS:
        bot.send(a, text)
    bot.send(int(u["id"]), "Заказ принят ✅\n" + "\n".join(rows) + f"\n\nИтого: {bot.money(total)}\n\n"
             f"Менеджер скоро напишет вам. Или напишите сами: @{bot.MANAGER}")
    with bot._lock:
        db = bot._load()
        db["stats"]["orders"] = db["stats"].get("orders", 0) + 1
        bot._save(db)
    return _js(200, {"ok": True})


def handle(method, path, query, headers, body):
    """Возвращает (код, content-type, байты)."""
    qs = dict(urllib.parse.parse_qsl(query or ""))
    if method == "GET" and path in ("/svs", "/svs/"):
        with open(HTML, "rb") as f:
            return 200, "text/html; charset=utf-8", f.read()
    m = re.match(r"^/svs/img/(\d+)\.jpg$", path)
    if method == "GET" and m:
        raw = bot.item_image(m.group(1))
        return (200, "image/jpeg", raw) if raw else (404, "text/plain", b"")
    if method == "GET" and path == "/svs/api/items":
        return _js(200, {"ok": True, "items": items(), "manager": bot.MANAGER})
    u = auth(headers.get("X-Init-Data") or qs.get("init"))
    if not u:
        return _js(401, {"ok": False, "error": "Откройте витрину из бота @sila_v_stile_bot"})
    uid = int(u["id"])
    if method == "GET" and path == "/svs/api/me":
        return _js(200, {"ok": True, "has_photo": bool(bot.user_photo(uid)), "left": left_today(uid),
                         "max_items": MAX_ITEMS})
    if method == "GET" and path == "/svs/api/myphoto":
        raw = bot.user_photo(uid)
        return (200, "image/jpeg", raw) if raw else (404, "text/plain", b"")
    if path == "/svs/api/photo":
        if method == "DELETE":
            bot.drop_user_photo(uid)
            return _js(200, {"ok": True})
        if method == "POST":
            if not body or len(body) > 12 * 1024 * 1024 or body[:2] != b"\xff\xd8":
                return _js(400, {"ok": False, "error": "Нужно фото (JPEG)"})
            bot.save_user_photo(uid, body)
            return _js(200, {"ok": True})
    if method == "POST" and path == "/svs/api/tryon":
        return start_tryon(u, (json.loads(body or b"{}").get("items") or []))
    m = re.match(r"^/svs/api/job/([0-9a-f]+)$", path)
    if method == "GET" and m:
        job = _jobs.get(m.group(1))
        if not job or job["uid"] != uid:
            return _js(404, {"ok": False, "error": "Примерка не найдена"})
        return _js(200, {"ok": True, **{k: job.get(k) for k in ("status", "step", "total", "result", "error", "titles")}})
    if method == "POST" and path == "/svs/api/order":
        return order(u, json.loads(body or b"{}"))
    return _js(404, {"ok": False, "error": "not found"})
