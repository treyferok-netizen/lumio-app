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
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
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
        print("tg error", method, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


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
GREETING = ("Привет! Я помощник студии <b>Lumio</b> ✨\n\n"
            "Мы превращаем истории вашей семьи в волшебные мультфильмы: сценарий, герой по фото, "
            "озвучка и музыка. Эскиз героя — бесплатно.\n\n"
            "Нажмите «Открыть Lumio», чтобы посмотреть мультфильмы и рассчитать цену, "
            "или просто задайте вопрос здесь.")


def handle_admin(msg):
    text = msg.get("text") or ""
    rep = msg.get("reply_to_message")
    if rep:
        src = (rep.get("text") or "") + " " + (rep.get("caption") or "")
        m = TAG_RE.search(src)
        if not m:
            send(ADMIN, "Не нашёл, кому ответить: отвечайте на сообщение с меткой #c…")
            return
        client = m.group(1)
        res = tg("copyMessage", {"chat_id": client, "from_chat_id": msg["chat"]["id"],
                                 "message_id": msg["message_id"]})
        send(ADMIN, "✓ Отправлено клиенту" if res.get("ok") else
             "Не удалось отправить: клиент мог заблокировать бота.",
             reply_to_message_id=msg["message_id"])
        return
    if text.startswith("/"):
        send(ADMIN, "Вы — администратор Lumio.\n\n"
                    "• Заявки из Mini App и вопросы клиентов приходят сюда.\n"
                    "• Чтобы ответить клиенту, ответьте (свайп влево) на его сообщение с меткой #c… "
                    "— текстом, фото или голосовым.\n"
                    f"• Ваш chat id: <code>{msg['chat']['id']}</code>")


def handle_client(msg, context):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or msg.get("caption") or "").strip()
    tag = f"#c{chat_id}"

    if text.startswith("/myid"):
        send(chat_id, f"Ваш chat id: <code>{chat_id}</code>")
        return
    if text.startswith("/start"):
        send(chat_id, GREETING, reply_markup=open_app_markup())
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

    if not text:  # photo / voice / sticker without words
        send(chat_id, "Спасибо! Передал команде — скоро ответим 🤍")
        if ADMIN:
            send(ADMIN, f"📎 Сообщение от {who(user)} {tag}\nОтветьте на это сообщение, чтобы ответить клиенту.")
            tg("forwardMessage", {"chat_id": ADMIN, "from_chat_id": chat_id, "message_id": msg["message_id"]})
        return

    tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    answer, need_human = ask_gpt(text, context)
    if answer:
        send(chat_id, esc(answer), reply_markup=open_app_markup())
    else:
        send(chat_id, "Спасибо за вопрос! Передал его команде Lumio — ответим совсем скоро 🤍")
    if ADMIN:
        head = "⚠️ <b>Нужен ваш ответ</b>" if need_human else "💬 Вопрос клиента"
        body = (f"{head} — {who(user)} {tag}\n\n<b>Вопрос:</b> {esc(text)}"
                + (f"\n\n<b>Бот ответил:</b> {esc(answer)}" if answer else "")
                + "\n\nОтветьте на это сообщение, чтобы написать клиенту.")
        send(ADMIN, body)


def handle_update(update, context):
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


def order_text(o, tg_user):
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
    lines += ["", "<b>История:</b>", esc(o.get("story") or "—")]
    return "\n".join(lines)[:4000]


def deliver_order(order, photos, tg_user):
    if not ADMIN:
        print("ADMIN_CHAT_ID is not set; order lost:", order)
        return False
    res = send(ADMIN, order_text(order, tg_user))
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
        send(tg_user["id"], f"Спасибо, {esc(order.get('name') or '')}! Заявка получена 🤍\n"
                            "Сказочник Lumio напишет вам в течение часа и пришлёт эскиз героя.")
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
                                 "allowed_updates": ["message"], "drop_pending_updates": True})
        cmds = tg("setMyCommands", {"commands": [{"command": "start", "description": "Начать"},
                                                 {"command": "myid", "description": "Мой chat id"}]})
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
