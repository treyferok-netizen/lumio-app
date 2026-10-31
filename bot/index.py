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

import crm
import seedream

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
QUIZ_URL = os.environ.get("QUIZ_URL", "https://treyferok-netizen.github.io/lumio-app/quiz.html?src=bot")
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
_sub_cache = {}  # user_id -> (time, True): подписчиков не перепроверяем 10 минут


def is_subscribed(user_id):
    """True / False — подписан ли на канал; None — проверить нельзя (бот не админ канала, сеть)."""
    if not CHANNEL or not user_id:
        return None
    hit = _sub_cache.get(user_id)
    if hit and time.time() - hit[0] < 600:
        return True
    r = tg("getChatMember", {"chat_id": CHANNEL, "user_id": user_id}, timeout=(5, 8))
    if not r.get("ok"):
        print("getChatMember:", r.get("description") or r.get("error"))
        return None
    m = r.get("result") or {}
    st = m.get("status")
    ok = st in ("creator", "administrator", "member") or (st == "restricted" and bool(m.get("is_member")))
    if ok:
        _sub_cache[user_id] = (time.time(), True)
    else:
        _sub_cache.pop(user_id, None)
    return ok


GATE_TEXT = ("Привет! Вы в <b>Lumio</b> ✨ — студии мультфильмов по вашим историям: рисуем вашу семью "
             "мультяшными героями и снимаем про вас настоящий мультфильм.\n\n"
             f"Бот и приложение Lumio открыты для подписчиков нашего канала {CHANNEL} — там наши мультфильмы, "
             "закулисье и праздничные предложения. А ещё подписчикам — эскиз героя бесплатно 🎁\n\n"
             "Подпишитесь и нажмите «Я подписался» 👇")


def gate(chat_id, text):
    """Обязательная подписка: показываем приглашение, после подписки ведём туда, куда человек шёл."""
    dest = "sketch" if (text.startswith("/sketch") or text.startswith("/start sketch")) else "start"
    send(chat_id, GATE_TEXT, reply_markup=subscribe_markup("gate_" + dest))


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
    if (cq.get("data") or "").startswith("st:"):
        if ADMIN and str(uid) == ADMIN:
            handle_status(cq)
        else:
            tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
        return
    if cq.get("data") == "sketch":
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
        sketch_offer(uid, user)
        return
    if (cq.get("data") or "").startswith("gate_"):
        sub = is_subscribed(uid)
        if sub is False:
            tg("answerCallbackQuery", {"callback_query_id": cq["id"], "show_alert": True,
                                       "text": "Пока не вижу подписки. Подпишитесь на канал и нажмите кнопку ещё раз 🤍"})
            return
        tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Добро пожаловать в Lumio ✨"})
        if msg:
            tg("editMessageReplyMarkup", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                          "reply_markup": {"inline_keyboard": []}})
        if cq["data"] == "gate_sketch":
            sketch_offer(uid, user)
        else:
            send(uid, GREETING, reply_markup=greeting_markup())
        crm.log_event("sub", uid)
        if ADMIN:
            send(ADMIN, f"📢 Подписался на канал и открыл бота — {who(user)} #c{uid}")
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


# ---------------------------------------------------------------- квиз: идея мультфильма от нейросети
IDEA_MODEL = os.environ.get("IDEA_MODEL", "yandexgpt/latest")
IDEA_SYSTEM = """Ты — сценарист студии персональных мультфильмов Lumio. Родитель прошёл квиз о ребёнке.
По его ответам придумай идею мультфильма на 30 секунд – 2 минуты (длину берёт из ответов; для 2 минут можно два коротких эпизода приключения), где главный герой — этот ребёнок.

Правила:
- Используй живые детали из ответов: имя, игрушку, словечки, случай из жизни, семейный ритуал. Чем конкретнее, тем лучше.
- Обязательно немного волшебства: любимая вещь ребёнка оживает и становится его другом, или привычное место превращается в сказочный мир его увлечений (двор — в гоночную трассу, ванна — в океан).
- Одна простая цель, одна смешная мультяшная неудача (плюхнулся, застрял, перепутал), герой справляется сам или с близкими, тёплый финал дома с семейным ритуалом, если он есть.
- Пиши живо, в настоящем времени, как короткий пересказ мультфильма, с одной неожиданной смешной деталью.
- Без злодеев, страшного, травм и слёз. Подходит для просмотра перед сном.
- Если родитель назвал игрушку из известного мультфильма (например, Молния Маквин), можно так и написать: «его машинка, похожая на Молнию Маквин». Но сам герой чужого мультфильма в истории не участвует и не говорит — оживает именно игрушка ребёнка.
- Только свои персонажи. Нельзя: Маша и Медведь, Смешарики, Фиксики, Синий трактор, Тачки, Молния Маккуин, Элли, Страшила, Тотошка, Disney, Pixar, Marvel, любые известные мультгерои и бренды.
- Учитывай возраст героя и выбранное настроение. Малыш до 2 лет почти не говорит и не совершает сложных действий: история о простом (первые шаги, игрушка, мама и папа рядом), волшебство вокруг него. С 3 лет герой действует сам («я сам!»), с 7 лет — настоящее приключение с выдумкой.
- Не упоминай цены, сроки, огонёк и студию.
- Пиши тепло и просто, обращаясь к родителю, без пафоса и общих слов.

Ответ строго в формате:
Название: <2–5 слов>
Идея: <3–4 предложения, до 450 знаков>"""
_idea_hits = {}
BANNED_IP = re.compile(r"ма[кк]{1,2}у?и[нн]|маквин|mcqueen|молни[яи]\s+ма|тачк[иа]|маш[аиуеой]\s+и\s+медвед|смешарик|фиксик|синий\s+трактор|щенячий\s+патрул|элли\b|страшил|тотошк|дисне|disney|pixar|пиксар|marvel|марвел|человек.паук|эльз[аы]\b|холодное\s+сердце", re.I)


def gen_idea(answers, ip="", context=None):
    """Возвращает {"title", "idea"} или None."""
    now = time.time()
    hits = [t for t in _idea_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= 15:  # защита от злоупотреблений: 15 идей в час с одного адреса
        return None
    _idea_hits[ip] = hits + [now]
    auth = iam_headers(context)
    if not FOLDER or not auth:
        return None
    user_text = "Ответы родителя:\n" + str(answers)[:2500]
    text = None
    for attempt in range(2):
        body = {
            "modelUri": f"gpt://{FOLDER}/{IDEA_MODEL}",
            "completionOptions": {"stream": False, "temperature": 0.7, "maxTokens": "400"},
            "messages": [{"role": "system", "text": IDEA_SYSTEM}, {"role": "user", "text": user_text}],
        }
        try:
            r = requests.post("https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                              json=body, headers={**auth, "x-folder-id": FOLDER}, timeout=(5, 25))
            r.raise_for_status()
            text = r.json()["result"]["alternatives"][0]["message"]["text"].strip()
        except Exception as e:
            print("idea error", e)
            return None
        # чужие имена допустимы, только если их назвал сам родитель (его игрушка)
        foreign = [m.group(0) for m in BANNED_IP.finditer(text) if not BANNED_IP.search(str(answers)) or m.group(0)[:4].lower() not in str(answers).lower()]
        if not foreign:
            break
        # в тексте всплыл чужой персонаж — просим переписать один раз, иначе отдаём шаблон
        user_text += "\n\nВажно: не упоминай известных мультгероев и брендов, кроме игрушек, которые назвал родитель."
        text = None
    if not text:
        return None
    m_t = re.search(r"Название:\s*(.+)", text)
    m_i = re.search(r"Идея:\s*([\s\S]+)", text)
    idea = (m_i.group(1) if m_i else text).strip().strip("*").strip()
    title = (m_t.group(1) if m_t else "").strip().strip("*«»\"").strip()
    if not idea:
        return None
    return {"title": title[:80], "idea": idea[:700]}


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
SHORT_DESCRIPTION = "Снимаем мультфильм про вашу семью по фото и вашей истории 🎬 Подписчикам @lumioRF — эскиз героя бесплатно"
COMMANDS = [{"command": "start", "description": "Начать"},
            {"command": "sketch", "description": "Бесплатный эскиз героя"},
            {"command": "operator", "description": "Связаться с менеджером"}]


def setup_profile():
    """Описание, короткое описание и меню команд бота. Вызывается при каждом запуске."""
    return {"commands": tg("setMyCommands", {"commands": COMMANDS}),
            "description": tg("setMyDescription", {"description": DESCRIPTION}),
            "short": tg("setMyShortDescription", {"short_description": SHORT_DESCRIPTION}),
            # отдельное меню команд — только в вашем чате с ботом
            "admin_menu": tg("setMyCommands", {"scope": {"type": "chat", "chat_id": int(ADMIN)}, "commands": [
                {"command": "img", "description": "Seedream: шаги и генерация кадра"},
                {"command": "orders", "description": "Открытые заявки"},
                {"command": "stats", "description": "Воронка за 30 дней"},
                {"command": "export", "description": "Все заявки таблицей"},
                {"command": "active", "description": "С кем я сейчас на связи"},
                {"command": "help", "description": "Подсказка"}]}) if ADMIN.lstrip("-").isdigit() else {}}


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
              "• <b>/active</b> — с кем вы сейчас на связи.\n\n"
              "<b>Кадры Seedream:</b> пришлите референсы, затем /img номер шага (или /img свой промт). /img — список.\n\n"
              "<b>Заявки (мини-CRM):</b>\n"
              "• Статус меняется кнопками под заявкой: 🆕 → 🎨 Эскиз → 💳 Оплачено → 🎬 В работе → ✅ Сдано / ❌ Отказ.\n"
              "• <b>/zayavka</b> — ответом на сообщение клиента: завести заявку из переписки "
              "(можно с деталями: /zayavka День матери, 1 минута). Клиенту не отправляется.\n"
              "• Клиент прислал фото в чат — заявка заводится сама.\n"
              "• <b>/orders</b> — открытые заявки · <b>/order 3</b> — карточка заявки №3 с кнопками.\n"
              "• <b>/stats</b> — воронка за 30 дней · <b>/export</b> — все заявки таблицей для Excel.\n\n"
              f"• Если вы молчите {int(HUMAN_HOURS)} ч, клиент сам возвращается к боту.")


def _download(file_id):
    info = tg("getFile", {"file_id": file_id}).get("result") or {}
    if not info.get("file_path"):
        return None
    try:
        r = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}", timeout=(10, 60))
        return r.content if r.ok else None
    except requests.RequestException:
        return None


def admin_img(text):
    """/img, /img N, /img текст, /img_clear — генерация кадров Seedream на сервере."""
    if text.lower().startswith("/img_clear"):
        seedream.clear_refs()
        send(ADMIN, "🧹 Референсы сброшены.")
        return
    arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    st = seedream.steps()
    if not arg:
        lines = [f"<b>{esc(k)}</b> — {esc(v['title'])}\n    {esc(v['refs'])}" for k, v in st.get("steps", {}).items()]
        send(ADMIN, f"🎨 <b>Seedream — {esc(st.get('film', ''))}</b>\n\n" + "\n".join(lines) +
             f"\n\nРеференсов сейчас: {seedream.refs_count()} · осталось на сегодня: {seedream.left_today()}\n"
             "1) Пришлите референсы по порядку (image 1, 2, 3) фото или файлом\n2) /img номер шага — или /img свой промт\n"
             "/img_clear — сбросить референсы")
        return
    if not seedream.ready():
        send(ADMIN, "Seedream не подключён: на сервере выполните\n<code>lumio-set SEEDREAM_API_KEY ваш_ключ</code>")
        return
    step = st.get("steps", {}).get(arg)
    prompt = step["prompt"] if step else arg
    label = f"шаг {arg}: {step['title']}" if step else "свой промт"
    m = re.search(r"\b(f\d+)\b", step["title"]) if step else None
    fname = m.group(1) if m else "img"
    n_refs = seedream.refs_count()
    send(ADMIN, f"⏳ Генерирую {seedream.VARIANTS} варианта — {esc(label)}\nРеференсов: {n_refs}"
                + (f" (нужно: {esc(step['refs'])})" if step else "") + "\nОбычно 20–60 секунд.")

    def done(images):
        for i, data in enumerate(images, 1):
            tg("sendDocument", {"chat_id": ADMIN, "caption": f"Вариант {i} — {label}"},
               files={"document": (f"{fname}_v{i}.jpg", data, "image/jpeg")})
        send(ADMIN, f"✅ Готово. Осталось на сегодня: {seedream.left_today()}. Референсы сохранены — можно сразу /img ещё раз.")

    seedream.generate(prompt, done, lambda e: send(ADMIN, "⚠️ " + esc(e)))


def handle_admin(msg):
    text = (msg.get("text") or "").strip()
    rep = msg.get("reply_to_message")
    # картинка без ответа на клиента = референс для /img
    if not rep and (msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").startswith("image/")):
        fid = msg["photo"][-1]["file_id"] if msg.get("photo") else msg["document"]["file_id"]
        data = _download(fid)
        if not data:
            send(ADMIN, "Не смог скачать картинку, пришлите ещё раз.")
            return
        n = seedream.add_ref(data, (msg.get("document") or {}).get("mime_type") or "image/jpeg")
        send(ADMIN, f"📎 Референс сохранён как image {n}. Дальше: /img номер шага или /img свой промт. Список шагов: /img")
        return
    if text.lower().startswith("/img"):
        admin_img(text)
        return
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
        if re.match(r"^/(zayavka|заявка)\b", text, re.IGNORECASE):
            info = re.sub(r"^/(zayavka|заявка)\S*\s*", "", text, flags=re.IGNORECASE).strip()
            num = order_from_chat(int(client), info, "добавил менеджер")
            set_mode(client, True)
            send(ADMIN, f"📝 Заявка #{num} заведена (клиенту ничего не отправлено).",
                 reply_to_message_id=msg["message_id"])
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
    if text.startswith("/") and admin_crm(text):
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


_quiz_notified = {}  # chat_id -> время последнего уведомления «пришёл за квизом»


def handle_client(msg, context):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or msg.get("caption") or "").strip()
    tag = f"#c{chat_id}"
    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x)

    if text.startswith("/myid"):
        send(chat_id, f"Ваш chat id: <code>{chat_id}</code>")
        return
    if text.startswith("/start"):
        crm.log_event("start", user.get("id"))
    # короткая ссылка на квиз: t.me/LumioMultBot?start=quiz (без обязательной подписки — это вход в воронку)
    if text.startswith("/start quiz") or text.startswith("/quiz"):
        send(chat_id, "🎬 <b>Какой мультфильм подойдёт вашему ребёнку?</b>\n\n"
                      "10 коротких вопросов — около 2 минут. В конце сказочник Lumio придумает историю, "
                      "где главный герой ваш ребёнок.",
             reply_markup={"inline_keyboard": [[{"text": "✨ Пройти квиз", "url": QUIZ_URL}]]})
        # уведомление — один раз в 12 часов на человека (ссылку часто нажимают повторно)
        last = _quiz_notified.get(chat_id, 0)
        if ADMIN and text.startswith("/start") and time.time() - last > 12 * 3600:
            _quiz_notified[chat_id] = time.time()
            send(ADMIN, f"👋 Новый человек в боте (квиз): {who(user)} {tag}")
        return
    # обязательная подписка (кроме клиентов, с которыми уже общается менеджер)
    if not (ADMIN and human_mode(chat_id)) and is_subscribed(user.get("id")) is False:
        if text.startswith("/start") and ADMIN:
            send(ADMIN, f"👋 Новый человек в боте (ещё не подписан на канал): {who(user)} {tag}")
        crm.log_event("gate", user.get("id"))
        gate(chat_id, text)
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

    # 0) фото в чат = хочет эскиз: заводим заявку (одну на клиента в сутки) и передаём фото
    if ADMIN and (msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").startswith("image/")):
        photos_from_client(msg, user, text)
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


_updates_seen = {}  # update_id -> время: Telegram иногда присылает одно и то же обновление повторно


def handle_update(update, context):
    uid = update.get("update_id")
    if uid is not None:
        now = time.time()
        if uid in _updates_seen:
            return
        _updates_seen[uid] = now
        if len(_updates_seen) > 2000:
            for k in [k for k, t in _updates_seen.items() if now - t > 3600]:
                _updates_seen.pop(k, None)
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
    num = crm.add_order(order, tg_user, sub)
    res = send(ADMIN, f"<b>Заявка #{num}</b>\n" + order_text(order, tg_user, sub), reply_markup=status_markup(num, "new"))
    if res.get("ok"):
        crm.set_admin_msg(num, res["result"]["message_id"])
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


# ---------------------------------------------------------------- mini-CRM (статусы заявок, воронка)
_album_seen = {}  # media_group_id -> time: одно «спасибо» и одна шапка на альбом из нескольких фото


def order_from_chat(uid, info, source):
    """Заявка из переписки (а не из формы Mini App). Возвращает номер."""
    ch = (tg("getChat", {"chat_id": uid}) or {}).get("result") or {}
    name = " ".join(x for x in [ch.get("first_name"), ch.get("last_name")] if x) or "Клиент"
    un = ch.get("username") or ""
    o = {"name": name, "contact": ("@" + un) if un else "Telegram", "occasion": info or "—", "source": source}
    sub = is_subscribed(uid)
    num = crm.add_order(o, {"id": uid, "username": un}, sub)
    res = send(ADMIN, f"<b>Заявка #{num}</b> ({source})\n<b>Имя:</b> {esc(name)}"
                      + (f" (@{esc(un)})" if un else "") + f" #c{uid}"
                      + (f"\n<b>Повод / детали:</b> {esc(info)}" if info else ""),
               reply_markup=status_markup(num, "new"))
    if res.get("ok"):
        crm.set_admin_msg(num, res["result"]["message_id"])
    return num


def photos_from_client(msg, user, caption):
    chat_id = msg["chat"]["id"]
    album = msg.get("media_group_id")
    first = True
    if album:
        now = time.time()
        for k in [k for k, t in _album_seen.items() if now - t > 600]:
            _album_seen.pop(k, None)
        first = album not in _album_seen
        _album_seen[album] = now
    if first:
        r = crm.recent_open_by_uid(chat_id)
        if r:
            send(ADMIN, f"📸 Ещё фото к заявке #{r['id']} — {who(user)} #c{chat_id}"
                        + (f"\n\n{esc(caption)}" if caption else ""))
        else:
            order_from_chat(chat_id, caption, "фото в чат")
        if not human_mode(chat_id):
            send(chat_id, "Спасибо! Фото получили и передали художнику 🎨\n"
                          "Напишем вам здесь в течение часа — уточним пару деталей и пришлём эскиз героя.")
        set_mode(chat_id, True, " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x))
    tg("copyMessage", {"chat_id": ADMIN, "from_chat_id": chat_id, "message_id": msg["message_id"]})
def status_markup(num, current):
    btn = lambda code, label: {"text": ("• " + label + " •") if code == current else label,  # noqa: E731
                               "callback_data": f"st:{num}:{code}"}
    s = crm.STATUSES
    return {"inline_keyboard": [[btn(*s[0]), btn(*s[1]), btn(*s[2])], [btn(*s[3]), btn(*s[4]), btn(*s[5])]]}


def handle_status(cq):
    try:
        _, num, code = cq["data"].split(":")
        num = int(num)
    except ValueError:
        return tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
    r = crm.set_status(num, code)
    if not r:
        return tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Заявка не найдена"})
    msg = cq.get("message") or {}
    if msg:
        tg("editMessageReplyMarkup", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                      "reply_markup": status_markup(num, code)})
    tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": f"#{num}: {crm.LABEL[code]}"})


def days_ago(ts):
    d = int((time.time() - ts) // 86400)
    return "сегодня" if d == 0 else f"{d} дн."


def admin_crm(text):
    """/orders, /order N, /stats, /export. True — команда обработана."""
    low = text.lower()
    if low.startswith("/orders"):
        rows = crm.open_orders()
        if not rows:
            send(ADMIN, "Открытых заявок нет 🙌")
        else:
            lines = [f"#{r['id']} · {esc(r['name'])} · {esc(r['occasion'])} · {crm.LABEL[r['status']]} · {days_ago(r['ts'])}"
                     for r in sorted(rows, key=lambda r: r["ts"])]
            send(ADMIN, "📋 <b>Открытые заявки</b>\n\n" + "\n".join(lines) +
                 "\n\nКарточка с кнопками: /order номер (например, /order 3)")
        return True
    if low.startswith("/order"):
        try:
            num = int(text.split()[1].lstrip("#"))
        except (IndexError, ValueError):
            send(ADMIN, "Напишите номер: /order 3")
            return True
        r = crm.get(num)
        if not r:
            send(ADMIN, f"Заявки #{num} нет.")
            return True
        hist = "\n".join(f"  {time.strftime('%d.%m %H:%M', time.localtime(t))} — {crm.LABEL.get(s, s)}" for t, s in r["history"])
        tag = f"#c{r['uid']}" if r.get("uid") else ""
        send(ADMIN, f"<b>Заявка #{num}</b> — {esc(r['name'])} {tag}\n<b>Контакт:</b> {esc(r['contact'])}\n"
                    f"<b>Повод:</b> {esc(r['occasion'])}\n<b>Параметры:</b> {esc(r['params'])}\n"
                    f"<b>История:</b> {esc(r['story'][:600]) or '—'}\n\n<b>Статусы:</b>\n{hist}",
             reply_markup=status_markup(num, r["status"]))
        return True
    if low.startswith("/stats"):
        s = crm.stats(30)
        pct = lambda a, b: f" ({round(100 * a / b)}%)" if b else ""  # noqa: E731
        send(ADMIN, "📊 <b>Воронка за 30 дней</b>\n\n"
                    f"👋 Пришли в бота: {s['start']}\n"
                    f"📢 Показали подписку: {s['gate']} → подписались: {s['sub']}{pct(s['sub'], s['gate'])}\n"
                    f"📝 Заявки: {s['orders']}\n"
                    f"🎨 Эскиз отправлен: {s['sketch']}{pct(s['sketch'], s['orders'])}\n"
                    f"💳 Оплатили: {s['paid']}{pct(s['paid'], s['orders'])}\n"
                    f"✅ Сдано: {s['done']}\n❌ Отказ: {s['lost']}\n⏳ В работе сейчас: {s['open']}")
        return True
    if low.startswith("/export"):
        tg("sendDocument", {"chat_id": ADMIN, "caption": "Все заявки Lumio (открывается в Excel)"},
           files={"document": (f"lumio_zayavki_{time.strftime('%Y-%m-%d')}.csv", crm.export_csv(), "text/csv")})
        return True
    return False


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

    if src == "check" and method == "POST":  # Mini App: подписан ли пользователь на канал
        try:
            init = json.loads(body or b"{}").get("initData", "")
        except ValueError:
            init = ""
        u = check_init_data(init)
        sub = is_subscribed(u.get("id")) if u and u.get("id") else None
        return respond(200, {"ok": True, "subscribed": sub, "channel": CHANNEL_URL})

    if src == "idea" and method == "POST":  # квиз: идея мультфильма по ответам
        try:
            answers = json.loads(body or b"{}").get("answers", "")
        except ValueError:
            answers = ""
        ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or headers.get("x-real-ip", "")
        res = gen_idea(answers, ip, context) if answers else None
        return respond(200, {"ok": bool(res), **(res or {})})

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
        if tg_user and tg_user.get("id") and is_subscribed(tg_user["id"]) is False:
            return respond(403, {"ok": False, "error": "not_subscribed", "channel": CHANNEL_URL})
        photos = [f for f in files if f[2].startswith("image/")]
        ok = deliver_order(order, photos, tg_user)
        return respond(200 if ok else 502, {"ok": ok})

    return respond(200, {"ok": True, "service": "lumio-bot"})
