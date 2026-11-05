"""Бот Lumio во ВКонтакте: сообщения сообщества vk.com/lumio.studio.

Работает через Bots Long Poll API (сервер сам ходит к ВК, вебхук не нужен).
Нужен ключ доступа сообщества с правами «сообщения сообщества» и «управление»:
    read -s K; lumio-set VK_TOKEN "$K"
Включается сам, если задан VK_TOKEN. Уведомления о клиентах приходят админу в Telegram
(тот же ADMIN_CHAT_ID), а ответить клиенту можно прямо в сообщениях сообщества ВК —
пока вы переписываетесь, бот этому клиенту молчит.

Метки ссылок: vk.me/lumio.studio?ref=sketch — сразу эскиз, ref=order — заказ,
ref=prices — цены, ref=examples — примеры, ref=ad001 — рекламная метка.
"""
import json
import os
import random
import re
import threading
import time

import requests

import crm
import index

TOKEN = os.environ.get("VK_TOKEN", "")
GROUP = int(os.environ.get("VK_GROUP_ID", "242968024"))
V = "5.199"
STATE_FILE = os.environ.get("VK_STATE_FILE", "/var/lib/lumio-bot/vk_state.json")
HUMAN_HOURS = float(os.environ.get("HUMAN_HOURS", "24"))
EXAMPLES = [x.strip() for x in os.environ.get("VK_EXAMPLES", "video-242968024_456239017,video-242968024_456239019,video-242968024_456239018").split(",") if x.strip()]  # video-242968024_456239017,...
VIDEOS_URL = f"https://vk.com/videos-{GROUP}"
SKETCH_FREE_FOR = os.environ.get("VK_SKETCH_FREE", "members")  # members — только подписчикам сообщества; all — всем

_lock = threading.Lock()


# ------------------------------------------------------------------ ВК API
def api(method, **params):
    params.update(access_token=TOKEN, v=V)
    try:
        r = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=(5, 20))
        d = r.json()
    except Exception as e:
        print("vk api error", method, repr(e), flush=True)
        return None
    if "error" in d:
        print("vk api error", method, d["error"].get("error_code"), d["error"].get("error_msg"), flush=True)
        return None
    return d.get("response")


def btn(label, cmd, color="secondary"):
    return {"action": {"type": "text", "label": label, "payload": json.dumps({"cmd": cmd})}, "color": color}


def link_btn(label, url):
    return {"action": {"type": "open_link", "label": label, "link": url}}


MAIN_KB = {"one_time": False, "buttons": [
    [btn("🎁 Бесплатный эскиз", "sketch", "positive"), btn("🎬 Заказать мультфильм", "order", "primary")],
    [btn("📺 Примеры", "examples"), btn("💰 Цены и сроки", "prices")],
    [btn("💬 Позвать менеджера", "human")],
]}


def kb(rows, one_time=False):
    return json.dumps({"one_time": one_time, "buttons": rows}, ensure_ascii=False)


def say(uid, text, keyboard=None, attachment=None):
    p = {"peer_id": uid, "message": text, "random_id": random.randint(1, 2 ** 31 - 1), "dont_parse_links": 1}
    p["keyboard"] = keyboard if keyboard is not None else json.dumps(MAIN_KB, ensure_ascii=False)
    if attachment:
        p["attachment"] = attachment
    return api("messages.send", **p)


# ------------------------------------------------------------------ состояние клиентов
def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def get(uid):
    with _lock:
        return _load().get(str(uid), {})


def put(uid, **kw):
    with _lock:
        st = _load()
        u = st.setdefault(str(uid), {})
        for k, v in kw.items():
            if v is None:
                u.pop(k, None)
            else:
                u[k] = v
        _save(st)
        return u


def human(uid):
    return time.time() < get(uid).get("human_until", 0)


def set_human(uid, on=True):
    put(uid, human_until=time.time() + HUMAN_HOURS * 3600 if on else 0, step=None, form=None)


_names = {}


def name_of(uid):
    if uid not in _names:
        r = api("users.get", user_ids=uid) or [{}]
        _names[uid] = " ".join(x for x in [r[0].get("first_name"), r[0].get("last_name")] if x) or f"id{uid}"
    return _names[uid]


def who(uid):
    return (f'<a href="https://vk.com/id{uid}">{index.esc(name_of(uid))}</a> · '
            f'<a href="https://vk.com/gim{GROUP}?sel={uid}">диалог ВК</a>')


def notify(text, photos=None):
    if not index.ADMIN:
        return
    index.send(index.ADMIN, "🟦 <b>ВКонтакте</b>\n" + text)
    if photos:
        media = [{"type": "photo", "media": u} for u in photos[:10]]
        index.tg("sendMediaGroup", {"chat_id": index.ADMIN, "media": media})


def photo_urls(msg):
    out = []
    for a in msg.get("attachments") or []:
        if a.get("type") == "photo":
            sizes = (a["photo"].get("sizes") or [])
            if sizes:
                out.append(max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))["url"])
        elif a.get("type") == "doc" and (a["doc"].get("ext") or "").lower() in ("jpg", "jpeg", "png", "heic", "webp"):
            out.append(a["doc"].get("url"))
    return [u for u in out if u]


def is_member(uid):
    r = api("groups.isMember", group_id=GROUP, user_id=uid)
    return bool(r) if r is not None else True


# ------------------------------------------------------------------ тексты
GREETING = ("Здравствуйте! Это Lumio ✨ — студия мультфильмов по вашей истории.\n\n"
            "Главными героями становятся ваши близкие: дети, родители, бабушки и дедушки, вторая половинка. "
            "Рисуем героев по фото и придумываем сказку под ваш повод.\n\n"
            "🎬 Мультфильм — от 3 900 ₽\n"
            "🎁 Эскиз героя — бесплатно для подписчиков сообщества\n"
            "⏳ Новогодние заказы принимаем до 10 декабря\n\n"
            "Выберите кнопку ниже или просто напишите вопрос.")
PRICES = ("💰 Цены\n"
          "• 30 секунд — 3 900 ₽ (около 4 дней)\n"
          "• 1 минута — 6 900 ₽ (около 5 дней)\n"
          "• 2 минуты — 11 900 ₽ (около 7 дней)\n"
          "• 3 минуты — 15 900 ₽ (около 9 дней)\n\n"
          "Сценарий, герои (1–2), озвучка героев и музыка уже включены.\n"
          "Дополнительно: 3–4 героя +1 500 ₽, 5 и больше +3 000 ₽, диктор +1 500 ₽, "
          "песня с вашими именами +3 000 ₽, питомцы +900 ₽, срочно за 3 дня +30%.\n\n"
          "Нажмите «🎬 Заказать мультфильм» — соберём заявку за пару минут.")
EXAMPLES_TEXT = ("📺 Наши мультфильмы — в разделе «Видео» сообщества:\n" + VIDEOS_URL +
                 "\n\nЗахотите такой же про своих близких — нажмите «🎬 Заказать мультфильм».")

OCCASIONS = ["Новый год", "День рождения", "День матери", "Свадьба / годовщина", "Другой повод"]
DURATIONS = [("30 секунд — 3 900 ₽", "30 секунд"), ("1 минута — 6 900 ₽", "1 минута"),
             ("2 минуты — 11 900 ₽", "2 минуты"), ("3 минуты — 15 900 ₽", "3 минуты")]
CANCEL = [btn("✖️ Отмена", "cancel", "negative")]
SKIP = btn("Пропустить", "skip")
DONE_PHOTOS = btn("✅ Готово, фото отправил(а)", "photos_done", "positive")


def ask(uid, step, text, rows=None):
    put(uid, step=step)
    say(uid, text, keyboard=kb((rows or []) + [CANCEL]))


# ------------------------------------------------------------------ сценарии
def start_sketch(uid):
    if SKETCH_FREE_FOR == "members" and not is_member(uid):
        say(uid, "🎁 Эскиз героя бесплатно — для подписчиков нашего сообщества.\n\n"
                 "Подпишитесь на сообщество (кнопка «Подписаться» на странице vk.com/lumio.studio) "
                 "и нажмите «🎁 Бесплатный эскиз» ещё раз.",
            keyboard=kb([[link_btn("Открыть сообщество", "https://vk.com/lumio.studio")],
                         [btn("🎁 Я подписался(ась)", "sketch", "positive")],
                         [btn("⬅️ В меню", "menu")]]))
        crm.log_event("gate", f"vk{uid}")
        return
    put(uid, form={"kind": "sketch", "photos": []})
    ask(uid, "sk_who", "🎁 Отлично! Нарисуем эскиз героя по фото — бесплатно и до оплаты.\n\n"
                       "Кто будет главным героем? Напишите имя и кто это: например, «Аня, 5 лет», "
                       "«мама Лена» или «мы с мужем».")


def start_order(uid):
    put(uid, form={"kind": "order", "photos": []})
    ask(uid, "or_occ", "🎬 Соберём заявку на мультфильм — это пара минут.\n\nК какому поводу мультфильм?",
        [[btn(o, "occ:" + o)] for o in OCCASIONS])


def finish(uid, form):
    kind = form.get("kind")
    photos = form.get("photos") or []
    o = {"name": name_of(uid), "contact": f"vk.com/id{uid}", "occasion": form.get("occasion", ""),
         "heroes": form.get("heroes", ""), "story": form.get("story", ""), "photos": len(photos),
         "source": "ВК: эскиз" if kind == "sketch" else "ВК: заказ",
         "calc": {"text": form.get("duration", "")}}
    num = crm.add_order(o, {"id": f"vk{uid}", "username": ""}, None)
    lines = [f"{'🎁 Заявка на эскиз' if kind == 'sketch' else '🎬 Заявка на мультфильм'} #{num} — {who(uid)}"]
    for label, key in [("Повод", "occasion"), ("Длительность", "duration"), ("Герои", "heroes"), ("История", "story")]:
        if form.get(key):
            lines.append(f"<b>{label}:</b> {index.esc(form[key])}")
    lines.append(f"<b>Фото:</b> {len(photos)}")
    lines.append("\nОтветьте клиенту в сообщениях сообщества ВК — бот ему пока не пишет.")
    notify("\n".join(lines), photos)
    set_human(uid)
    if kind == "sketch":
        say(uid, "Спасибо! Фото и детали передали художнику 🎨\n"
                 "Напишем вам здесь, как только эскиз будет готов, и уточним пару деталей.")
    else:
        say(uid, f"Спасибо! Заявка №{num} принята 🤍\n"
                 "Менеджер Lumio напишет вам здесь, обсудит детали сценария и расскажет про оплату.")


def handle_step(uid, step, text, cmd, photos):
    form = get(uid).get("form") or {}
    if step == "sk_who":
        if not text:
            return ask(uid, step, "Напишите, пожалуйста, имя героя и кто это 🙂")
        form["heroes"] = text
        put(uid, form=form)
        return ask(uid, "sk_photo", "Теперь пришлите 2–3 фото героя: лицо крупно, при хорошем свете. "
                                    "Когда отправите, нажмите «✅ Готово».", [[DONE_PHOTOS]])
    if step in ("sk_photo", "or_photo"):
        if photos:
            form.setdefault("photos", []).extend(photos)
            put(uid, form=form)
            if cmd != "photos_done":
                return say(uid, f"Получили фото: {len(form['photos'])} 👍 Можно добавить ещё или нажать «✅ Готово».",
                           keyboard=kb([[DONE_PHOTOS], CANCEL]))
        if cmd in ("photos_done", "skip"):
            if step == "sk_photo" and not form.get("photos"):
                return ask(uid, step, "Чтобы нарисовать эскиз, нужно хотя бы одно фото героя 📸", [[DONE_PHOTOS]])
            put(uid, step=None, form=None)
            return finish(uid, form)
        return ask(uid, step, "Пришлите фото героя или нажмите кнопку ниже.",
                   [[DONE_PHOTOS]] + ([[SKIP]] if step == "or_photo" else []))
    if step == "or_occ":
        occ = cmd[4:] if cmd.startswith("occ:") else text
        if not occ:
            return ask(uid, step, "Выберите повод кнопкой или напишите своими словами.",
                       [[btn(o, "occ:" + o)] for o in OCCASIONS])
        form["occasion"] = occ
        put(uid, form=form)
        return ask(uid, "or_dur", "Какой длины мультфильм?", [[btn(lbl, "dur:" + v)] for lbl, v in DURATIONS])
    if step == "or_dur":
        dur = cmd[4:] if cmd.startswith("dur:") else text
        if not dur:
            return ask(uid, step, "Выберите длительность кнопкой.", [[btn(lbl, "dur:" + v)] for lbl, v in DURATIONS])
        form["duration"] = dur
        put(uid, form=form)
        return ask(uid, "or_heroes", "Кто будут героями? Например: «дочка Аня 5 лет, мама и папа».")
    if step == "or_heroes":
        if not text:
            return ask(uid, step, "Напишите, кто будут героями мультфильма 🙂")
        form["heroes"] = text
        put(uid, form=form)
        return ask(uid, "or_story", "Расскажите коротко историю или пару тёплых деталей: любимые словечки, "
                                    "семейные привычки, важные моменты. Из них напишем сценарий.", [[SKIP]])
    if step == "or_story":
        if text and cmd != "skip":
            form["story"] = text
        put(uid, form=form)
        return ask(uid, "or_photo", "Пришлите 2–3 фото героев (лица крупно, при хорошем свете) "
                                    "или нажмите «Пропустить» — фото можно прислать позже.",
                   [[DONE_PHOTOS], [SKIP]])
    put(uid, step=None, form=None)
    say(uid, GREETING)


def on_message(msg):
    uid = msg.get("from_id")
    if not uid or uid < 0 or msg.get("peer_id") != uid:  # только личные сообщения от людей
        return
    text = (msg.get("text") or "").strip()
    try:
        cmd = json.loads(msg.get("payload") or "{}").get("cmd", "")
    except (ValueError, AttributeError):
        cmd = ""
    if cmd == "start":
        cmd = ""
    ref = (msg.get("ref") or "").strip()
    photos = photo_urls(msg)
    u = get(uid)
    first = not u.get("seen")
    if first:
        put(uid, seen=time.time())
        crm.log_event("start", f"vk{uid}", ("vk:" + ref) if ref else "vk")
        notify(f"👋 Новый человек в сообщениях: {who(uid)}" + (f"\nМетка: {index.esc(ref)}" if ref else ""))
    elif text and not cmd:
        crm.log_event("msg", f"vk{uid}")

    # менеджер уже ведёт клиента — просто пересылаем
    if human(uid) and cmd not in ("menu", "cancel"):
        notify(f"👤 Клиент пишет: {who(uid)}" + (f"\n\n{index.esc(text)}" if text else ""), photos)
        return

    if cmd in ("cancel", "menu") or text.lower() in ("начать", "старт", "start", "меню", "/start"):
        set_human(uid, False)
        return say(uid, GREETING)
    ref_cmd = {"sketch": "sketch", "order": "order", "prices": "prices", "examples": "examples"}.get(ref)
    if first and ref_cmd and not cmd:
        cmd = ref_cmd
    if cmd == "sketch":
        return start_sketch(uid)
    if cmd == "order":
        return start_order(uid)
    if cmd == "prices":
        return say(uid, PRICES)
    if cmd == "examples":
        return say(uid, EXAMPLES_TEXT, attachment=",".join(EXAMPLES[:3]) or None)
    if cmd == "human":
        set_human(uid)
        say(uid, "Позвали менеджера Lumio 🤍 Он ответит здесь же, обычно в течение часа. "
                 "Пока можно написать подробности.")
        return notify(f"🙋 <b>Клиент просит живого человека</b>: {who(uid)}")

    step = u.get("step")
    if step:
        return handle_step(uid, step, text, cmd, photos)
    if first:
        say(uid, GREETING)
        if not text and not photos:
            return
    if photos:  # фото без сценария = хочет эскиз
        put(uid, form={"kind": "sketch", "photos": photos, "heroes": text})
        put(uid, step=None)
        return finish(uid, get(uid).get("form") or {"kind": "sketch", "photos": photos, "heroes": text})
    if not text or first and text.lower() in ("привет", "здравствуйте", "добрый день"):
        return
    if index.HUMAN_RE.search(text):
        set_human(uid)
        say(uid, "Позвали менеджера Lumio 🤍 Он ответит здесь же, обычно в течение часа.")
        return notify(f"🙋 <b>Клиент просит живого человека</b>: {who(uid)}\n\n{index.esc(text)}")
    answer, need = index.ask_gpt(text, None)
    if answer:
        answer = re.sub(r"«?Открыть Lumio»?", "«🎬 Заказать мультфильм»", answer)
        say(uid, answer)
    else:
        say(uid, "Спасибо за вопрос! Передали его команде Lumio — ответим совсем скоро 🤍")
    if need:
        set_human(uid)
    notify(("⚠️ <b>Нужен ваш ответ</b>" if need else "💬 Вопрос клиента") + f": {who(uid)}\n\n"
           f"<b>Вопрос:</b> {index.esc(text)}" + (f"\n\n<b>Бот ответил:</b> {index.esc(answer)}" if answer else ""))


def on_reply(msg):
    """Исходящее сообщение сообщества. Если его написал админ руками — бот замолкает для этого клиента."""
    if msg.get("admin_author_id") and msg.get("peer_id", 0) > 0:
        set_human(msg["peer_id"])


def loop():
    if not TOKEN:
        print("vk bot off (VK_TOKEN)", flush=True)
        return
    srv = None
    while True:
        if srv is None:
            srv = api("groups.getLongPollServer", group_id=GROUP)
            if not srv:
                time.sleep(30)
                continue
            print("vk long poll ok", flush=True)
        try:
            r = requests.get(srv["server"], params={"act": "a_check", "key": srv["key"], "ts": srv["ts"], "wait": 25},
                             timeout=(10, 35)).json()
        except Exception as e:
            print("vk poll error", repr(e), flush=True)
            time.sleep(5)
            continue
        if "failed" in r:
            if r["failed"] == 1:
                srv["ts"] = r.get("ts", srv["ts"])
            else:
                srv = None
            continue
        srv["ts"] = r.get("ts", srv["ts"])
        for upd in r.get("updates", []):
            try:
                obj = upd.get("object") or {}
                if upd.get("type") == "message_new":
                    on_message(obj.get("message") or obj)
                elif upd.get("type") == "message_reply":
                    on_reply(obj)
            except Exception as e:
                print("vk update error", repr(e), flush=True)


def start():
    threading.Thread(target=loop, daemon=True).start()
