"""Lumio bot — one Yandex Cloud Function, three entry points (chosen by ?src=):

  ?src=tg      Telegram webhook: client questions (answered by YandexGPT from knowledge.md),
               admin replies relayed back to clients.
  ?src=order   Order form from the Mini App (multipart: order JSON + photos) -> sent to the admin.
  ?src=setup   One-time: registers the webhook and bot commands. Needs &key=<WEBHOOK_SECRET>.

Environment variables:
  BOT_TOKEN        token of @LumioMultBot (from @BotFather)
  WEBHOOK_SECRET   any long random string; protects the webhook and the setup link
  PUBLIC_URL       this function's public link, e.g. https://functions.yandexcloud.net/d4e...
  ADMIN_CHAT_ID    your Telegram chat id (send /myid to the bot to learn it)
  FOLDER_ID        Yandex Cloud folder id (for YandexGPT)
  GPT_MODEL        optional, default "yandexgpt-lite/latest"
  YANDEX_API_KEY   optional; if empty, the function's service account token is used
  MINIAPP_URL      optional, default https://t.me/LumioMultBot/app
  CHANNEL          optional, default @lumioRF — подписчикам канала эскиз героя бесплатно
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import threading
import time
import urllib.parse
from email.parser import BytesParser
from email.policy import default as email_policy

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SECRET = os.environ.get("WEBHOOK_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
ADMIN = os.environ.get("ADMIN_CHAT_ID", "").strip()
FOLDER = os.environ.get("FOLDER_ID", "")
MODEL = os.environ.get("GPT_MODEL", "yandexgpt-lite/latest")
API_KEY = os.environ.get("YANDEX_API_KEY", "")
MINIAPP_URL = os.environ.get("MINIAPP_URL", "https://t.me/LumioMultBot/app")
WEBAPP_PAGE = os.environ.get("WEBAPP_PAGE", "https://treyferok-netizen.github.io/lumio-app/")
CHANNEL = os.environ.get("CHANNEL", "@lumioRF")  # бот должен быть администратором канала (без прав) — иначе подписку не проверить
CHANNEL_URL = "https://t.me/" + CHANNEL.lstrip("@")

API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
HERE = os.path.dirname(os.path.abspath(__file__))
HANDOFF = "[ПЕРЕДАТЬ]"
TAG_RE = re.compile(r"#c(-?\d+)")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


# ---------------------------------------------------------------- telegram helpers
def tg(method, data=None, files=None, timeout=(5, 12)):
    try:
        r = requests.post(API + method, data=data if files else None, json=None if files else data,
                          files=files, timeout=timeout)
        return r.json()
    except Exception as e:  # network problems must not crash the webhook
        err = f"{type(e).__name__}: {e}".replace(BOT_TOKEN, "<token>") if BOT_TOKEN else f"{type(e).__name__}: {e}"
        print("tg error", method, err)
        return {"ok": False, "error": err[:300]}


def diagnose(context):
    """Quick self-check, readable in a browser: env vars, Telegram reachability, YandexGPT."""
    import socket
    import time
    out = {"env": {"BOT_TOKEN": bool(BOT_TOKEN), "WEBHOOK_SECRET": bool(SECRET), "PUBLIC_URL": PUBLIC_URL or None,
                   "ADMIN_CHAT_ID": ADMIN or None, "FOLDER_ID": FOLDER or None,
                   "service_account_token": bool(iam_headers(context))}}
    t = time.time()
    try:
        out["telegram_dns"] = socket.gethostbyname("api.telegram.org")
    except Exception as e:
        out["telegram_dns"] = f"{type(e).__name__}: {e}"
    me = tg("getMe", {}, timeout=(9, 6))
    out["telegram_getMe"] = {"ok": me.get("ok"), "bot": (me.get("result") or {}).get("username"),
                             "error": me.get("error") or me.get("description"), "sec": round(time.time() - t, 1)}
    t = time.time()
    ans, need = ask_gpt("Сколько стоит мультфильм на 1 минуту?", context)
    out["yandexgpt"] = {"answer": ans, "handoff": need, "sec": round(time.time() - t, 1)}
    return out


def send(chat_id, text, **kw):
    return tg("sendMessage", {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
                              "disable_web_page_preview": True, **kw})


def esc(s):
    return html.escape(str(s or ""))


def who(user):
    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "Клиент"
    un = user.get("username")
    return f"{esc(name)}" + (f" (@{esc(un)})" if un else "")


def open_app_markup():
    return {"inline_keyboard": [[{"text": "✨ Открыть Lumio", "web_app": {"url": WEBAPP_PAGE}}]]}


def sketch_app_markup():
    url = WEBAPP_PAGE + ("&" if "?" in WEBAPP_PAGE else "?") + "src=sketch#sketch"
    return {"inline_keyboard": [[{"text": "📸 Загрузить фото для эскиза", "web_app": {"url": url}}]]}


# ---------------------------------------------------------------- channel subscription (bonus: priority)
def is_subscribed(user_id):
    """True / False — подписан ли на канал; None — проверить нельзя (бот не админ канала, сеть)."""
    if not CHANNEL or not user_id:
        return None
    r = tg("getChatMember", {"chat_id": CHANNEL, "user_id": user_id}, timeout=(5, 8))
    if not r.get("ok"):
        print("getChatMember:", r.get("description") or r.get("error"))
        return None
    m = r.get("result") or {}
    st = m.get("status")
    return st in ("creator", "administrator", "member") or (st == "restricted" and bool(m.get("is_member")))


def subscribe_markup(data="sub_check"):
    return {"inline_keyboard": [[{"text": "📢 Подписаться на канал", "url": CHANNEL_URL}],
                                [{"text": "✅ Я подписался", "callback_data": data}]]}


SKETCH_OK = ("🎁 Бесплатный эскиз ваш!\n\nНажмите кнопку ниже — откроется заявка. Добавьте 2–3 фото героя "
             "(лицо крупно, при хорошем свете) и пару слов, для кого мультфильм. Нарисуем героя и покажем до оплаты.\n\n"
             "Удобнее здесь? Просто пришлите фото в этот чат — передадим художнику.")
SKETCH_ASK = (f"🎁 Эскиз героя бесплатно — для подписчиков нашего канала {CHANNEL}.\n\n"
              "Подпишитесь и нажмите «Я подписался» — и эскиз ваш.")


def sketch_offer(chat_id, user):
    """/start sketch или /sketch: подписан -> открыть заявку, нет -> предложить подписку."""
    sub = is_subscribed(user.get("id"))
    if sub is False:
        send(chat_id, SKETCH_ASK, reply_markup=subscribe_markup("sketch_check"))
        return
    send(chat_id, SKETCH_OK, reply_markup=sketch_app_markup())
    if ADMIN and sub:
        send(ADMIN, f"🎁 Подписчик забирает бесплатный эскиз — {who(user)} #c{chat_id}")


def handle_callback(cq):
    user = cq.get("from") or {}
    uid = user.get("id")
    msg = cq.get("message") or {}
    if cq.get("data") == "sketch":
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
        sketch_offer(uid, user)
        return
    if cq.get("data") not in ("sub_check", "sketch_check"):
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
        return
    sub = is_subscribed(uid)
    if sub:
        tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Спасибо за подписку! 🎁"})
        if msg:
            tg("editMessageReplyMarkup", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                          "reply_markup": {"inline_keyboard": []}})
        if cq.get("data") == "sketch_check":
            send(uid, SKETCH_OK, reply_markup=sketch_app_markup())
        else:
            send(uid, "Спасибо за подписку! 🎁 Эскиз героя для вас — бесплатно: нарисуем и покажем до оплаты.")
        if ADMIN:
            send(ADMIN, f"🎁 <b>Клиент подписался на канал</b> — {who(user)} #c{uid}\nЭскиз ему — бесплатно, до оплаты.")
    elif sub is False:
        tg("answerCallbackQuery", {"callback_query_id": cq["id"], "show_alert": True,
                                   "text": "Пока не вижу подписки. Подпишитесь на канал и нажмите кнопку ещё раз 🤍"})
    else:
        tg("answerCallbackQuery", {"callback_query_id": cq["id"], "show_alert": True,
                                   "text": "Не получилось проверить подписку — попробуйте чуть позже."})


# ---------------------------------------------------------------- knowledge + AI
def knowledge():
    try:
        with open(os.path.join(HERE, "knowledge.md"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


SYSTEM = """Ты — вежливый помощник студии авторских мультфильмов Lumio в Telegram.
Отвечай по-русски, тепло и коротко: 2–5 предложений, без markdown-разметки, можно 1 эмодзи.
Отвечай ТОЛЬКО на основе базы знаний ниже. Ничего не придумывай: цены, сроки, скидки и условия — строго из базы.
Если в базе нет ответа, вопрос про оплату, договор, возврат, индивидуальные условия, жалоба,
или человек хочет поговорить с живым человеком — ответь, что передал вопрос команде и она скоро ответит,
и в самом конце добавь метку [ПЕРЕДАТЬ].
Если человек готов заказать — предложи нажать кнопку «Открыть Lumio» под сообщением и оставить заявку. Не пиши названия кнопок в квадратных скобках.
Никогда не обещай того, чего нет в базе. Не обсуждай темы, не связанные со студией.

БАЗА ЗНАНИЙ:
"""


def iam_headers(context):
    if API_KEY:
        return {"Authorization": f"Api-Key {API_KEY}"}
    tok = None
    try:
        tok = (getattr(context, "token", None) or {}).get("access_token")
    except Exception:
        tok = None
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def ask_gpt(question, context):
    """Returns (answer_text, needs_human)."""
    auth = iam_headers(context)
    if not FOLDER or not auth:
        return None, True
    body = {
        "modelUri": f"gpt://{FOLDER}/{MODEL}",
        "completionOptions": {"stream": False, "temperature": 0.2, "maxTokens": "500"},
        "messages": [{"role": "system", "text": SYSTEM + knowledge()},
                     {"role": "user", "text": question[:2000]}],
    }
    try:
        r = requests.post("https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                          json=body, headers={**auth, "x-folder-id": FOLDER}, timeout=(5, 12))
        r.raise_for_status()
        text = r.json()["result"]["alternatives"][0]["message"]["text"].strip()
    except Exception as e:
        print("gpt error", e)
        return None, True
    need = HANDOFF in text
    text = text.replace(HANDOFF, "").strip()
    return (text or None), need or not text


# ---------------------------------------------------------------- telegram updates
GREETING = ("Привет! Вы в <b>Lumio</b> ✨ — студии мультфильмов по вашим историям.\n\n"
            "Рисуем вашу семью мультяшными героями по фото и снимаем про вас настоящий мультфильм: "
            "сценарий, анимация, озвучка и музыка. Ко Дню матери, дню рождения, Новому году — или просто так.\n\n"
            "<b>Что можно сделать здесь:</b>\n"
            "🎬 посмотреть наши мультфильмы и рассчитать цену — кнопка «Открыть Lumio»\n"
            f"🎁 забрать бесплатный эскиз героя — для подписчиков канала {CHANNEL}\n"
            "💬 задать любой вопрос — просто напишите его сюда, я отвечу сразу. "
            "Нужен живой человек — /operator")

# что человек видит в пустом чате ДО нажатия «Старт» (до 512 символов) и в профиле бота (до 120)
DESCRIPTION = ("✨ Lumio — мультфильмы по вашей истории.\n\n"
               "Рисуем вашу семью мультяшными героями по фото и снимаем про вас настоящий мультфильм — "
               "ко Дню матери, дню рождения, Новому году.\n\n"
               "Здесь можно:\n"
               "🎬 посмотреть наши мультфильмы и узнать цену\n"
               "🎁 получить бесплатный эскиз героя\n"
               "💬 задать вопрос — отвечаем сразу\n\n"
               "Нажмите «Старт» 👇")
SHORT_DESCRIPTION = "Мультфильмы по вашей истории: ваша семья — главные герои ✨ Бесплатный эскиз героя подписчикам."
COMMANDS = [{"command": "start", "description": "Начать"},
            {"command": "sketch", "description": "Бесплатный эскиз героя"},
            {"command": "operator", "description": "Связаться с менеджером"}]


def setup_profile():
    """Описание, короткое описание и меню команд бота. Вызывается при каждом запуске."""
    return {"commands": tg("setMyCommands", {"commands": COMMANDS}),
            "description": tg("setMyDescription", {"description": DESCRIPTION}),
            "short": tg("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})}


def greeting_markup():
    return {"inline_keyboard": [[{"text": "✨ Открыть Lumio", "web_app": {"url": WEBAPP_PAGE}}],
                                [{"text": "🎁 Бесплатный эскиз героя", "callback_data": "sketch"}]]}


# ---------------------------------------------------------------- operator mode (per-client state)
STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/lumio-bot/state.json")
HUMAN_HOURS = float(os.environ.get("HUMAN_HOURS", "24"))  # back to the bot after this much operator silence
_lock = threading.Lock()

HUMAN_RE = re.compile(
    r"(^/(operator|manager|human|оператор|менеджер)\b)|оператор|менеджер|жив(ой|ого|ым) человек|"
    r"(позов|позв)\w* человек|с человеком|хочу человека|не бот|ты бот|позвоните|перезвоните",
    re.IGNORECASE)


def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print("state save error", e)


def human_mode(client_id):
    """True if a live operator is handling this client right now."""
    with _lock:
        rec = _load().get(str(client_id))
    if not rec:
        return False
    return time.time() - rec.get("ts", 0) < HUMAN_HOURS * 3600


def set_mode(client_id, human, name=""):
    with _lock:
        st = _load()
        if human:
            rec = st.get(str(client_id), {})
            rec.update({"ts": time.time(), **({"name": name} if name else {})})
            st[str(client_id)] = rec
        else:
            st.pop(str(client_id), None)
        _save(st)


def active_clients():
    with _lock:
        st = _load()
    now = time.time()
    return [(cid, r) for cid, r in st.items() if now - r.get("ts", 0) < HUMAN_HOURS * 3600]


ADMIN_HELP = ("Вы — администратор Lumio.\n\n"
              "• Заявки и вопросы клиентов приходят сюда.\n"
              "• Ответить клиенту — ответьте (свайп влево) на его сообщение с меткой #c… текстом, фото или голосовым. "
              "После вашего ответа бот перестаёт отвечать этому клиенту сам: дальше общаетесь вы.\n"
              "• <b>/bot</b> — ответом на сообщение клиента: вернуть его боту.\n"
              "• <b>/human</b> — ответом на сообщение клиента: забрать на себя без ответа.\n"
              "• <b>/active</b> — с кем вы сейчас на связи.\n"
              f"• Если вы молчите {int(HUMAN_HOURS)} ч, клиент сам возвращается к боту.")


def handle_admin(msg):
    text = (msg.get("text") or "").strip()
    rep = msg.get("reply_to_message")
    if rep:
        src = (rep.get("text") or "") + " " + (rep.get("caption") or "")
        m = TAG_RE.search(src)
        if not m:
            send(ADMIN, "Не нашёл, кому ответить: отвечайте на сообщение с меткой #c…")
            return
        client = m.group(1)
        if text.lower() in ("/bot", "/бот"):
            set_mode(client, False)
            send(ADMIN, f"🤖 Клиент #c{client} снова общается с ботом.", reply_to_message_id=msg["message_id"])
            return
        if text.lower() in ("/human", "/я"):
            set_mode(client, True)
            send(ADMIN, f"👤 Клиент #c{client} теперь на вас — бот ему не отвечает.",
                 reply_to_message_id=msg["message_id"])
            return
        res = tg("copyMessage", {"chat_id": client, "from_chat_id": msg["chat"]["id"],
                                 "message_id": msg["message_id"]})
        if res.get("ok"):
            was = human_mode(client)
            set_mode(client, True)
            send(ADMIN, "✓ Отправлено. " + ("" if was else
                 "Бот больше не отвечает этому клиенту сам — дальше общаетесь вы. Вернуть боту: /bot ответом."),
                 reply_to_message_id=msg["message_id"])
        else:
            send(ADMIN, "Не удалось отправить: клиент мог заблокировать бота.",
                 reply_to_message_id=msg["message_id"])
        return
    if text.lower().startswith("/active"):
        rows = active_clients()
        if not rows:
            send(ADMIN, "Сейчас вы ни с кем не на связи — всем отвечает бот.")
        else:
            send(ADMIN, "👤 Вы на связи с:\n" + "\n".join(
                f"• {esc(r.get('name') or 'клиент')} #c{cid}" for cid, r in rows) +
                "\n\nВернуть клиента боту: ответьте /bot на его сообщение.")
        return
    if text.startswith("/"):
        send(ADMIN, ADMIN_HELP + f"\n\nВаш chat id: <code>{msg['chat']['id']}</code>")


def forward_to_admin(msg, user, head):
    chat_id = msg["chat"]["id"]
    tag = f"#c{chat_id}"
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if msg.get("text"):
        send(ADMIN, f"{head} — {who(user)} {tag}\n\n{esc(text)}")
    else:
        send(ADMIN, f"{head} — {who(user)} {tag}" + (f"\n\n{esc(text)}" if text else ""))
        tg("copyMessage", {"chat_id": ADMIN, "from_chat_id": chat_id, "message_id": msg["message_id"]})


def handle_client(msg, context):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or msg.get("caption") or "").strip()
    tag = f"#c{chat_id}"
    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x)

    if text.startswith("/myid"):
        send(chat_id, f"Ваш chat id: <code>{chat_id}</code>")
        return
    if text.startswith("/sketch") or text.startswith("/start sketch"):
        if text.startswith("/start"):
            send(chat_id, "Привет! Вы в <b>Lumio</b> ✨ — студии мультфильмов по вашим историям: "
                          "рисуем вашу семью мультяшными героями и снимаем про вас настоящий мультфильм.")
        sketch_offer(chat_id, user)
        if ADMIN and text.startswith("/start"):
            send(ADMIN, f"👋 Новый человек в боте (за эскизом): {who(user)} {tag}")
        return
    if text.startswith("/start"):
        send(chat_id, GREETING, reply_markup=greeting_markup())
        if ADMIN:
            send(ADMIN, f"👋 Новый человек в боте: {who(user)} {tag}")
        return

    wad = msg.get("web_app_data")
    if wad:  # order sent through Telegram.WebApp.sendData (keyboard-button launch)
        try:
            order = json.loads(wad.get("data") or "{}")
        except ValueError:
            order = {}
        deliver_order(order, [], {"id": chat_id, "username": user.get("username", "")})
        return

    # 1) a live operator already handles this client -> just pass messages through
    if ADMIN and human_mode(chat_id):  # timer counts from the operator's last reply, not the client's
        forward_to_admin(msg, user, "👤 Клиент пишет вам")
        return

    # 2) the client asks for a human
    if ADMIN and text and HUMAN_RE.search(text):
        set_mode(chat_id, True, name)
        send(chat_id, "Позвал менеджера Lumio 🤍 Он ответит здесь же, обычно в течение часа. "
                      "Пока можно написать подробности — всё передам.")
        forward_to_admin(msg, user, "🙋 <b>Клиент просит живого человека</b>")
        send(ADMIN, "Бот этому клиенту больше не отвечает. Ответьте свайпом на его сообщение.")
        return

    if not text:  # photo / voice / sticker without words
        send(chat_id, "Спасибо! Передал команде — скоро ответим 🤍")
        if ADMIN:
            set_mode(chat_id, True, name)
            forward_to_admin(msg, user, "📎 Сообщение от клиента")
        return

    # 3) the bot answers
    tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    answer, need_human = ask_gpt(text, context)
    if answer:
        send(chat_id, esc(answer), reply_markup=open_app_markup())
    else:
        send(chat_id, "Спасибо за вопрос! Передал его команде Lumio — ответим совсем скоро 🤍")
    if ADMIN:
        if need_human:
            set_mode(chat_id, True, name)
        head = "⚠️ <b>Нужен ваш ответ</b>" if need_human else "💬 Вопрос клиента"
        body = (f"{head} — {who(user)} {tag}\n\n<b>Вопрос:</b> {esc(text)}"
                + (f"\n\n<b>Бот ответил:</b> {esc(answer)}" if answer else "")
                + ("\n\nБот передал клиента вам и больше ему не отвечает." if need_human else "")
                + "\n\nОтветьте на это сообщение, чтобы написать клиенту.")
        send(ADMIN, body)


def handle_update(update, context):
    if update.get("callback_query"):
        handle_callback(update["callback_query"])
        return
    msg = update.get("message")
    if not msg or msg.get("chat", {}).get("type") != "private":
        return
    if ADMIN and str(msg["chat"]["id"]) == ADMIN:
        handle_admin(msg)
    else:
        handle_client(msg, context)


# ---------------------------------------------------------------- orders from the Mini App
def check_init_data(init_data):
    """Validates Telegram WebApp initData; returns the user dict or None."""
    if not init_data or not BOT_TOKEN:
        return None
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    want = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None


def parse_multipart(body, content_type):
    msg = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields, files = {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if part.get_filename():
            files.append((part.get_filename(), data, part.get_content_type()))
        elif name:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def order_text(o, tg_user, sub=None):
    c = o.get("calc") or {}
    lines = [
        "🎬 <b>Новая заявка Lumio</b>",
        f"<b>Имя:</b> {esc(o.get('name'))}",
        f"<b>Контакт:</b> {esc(o.get('contact'))}",
        f"<b>Повод:</b> {esc(o.get('occasion'))}",
        f"<b>Параметры:</b> {esc(c.get('text'))}",
        f"<b>Герои:</b> {esc(o.get('heroes') or '—')}",
        f"<b>К дате:</b> {esc(o.get('date') or 'не указано')}",
        f"<b>Фото:</b> {esc(o.get('photos') or 0)} шт.",
    ]
    if o.get("source"):
        lines.append(f"<b>Источник:</b> {esc(o.get('source'))}")
    if tg_user and tg_user.get("id"):
        un = tg_user.get("username")
        lines.append(f"<b>Telegram:</b> " + (f"@{esc(un)} " if un else "") + f"#c{tg_user['id']}")
        lines.append({True: "🎁 <b>Подписчик канала — эскиз бесплатно</b>",
                      False: "Канал: не подписан — эскиз после предоплаты",
                      None: "Канал: проверить не удалось (бот должен быть админом канала)"}[sub])
    lines += ["", "<b>История:</b>", esc(o.get("story") or "—")]
    return "\n".join(lines)[:4000]


def deliver_order(order, photos, tg_user):
    if not ADMIN:
        print("ADMIN_CHAT_ID is not set; order lost:", order)
        return False
    sub = is_subscribed(tg_user.get("id")) if tg_user and tg_user.get("id") else None
    res = send(ADMIN, order_text(order, tg_user, sub))
    photos = photos[:10]
    if len(photos) == 1:
        n, d, t = photos[0]
        tg("sendPhoto", {"chat_id": ADMIN, "caption": f"Фото к заявке: {order.get('name', '')}"},
           files={"photo": (n, d, t)})
    elif len(photos) > 1:
        media, files = [], {}
        for i, (n, d, t) in enumerate(photos):
            files[f"p{i}"] = (n, d, t)
            item = {"type": "photo", "media": f"attach://p{i}"}
            if i == 0:
                item["caption"] = f"Фото к заявке: {order.get('name', '')}"
            media.append(item)
        tg("sendMediaGroup", {"chat_id": ADMIN, "media": json.dumps(media)}, files=files)
    if tg_user and tg_user.get("id"):
        thanks = (f"Спасибо, {esc(order.get('name') or '')}! Заявка получена 🤍\n"
                  "Сказочник Lumio напишет вам в течение часа.")
        if sub:
            send(tg_user["id"], thanks + "\n\n🎁 Вы подписаны на наш канал — эскиз героя для вас бесплатно, "
                                         "покажем его до оплаты.")
        elif sub is False:
            send(tg_user["id"], thanks + f"\n\n🎁 Подпишитесь на наш канал {CHANNEL} — и эскиз героя будет "
                                         "бесплатно, до оплаты.", reply_markup=subscribe_markup())
        else:
            send(tg_user["id"], thanks)
    return bool(res.get("ok"))


# ---------------------------------------------------------------- entry point
def respond(code, payload, extra=None):
    return {"statusCode": code, "headers": {"Content-Type": "application/json; charset=utf-8", **CORS, **(extra or {})},
            "body": json.dumps(payload, ensure_ascii=False)}


def handler(event, context):
    method = (event.get("httpMethod") or "POST").upper()
    qs = event.get("queryStringParameters") or {}
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
    src = qs.get("src", "")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    if src == "diag":
        if not SECRET or qs.get("key") != SECRET:
            return respond(403, {"ok": False, "error": "bad key"})
        return respond(200, diagnose(context))

    if src == "setup":
        if not SECRET or qs.get("key") != SECRET:
            return respond(403, {"ok": False, "error": "bad key"})
        hook = tg("setWebhook", {"url": f"{PUBLIC_URL}?src=tg", "secret_token": SECRET,
                                 "allowed_updates": ["message", "callback_query"], "drop_pending_updates": True})
        cmds = setup_profile()
        return respond(200, {"webhook": hook, "commands": cmds, "admin_set": bool(ADMIN),
                             "gpt_ready": bool(FOLDER and (API_KEY or iam_headers(context)))})

    if src == "tg":
        if headers.get("x-telegram-bot-api-secret-token") != SECRET:
            return respond(403, {"ok": False})
        try:
            handle_update(json.loads(body or b"{}"), context)
        except Exception as e:  # always 200 so Telegram does not retry forever
            print("update error", repr(e))
        return respond(200, {"ok": True})

    if src == "order" and method == "POST":
        ctype = headers.get("content-type", "")
        try:
            if "multipart/form-data" in ctype:
                fields, files = parse_multipart(body, ctype)
            else:
                fields, files = {"order": body.decode("utf-8", "replace")}, []
            order = json.loads(fields.get("order") or "{}")
        except Exception as e:
            print("order parse error", repr(e))
            return respond(400, {"ok": False, "error": "bad request"})
        if not (order.get("name") and order.get("contact")):
            return respond(400, {"ok": False, "error": "name and contact required"})
        tg_user = check_init_data(fields.get("initData", ""))
        photos = [f for f in files if f[2].startswith("image/")]
        ok = deliver_order(order, photos, tg_user)
        return respond(200 if ok else 502, {"ok": ok})

    return respond(200, {"ok": True, "service": "lumio-bot"})
