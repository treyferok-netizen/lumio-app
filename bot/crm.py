"""Мини-CRM Lumio: заявки, статусы и воронка — в файлах на сервере (без внешних сервисов).

orders.json  — список заявок со статусами и историей
events.jsonl — события воронки: start (пришёл в бота), gate (показали подписку), sub (подписался)
"""
import csv
import io
import json
import os
import threading
import time

DIR = os.path.dirname(os.environ.get("STATE_FILE", "/var/lib/lumio-bot/state.json")) or "."
ORDERS = os.path.join(DIR, "orders.json")
EVENTS = os.path.join(DIR, "events.jsonl")
_lock = threading.Lock()

STATUSES = [("new", "🆕 Новая"), ("sketch", "🎨 Эскиз"), ("paid", "💳 Оплачено"),
            ("work", "🎬 В работе"), ("done", "✅ Сдано"), ("lost", "❌ Отказ")]
LABEL = dict(STATUSES)
OPEN = ("new", "sketch", "paid", "work")


def _load():
    try:
        with open(ORDERS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save(rows):
    os.makedirs(DIR, exist_ok=True)
    tmp = ORDERS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ORDERS)


def add_order(o, tg_user, sub):
    """Сохраняет заявку, возвращает её номер."""
    with _lock:
        rows = _load()
        num = max([r["id"] for r in rows] or [0]) + 1
        now = time.time()
        rows.append({
            "id": num, "ts": now, "status": "new", "history": [[now, "new"]],
            "name": o.get("name", ""), "contact": o.get("contact", ""), "occasion": o.get("occasion", ""),
            "params": (o.get("calc") or {}).get("text", ""), "heroes": o.get("heroes", ""),
            "date": o.get("date", ""), "story": (o.get("story") or "")[:1500], "photos": o.get("photos") or 0,
            "source": o.get("source", ""), "uid": (tg_user or {}).get("id"),
            "username": (tg_user or {}).get("username", ""), "subscribed": sub, "admin_msg": None,
        })
        _save(rows)
        return num


def set_admin_msg(num, message_id):
    with _lock:
        rows = _load()
        for r in rows:
            if r["id"] == num:
                r["admin_msg"] = message_id
        _save(rows)


def set_status(num, status):
    with _lock:
        rows = _load()
        for r in rows:
            if r["id"] == num:
                if r["status"] != status:
                    r["status"] = status
                    r["history"].append([time.time(), status])
                _save(rows)
                return r
    return None


def get(num):
    return next((r for r in _load() if r["id"] == num), None)


def recent_open_by_uid(uid, hours=24):
    """Открытая заявка этого клиента, заведённая за последние N часов (чтобы не плодить дубли)."""
    since = time.time() - hours * 3600
    rows = [r for r in _load() if r.get("uid") == uid and r["status"] in OPEN and r["ts"] >= since]
    return rows[-1] if rows else None


def open_orders():
    return [r for r in _load() if r["status"] in OPEN]


def log_event(kind, uid):
    try:
        os.makedirs(DIR, exist_ok=True)
        with _lock, open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "e": kind, "uid": uid}) + "\n")
    except OSError as e:
        print("event log error", e)


def stats(days=30):
    since = time.time() - days * 86400
    people = {"start": set(), "gate": set(), "sub": set()}
    try:
        with open(EVENTS, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("ts", 0) >= since and ev.get("e") in people:
                    people[ev["e"]].add(ev.get("uid"))
    except OSError:
        pass
    orders = [r for r in _load() if r["ts"] >= since]
    reached = lambda st: sum(1 for r in orders if any(h[1] == st for h in r["history"]))  # noqa: E731
    return {
        "days": days,
        "start": len(people["start"]), "gate": len(people["gate"]), "sub": len(people["sub"]),
        "orders": len(orders), "sketch": reached("sketch"),
        "paid": sum(1 for r in orders if any(h[1] in ("paid", "work", "done") for h in r["history"])),
        "done": reached("done"), "lost": sum(1 for r in orders if r["status"] == "lost"),
        "open": sum(1 for r in orders if r["status"] in OPEN),
    }


def export_csv():
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["№", "Дата", "Статус", "Имя", "Контакт", "Telegram", "Повод", "Параметры", "Герои",
                "К дате", "Фото", "Источник", "Подписчик", "История"])
    for r in _load():
        w.writerow([r["id"], time.strftime("%d.%m.%Y %H:%M", time.localtime(r["ts"])), LABEL.get(r["status"], r["status"]),
                    r["name"], r["contact"], ("@" + r["username"]) if r.get("username") else (r.get("uid") or ""),
                    r["occasion"], r["params"], r["heroes"], r["date"], r["photos"], r["source"],
                    {True: "да", False: "нет"}.get(r.get("subscribed"), "?"), r["story"]])
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM — чтобы Excel открыл кириллицу
