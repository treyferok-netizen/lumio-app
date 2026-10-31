"""Рекламные ссылки Lumio (Telegram Ads) и кабинет менеджера с аналитикой по ним.

Ссылка:  https://t.me/LumioMultBot?start=ad001  …  ad100
Бот при /start ad001 записывает событие start с меткой и «привязывает» человека к первой рекламной
ссылке, по которой он пришёл. Дальше всё, что человек делает (подписка, приложение, вопросы, заявки,
оплата), считается в пользу этой ссылки.

Файлы (рядом с orders.json):
  ad_links.json — список ссылок: название, кампания, расход в ₽
  attrib.json   — кто когда впервые пришёл в бота и по какой рекламной ссылке
  cabinet_key   — ключ входа в кабинет (создаётся сам; новая ссылка для входа — /cabinet_new в боте)

Кабинет: https://<сервер>/cabinet?k=<ключ>  (ключ ставит cookie, дальше ссылка без ключа).
"""
import calendar
import csv
import hmac
import io
import json
import os
import re
import secrets
import threading
import time

import crm

DIR = crm.DIR
LINKS = os.path.join(DIR, "ad_links.json")
ATTR = os.path.join(DIR, "attrib.json")
KEYF = os.path.join(DIR, "cabinet_key")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "LumioMultBot").lstrip("@")
PUBLIC_BASE = (os.environ.get("PUBLIC_BASE") or "https://193-32-178-96.sslip.io").rstrip("/")
TZ = float(os.environ.get("CABINET_TZ", "10"))      # часы относительно UTC: Владивосток +10
CODE_RE = re.compile(r"^ad\d{3,4}$")
_lock = threading.Lock()


# ---------------------------------------------------------------- ссылки
def link_url(code):
    return f"https://t.me/{BOT_USERNAME}?start={code}"


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    os.makedirs(DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def links():
    """{code: {"name", "campaign", "spend", "created"}}; при первом запуске создаёт ad001…ad100."""
    with _lock:
        data = _read(LINKS, None)
        if not data:
            now = time.time()
            data = {f"ad{i:03d}": {"name": "", "campaign": "", "spend": 0, "created": now} for i in range(1, 101)}
            _write(LINKS, data)
        return data


def add_links(n):
    n = max(1, min(int(n), 100))
    with _lock:
        data = _read(LINKS, {})
        nums = [int(c[2:]) for c in data if CODE_RE.match(c)] or [0]
        start = max(nums) + 1
        now = time.time()
        for i in range(start, start + n):
            data[f"ad{i:03d}"] = {"name": "", "campaign": "", "spend": 0, "created": now}
        _write(LINKS, data)
        return data


def update_link(code, fields):
    with _lock:
        data = _read(LINKS, {})
        if code not in data:
            return None
        row = data[code]
        if "name" in fields:
            row["name"] = str(fields["name"])[:120]
        if "campaign" in fields:
            row["campaign"] = str(fields["campaign"])[:80]
        if "spend" in fields:
            try:
                row["spend"] = max(0.0, round(float(str(fields["spend"]).replace(",", ".").replace(" ", "") or 0), 2))
            except ValueError:
                pass
        _write(LINKS, data)
        return row


def is_ad(code):
    return bool(code) and code in links()


# ---------------------------------------------------------------- кто откуда пришёл
def _attr():
    data = _read(ATTR, None)
    if data is None:  # первый запуск: все, кто уже был в боте, — «старые», без рекламы
        data = {}
        try:
            with open(crm.EVENTS, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    k = str(ev.get("uid"))
                    if ev.get("uid") is not None and k not in data:
                        data[k] = {"first": ev.get("ts", 0)}
        except OSError:
            pass
        for r in crm._load():
            k = str(r.get("uid"))
            if r.get("uid") is not None and k not in data:
                data[k] = {"first": r["ts"]}
        _write(ATTR, data)
    return data


def touch(user, code=None):
    """Вызывается на каждый /start. Возвращает True, если человек в боте впервые."""
    uid = user.get("id")
    if uid is None:
        return False
    k = str(uid)
    now = time.time()
    with _lock:
        data = _attr()
        row = data.get(k)
        new = row is None
        if new:
            row = data[k] = {"first": now}
        if code and not row.get("ad"):
            row["ad"], row["ad_ts"], row["ad_new"] = code, now, new
        row["un"] = user.get("username") or row.get("un", "")
        row["name"] = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or row.get("name", "")
        if new or code:
            _write(ATTR, data)
        return new


# ---------------------------------------------------------------- аналитика
def _events():
    out = []
    try:
        with open(crm.EVENTS, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def day_of(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts + TZ * 3600))


def _utc_ts(day):
    return calendar.timegm(time.strptime(day, "%Y-%m-%d"))


PAID = ("paid", "work", "done")


def report(day_from=None, day_to=None):
    """Сводка по всем ссылкам за период (по дате перехода). day_* — 'YYYY-MM-DD' включительно."""
    t0 = _utc_ts(day_from) - TZ * 3600 if day_from else 0
    t1 = _utc_ts(day_to) - TZ * 3600 + 86400 if day_to else time.time() + 1
    lk = links()
    with _lock:
        at = dict(_attr())
    evs = _events()
    orders = crm._load()

    blank = lambda: {"clicks": set(), "new": set(), "people": set(), "sub": set(), "app": set(),  # noqa: E731
                     "msg": set(), "orders": 0, "paid": 0, "done": 0}
    agg = {c: blank() for c in lk}
    daily = {}

    # переходы по ссылке за период (уникальные люди; в т.ч. те, кто уже бывал в боте)
    for ev in evs:
        c = ev.get("src")
        if ev.get("e") == "start" and c in agg and t0 <= ev.get("ts", 0) < t1:
            agg[c]["clicks"].add(ev.get("uid"))
            d = daily.setdefault(day_of(ev["ts"]), {"clicks": set(), "new": set(), "orders": 0})
            d["clicks"].add((c, ev.get("uid")))

    # люди, привязанные к ссылке, с датой первого перехода в периоде
    cohort = {}
    for k, row in at.items():
        c = row.get("ad")
        if c in agg and t0 <= row.get("ad_ts", 0) < t1:
            cohort[k] = c
            agg[c]["people"].add(k)
            if row.get("ad_new"):
                agg[c]["new"].add(k)
                daily.setdefault(day_of(row["ad_ts"]), {"clicks": set(), "new": set(), "orders": 0})["new"].add(k)

    kinds = {"sub": "sub", "app": "app", "msg": "msg"}
    for ev in evs:
        k = str(ev.get("uid"))
        if k in cohort and ev.get("e") in kinds and ev.get("ts", 0) >= at[k]["ad_ts"]:
            agg[cohort[k]][kinds[ev["e"]]].add(k)

    per_user = {}
    for r in orders:
        k = str(r.get("uid"))
        if k in cohort and r["ts"] >= at[k]["ad_ts"] - 60:
            a = agg[cohort[k]]
            a["orders"] += 1
            hist = [h[1] for h in r.get("history", [])]
            if any(h in PAID for h in hist):
                a["paid"] += 1
            if "done" in hist:
                a["done"] += 1
            per_user.setdefault(k, []).append(r)
            d = daily.setdefault(day_of(r["ts"]), {"clicks": set(), "new": set(), "orders": 0})
            d["orders"] += 1

    rows = []
    for c, meta in lk.items():
        a = agg[c]
        rows.append({"code": c, "url": link_url(c), "name": meta.get("name", ""), "campaign": meta.get("campaign", ""),
                     "spend": meta.get("spend", 0), "clicks": len(a["clicks"]), "new": len(a["new"]),
                     "people": len(a["people"]), "sub": len(a["sub"]), "app": len(a["app"]), "msg": len(a["msg"]),
                     "orders": a["orders"], "paid": a["paid"], "done": a["done"]})
    days = [{"day": d, "clicks": len(v["clicks"]), "new": len(v["new"]), "orders": v["orders"]}
            for d, v in sorted(daily.items())]
    return {"from": day_from, "to": day_to, "now": time.time(), "rows": rows, "days": days,
            "bot": BOT_USERNAME}


def people(code, day_from=None, day_to=None):
    """Кто пришёл по ссылке: имя, дата, что успел сделать, заявки и статусы."""
    t0 = _utc_ts(day_from) - TZ * 3600 if day_from else 0
    t1 = _utc_ts(day_to) - TZ * 3600 + 86400 if day_to else time.time() + 1
    with _lock:
        at = dict(_attr())
    ks = {k: r for k, r in at.items() if r.get("ad") == code and t0 <= r.get("ad_ts", 0) < t1}
    acts = {k: set() for k in ks}
    for ev in _events():
        k = str(ev.get("uid"))
        if k in ks and ev.get("ts", 0) >= ks[k]["ad_ts"] and ev.get("e") in ("sub", "app", "msg"):
            acts[k].add(ev["e"])
    ords = {}
    for r in crm._load():
        k = str(r.get("uid"))
        if k in ks and r["ts"] >= ks[k]["ad_ts"] - 60:
            ords.setdefault(k, []).append({"id": r["id"], "status": crm.LABEL.get(r["status"], r["status"])})
    out = []
    for k, r in sorted(ks.items(), key=lambda x: -x[1]["ad_ts"]):
        out.append({"uid": k, "username": r.get("un", ""), "name": r.get("name", ""), "ts": r["ad_ts"],
                    "new": bool(r.get("ad_new")), "acts": sorted(acts[k]), "orders": ords.get(k, [])})
    return out


def links_csv():
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Метка", "Ссылка", "Название", "Кампания"])
    for c, m in links().items():
        w.writerow([c, link_url(c), m.get("name", ""), m.get("campaign", "")])
    return ("﻿" + out.getvalue()).encode("utf-8")


def report_csv(day_from=None, day_to=None):
    rep = report(day_from, day_to)
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Метка", "Название", "Кампания", "Ссылка", "Переходы", "Новые", "Подписались", "Открыли приложение",
                "Написали", "Заявки", "Оплатили", "Сдано", "Расход, ₽", "₽ за нового", "₽ за заявку", "₽ за оплату"])
    per = lambda s, n: round(s / n) if n and s else ""  # noqa: E731
    for r in rep["rows"]:
        w.writerow([r["code"], r["name"], r["campaign"], r["url"], r["clicks"], r["new"], r["sub"], r["app"], r["msg"],
                    r["orders"], r["paid"], r["done"], r["spend"] or "", per(r["spend"], r["new"]),
                    per(r["spend"], r["orders"]), per(r["spend"], r["paid"])])
    return ("﻿" + out.getvalue()).encode("utf-8")


# ---------------------------------------------------------------- доступ
def key(rotate=False):
    with _lock:
        k = None if rotate else (open(KEYF).read().strip() if os.path.isfile(KEYF) else None)
        if not k:
            k = secrets.token_urlsafe(18)
            os.makedirs(DIR, exist_ok=True)
            with open(KEYF, "w") as f:
                f.write(k)
            os.chmod(KEYF, 0o600)
        return k


def check(given):
    return bool(given) and hmac.compare_digest(str(given), key())


def cabinet_url():
    return f"{PUBLIC_BASE}/cabinet?k={key()}"
