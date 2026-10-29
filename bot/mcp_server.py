"""Свой MCP-коннектор для Claude: Seedream (BytePlus ModelArk) + кадры из видео — прямо с сервера Lumio.

Адрес коннектора:  https://<сервер>/mcp/<MCP_SECRET>   (добавляется в claude.ai → Настройки → Коннекторы)
Готовые картинки:  https://<сервер>/files/<случайное имя>.jpg — публичные ссылки для Kling и для вас.

Настройки (lumio-set ИМЯ ЗНАЧЕНИЕ):
  MCP_SECRET            секретная часть адреса (без неё коннектор выключен)
  SEEDREAM_API_KEY      ключ BytePlus ModelArk (в чат не присылать)
  SEEDREAM_MODEL / SEEDREAM_BASE / SEEDREAM_DAILY_LIMIT — как в seedream.py
  PUBLIC_BASE           внешний адрес сервера, напр. https://193-32-178-96.sslip.io (по умолчанию берётся из запроса)

Протокол: MCP Streamable HTTP, ответы обычным JSON (без SSE). Генерация — строго одна за раз.
"""
import base64
import hmac
import json
import os
import secrets
import tempfile
import threading
import time

import requests

import seedream

SECRET = os.environ.get("MCP_SECRET", "").strip()
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "").rstrip("/")
GEN_DIR = os.path.join(seedream.DIR, "gen")
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
WAIT = 45                      # сколько секунд tools/call ждёт результат, дальше — seedream_result
SIZES = {"16:9": "2560x1440", "9:16": "1440x2560", "1:1": "2048x2048", "4:3": "2304x1728", "3:4": "1728x2304"}

_jobs = {}                     # id -> {"status", "file", "error", "ts", "prompt"}
_busy = threading.Lock()       # одна генерация за раз


def enabled():
    return len(SECRET) >= 16


def path_ok(path):
    parts = path.strip("/").split("/")
    return len(parts) == 2 and parts[0] == "mcp" and enabled() and hmac.compare_digest(parts[1], SECRET)


# ---------- файлы ----------
def _save(data, ext="jpg"):
    os.makedirs(GEN_DIR, exist_ok=True)
    name = time.strftime("%m%d-%H%M%S-") + secrets.token_hex(8) + "." + ext
    with open(os.path.join(GEN_DIR, name), "wb") as f:
        f.write(data)
    return name


def file_path(name):
    """Путь к файлу для GET /files/<name> (только наши имена, без ../)."""
    if not name or "/" in name or "\\" in name or name.startswith(".") or not name.endswith((".jpg", ".png")):
        return None
    p = os.path.join(GEN_DIR, name)
    return p if os.path.isfile(p) else None


def _url(name, host):
    base = PUBLIC_BASE or ("https://" + host if host else "")
    return f"{base}/files/{name}"


def _preview(data, width=1024):
    """Уменьшенная копия для просмотра в чате (Claude видит её как картинку)."""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        if w > width:
            img = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return base64.b64encode(buf.tobytes()).decode() if ok else None
    except Exception as e:  # нет opencv — просто без превью
        print("preview error", repr(e), flush=True)
        return None


def _fetch(url, limit=40 * 1024 * 1024):
    r = requests.get(url, timeout=(10, 120), stream=True, headers={"User-Agent": "lumio-mcp"})
    r.raise_for_status()
    out = bytearray()
    for chunk in r.iter_content(256 * 1024):
        out += chunk
        if len(out) > limit:
            raise RuntimeError("файл больше 40 МБ")
    return bytes(out), (r.headers.get("Content-Type") or "").split(";")[0]


# ---------- Seedream ----------
def _shrink(data, ctype, side=2048):
    """Референс до 2048 px по длинной стороне, JPEG q90 — чтобы запрос в BytePlus был лёгким."""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        k = side / max(h, w)
        if k < 1:
            img = cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ok:
            return buf.tobytes(), "image/jpeg"
    except Exception as e:
        print("shrink error", repr(e), flush=True)
    return data, (ctype if ctype.startswith("image/") else "image/jpeg")


def _seedream(prompt, refs, size):
    images = []
    for u in refs:
        data, ctype = _fetch(u)
        data, mime = _shrink(data, ctype)
        images.append("data:%s;base64,%s" % (mime, base64.b64encode(data).decode()))
    body = {"model": seedream.MODEL, "prompt": prompt, "size": size, "response_format": "b64_json",
            "output_format": "jpeg", "watermark": False}
    if images:
        body["image"] = images if len(images) > 1 else images[0]
    r = requests.post(seedream.BASE + "/images/generations", json=body, timeout=(120, 300),
                      headers={"Authorization": "Bearer " + seedream.KEY, "Content-Type": "application/json"})
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code != 200 or not j.get("data"):
        err = j.get("error") if isinstance(j.get("error"), dict) else {"message": j.get("error") or r.text[:400]}
        raise RuntimeError(f"BytePlus HTTP {r.status_code}: {err.get('code', '')} {err.get('message', '')}".strip())
    return base64.b64decode(j["data"][0]["b64_json"])


def _run(job_id, prompt, refs, size):
    job = _jobs[job_id]
    try:
        data = _seedream(prompt, refs, size)
        seedream._add_used(1)
        job.update(status="done", file=_save(data))
    except Exception as e:
        job.update(status="error", error=str(e)[:500])
    finally:
        _busy.release()


def _job_result(job_id, host):
    job = _jobs.get(job_id)
    if not job:
        return _err(f"Задача {job_id} не найдена (сервер мог перезапуститься).")
    if job["status"] == "running":
        return _text(f"Ещё генерируется ({int(time.time() - job['ts'])} с). Проверьте позже: seedream_result job_id={job_id}")
    if job["status"] == "error":
        return _err("Seedream ответил ошибкой: " + job["error"])
    return _image_result(job["file"], host, f"Готово. job_id={job_id}. Осталось сегодня: {seedream.left_today()}")


def gen_tool(args, host):
    prompt = (args.get("prompt") or "").strip()
    refs = [u for u in (args.get("image_urls") or []) if isinstance(u, str) and u.startswith("http")][:10]
    aspect = args.get("aspect") or "16:9"
    size = SIZES.get(aspect, aspect if "x" in str(aspect) else "2560x1440")
    if not prompt:
        return _err("Нужен prompt.")
    if not seedream.KEY:
        return _err("На сервере нет ключа: read -s K; lumio-set SEEDREAM_API_KEY \"$K\"")
    if seedream.left_today() <= 0:
        return _err(f"Дневной лимит {seedream.DAILY} картинок исчерпан (lumio-set SEEDREAM_DAILY_LIMIT N).")
    if not _busy.acquire(blocking=False):
        running = [k for k, v in _jobs.items() if v["status"] == "running"]
        return _err("Уже идёт генерация (одна за раз). job_id=" + (running[0] if running else "?"))
    job_id = secrets.token_hex(6)
    _jobs[job_id] = {"status": "running", "ts": time.time(), "prompt": prompt[:200]}
    threading.Thread(target=_run, args=(job_id, prompt, refs, size), daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < WAIT and _jobs[job_id]["status"] == "running":
        time.sleep(1)
    return _job_result(job_id, host)


def frame_tool(args, host):
    url = args.get("video_url") or ""
    at = args.get("at", "last")
    if not url.startswith("http"):
        return _err("Нужен video_url (http/https).")
    try:
        import cv2
    except ImportError:
        return _err("На сервере нет opencv: выполните lumio-update.")
    data, _ = _fetch(url, limit=300 * 1024 * 1024)
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        tmp.write(data)
        tmp.flush()
        cap = cv2.VideoCapture(tmp.name)
        fps = cap.get(cv2.CAP_PROP_FPS) or 24
        frame, idx, want = None, 0, None
        if str(at) not in ("last", ""):
            want = int(float(at) * fps)
        while True:
            ok, f = cap.read()
            if not ok:
                break
            frame = f
            if want is not None and idx >= want:
                break
            idx += 1
        cap.release()
    if frame is None:
        return _err("Не удалось прочитать кадры из видео.")
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    name = _save(buf.tobytes())
    h, w = frame.shape[:2]
    if want is None:
        idx = max(0, idx - 1)
    return _image_result(name, host, f"Кадр №{idx} ({idx / fps:.2f} с), {w}x{h}.")


# ---------- ответы MCP ----------
def _text(s):
    return {"content": [{"type": "text", "text": s}]}


def _err(s):
    return {"content": [{"type": "text", "text": s}], "isError": True}


def _image_result(name, host, note):
    with open(os.path.join(GEN_DIR, name), "rb") as f:
        data = f.read()
    content = [{"type": "text", "text": f"{note}\nПолный размер: {_url(name, host)}"}]
    pv = _preview(data)
    if pv:
        content.append({"type": "image", "data": pv, "mimeType": "image/jpeg"})
    return {"content": content}


TOOLS = [
    {"name": "seedream_generate",
     "description": "Сгенерировать ОДНУ картинку в Seedream 5.0 Pro (BytePlus, платно, ~$0.05). "
                    "Референсы — публичные ссылки (до 10), в промте на них ссылаются как image 1, image 2… по порядку. "
                    "Возвращает превью и постоянную публичную ссылку на полный размер. Перед каждым вызовом нужно согласие пользователя.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string"},
         "image_urls": {"type": "array", "items": {"type": "string"}, "description": "Ссылки на референсы по порядку"},
         "aspect": {"type": "string", "description": "16:9 (по умолчанию), 9:16, 1:1, 4:3, 3:4 или WxH"}},
         "required": ["prompt"]}},
    {"name": "seedream_result",
     "description": "Получить результат генерации, если seedream_generate ответил «ещё генерируется».",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}},
    {"name": "video_frame",
     "description": "Вырезать кадр из видео по ссылке (по умолчанию последний) и выложить его публичной ссылкой. Бесплатно.",
     "inputSchema": {"type": "object", "properties": {
         "video_url": {"type": "string"},
         "at": {"type": "string", "description": "\"last\" или секунда, напр. \"3.5\""}},
         "required": ["video_url"]}},
    {"name": "save_image",
     "description": "Скопировать картинку по ссылке на сервер Lumio и получить постоянную публичную ссылку (для Kling/Seedream). Бесплатно.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "seedream_status",
     "description": "Подключён ли ключ, модель, сколько картинок осталось на сегодня по лимиту.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _call(name, args, host):
    if name == "seedream_generate":
        return gen_tool(args, host)
    if name == "seedream_result":
        return _job_result(args.get("job_id", ""), host)
    if name == "video_frame":
        return frame_tool(args, host)
    if name == "save_image":
        data, ctype = _fetch(args.get("url", ""))
        return _image_result(_save(data, "png" if ctype == "image/png" else "jpg"), host, "Сохранено.")
    if name == "seedream_status":
        return _text(json.dumps({"key": bool(seedream.KEY), "model": seedream.MODEL, "base": seedream.BASE,
                                 "left_today": seedream.left_today(), "daily_limit": seedream.DAILY,
                                 "busy": _busy.locked()}, ensure_ascii=False))
    return _err("Нет такого инструмента: " + name)


def _one(msg, host):
    mid, method = msg.get("id"), msg.get("method", "")
    if mid is None:                       # уведомления — ответа не нужно
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        want = params.get("protocolVersion")
        result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "lumio-seedream", "version": "1.0.0"},
                  "instructions": "Seedream (BytePlus) и кадры из видео для студии Lumio. Платная генерация — "
                                  "строго одна за раз и только после согласия пользователя."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            result = _call(params.get("name", ""), params.get("arguments") or {}, host)
        except Exception as e:
            result = _err("Ошибка: " + str(e)[:400])
    elif method in ("resources/list", "prompts/list"):
        result = {method.split("/")[0]: []}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: " + method}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def handle(raw, host):
    """POST /mcp/<secret>: возвращает (status, body_bytes_or_None)."""
    try:
        msg = json.loads(raw or b"{}")
    except ValueError:
        return 400, json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}).encode()
    if isinstance(msg, list):
        out = [r for r in (_one(m, host) for m in msg) if r]
    else:
        out = _one(msg, host)
    if not out:
        return 202, None
    return 200, json.dumps(out, ensure_ascii=False).encode("utf-8")
