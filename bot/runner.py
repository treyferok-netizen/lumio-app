"""Runs the Lumio bot on an ordinary server (VPS) instead of Yandex Cloud Functions.

- Telegram: long polling (the server only connects out to Telegram, no webhook needed).
- Mini App orders: small HTTP server on 127.0.0.1:8080, put behind Caddy for HTTPS.
- YandexGPT: through YANDEX_API_KEY (an API key of the Yandex Cloud service account).

All business logic lives in index.py; this file only feeds it events.
"""
import base64
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ads
import index
import mcp_server

MAX_BODY = 12 * 1024 * 1024
DENIED = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
          "<body style='font:16px system-ui;padding:40px;max-width:520px;margin:auto'>"
          "<h2>Кабинет рекламы Lumio</h2><p>Ссылка для входа устарела или неверная. "
          "Попросите новую: команда <b>/cabinet</b> в боте Lumio.</p>").encode()


class Ctx:  # no Yandex service-account token here; index uses YANDEX_API_KEY instead
    token = None


CTX = Ctx()


def poll():
    index.tg("deleteWebhook", {"drop_pending_updates": False})
    me = index.tg("getMe", {})
    print("polling as", (me.get("result") or {}).get("username"), "ok" if me.get("ok") else me, flush=True)
    prof = index.setup_profile()
    print("profile", {k: v.get("ok") for k, v in prof.items()}, flush=True)
    offset = None
    while True:
        q = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            q["offset"] = offset
        res = index.tg("getUpdates", q, timeout=(10, 65))
        if not res.get("ok"):
            print("getUpdates failed:", res.get("error") or res.get("description"), flush=True)
            time.sleep(5)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                index.handle_update(upd, CTX)
            except Exception as e:
                print("update error", repr(e), flush=True)


class Web(BaseHTTPRequestHandler):
    def _send(self, resp):
        self.send_response(resp["statusCode"])
        for k, v in (resp.get("headers") or {}).items():
            self.send_header(k, v)
        self.end_headers()
        body = resp.get("body") or ""
        self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    def _event(self, method):
        path, _, query = self.path.partition("?")
        qs = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path.rstrip("/").endswith("/order"):
            qs["src"] = "order"
        elif path.rstrip("/").endswith("/check"):
            qs["src"] = "check"
        elif path.rstrip("/").endswith("/idea"):
            qs["src"] = "idea"
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return None
        raw = self.rfile.read(n) if n else b""
        return {"httpMethod": method, "queryStringParameters": qs, "headers": dict(self.headers),
                "body": base64.b64encode(raw).decode(), "isBase64Encoded": True}

    def do_OPTIONS(self):
        self._send({"statusCode": 204, "headers": index.CORS, "body": ""})

    def _raw(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    # ------------------------------------------------ кабинет рекламы (/cabinet)
    def _cab_ok(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "lc" and ads.check(v):
                return True
        return False

    def _cabinet(self, method):
        path, _, query = self.path.partition("?")
        qs = dict(urllib.parse.parse_qsl(query))
        if path.rstrip("/") == "/cabinet" and method == "GET":
            if qs.get("k"):
                if not ads.check(qs["k"]):
                    return self._raw(403, DENIED, "text/html; charset=utf-8")
                self.send_response(302)
                self.send_header("Set-Cookie", f"lc={qs['k']}; Path=/cabinet; Max-Age=31536000; HttpOnly; Secure; SameSite=Lax")
                self.send_header("Location", "/cabinet")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._cab_ok():
                return self._raw(403, DENIED, "text/html; charset=utf-8")
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cabinet.html"), "rb") as f:
                return self._raw(200, f.read(), "text/html; charset=utf-8")
        if not self._cab_ok():
            return self._raw(403, b'{"ok":false,"error":"auth"}')
        dfrom, dto = qs.get("from") or None, qs.get("to") or None
        js = lambda o: self._raw(200, json.dumps(o, ensure_ascii=False).encode(), "application/json; charset=utf-8")  # noqa: E731

        def csvf(name, data):
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        try:
            if method == "GET" and path == "/cabinet/api/report":
                return js(ads.report(dfrom, dto))
            if method == "GET" and path == "/cabinet/api/people":
                return js({"ok": True, "people": ads.people(qs.get("code", ""), dfrom, dto)})
            if method == "GET" and path == "/cabinet/links.csv":
                return csvf("lumio_reklama_ssylki.csv", ads.links_csv())
            if method == "GET" and path == "/cabinet/report.csv":
                return csvf("lumio_reklama_otchet.csv", ads.report_csv(dfrom, dto))
            if method == "POST" and path in ("/cabinet/api/link", "/cabinet/api/add"):
                if "json" not in (self.headers.get("Content-Type") or ""):
                    return self._raw(415)
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                if path.endswith("/add"):
                    ads.add_links(data.get("n", 10))
                    return js({"ok": True})
                row = ads.update_link(str(data.get("code", "")), data)
                return js({"ok": bool(row), "row": row})
        except (ValueError, KeyError) as e:
            return self._raw(400, json.dumps({"ok": False, "error": str(e)}).encode())
        return self._raw(404, b'{"error":"not found"}')

    def do_POST(self):
        path = self.path.partition("?")[0]
        if path.startswith("/cabinet"):
            return self._cabinet("POST")
        if path.startswith("/mcp/"):
            if not mcp_server.path_ok(path):
                return self._raw(404, b'{"error":"not found"}')
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._raw(413)
            code, body = mcp_server.handle(self.rfile.read(n) if n else b"", self.headers.get("Host", ""))
            return self._raw(code, body or b"")
        ev = self._event("POST")
        if ev is None:
            return self._send(index.respond(413, {"ok": False, "error": "too large"}))
        if ev["queryStringParameters"].get("src") not in ("order", "check", "idea"):
            return self._send(index.respond(404, {"ok": False}))
        self._send(index.handler(ev, CTX))

    def do_DELETE(self):
        self._raw(405 if self.path.startswith("/mcp/") else 404)

    def do_GET(self):
        path = self.path.partition("?")[0]
        if path.startswith("/cabinet"):
            return self._cabinet("GET")
        if path.startswith("/.well-known/"):
            return self._raw(404, b'{"error":"not found"}')
        if path.startswith("/mcp/"):
            return self._raw(405 if mcp_server.path_ok(path) else 404)
        if path.startswith("/files/"):
            fp = mcp_server.file_path(path[len("/files/"):])
            if not fp:
                return self._raw(404, b'{"error":"not found"}')
            with open(fp, "rb") as f:
                data = f.read()
            ctype = {".mp4": "video/mp4", ".png": "image/png", ".mp3": "audio/mpeg", ".wav": "audio/wav",
                     ".m4a": "audio/mp4"}.get(os.path.splitext(fp)[1], "image/jpeg")
            if "download" in self.path.partition("?")[2]:   # ?download=1 — браузер скачает файл
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(fp)}"')
                self.end_headers()
                self.wfile.write(data)
                return
            return self._raw(200, data, ctype)
        ev = self._event("GET")
        src = (ev or {}).get("queryStringParameters", {}).get("src")
        if src == "diag":
            return self._send(index.handler(ev, CTX))
        self._send(index.respond(200, {"ok": True, "service": "lumio-bot"}))

    def log_message(self, fmt, *args):
        print("http", self.address_string(), fmt % args, flush=True)


if __name__ == "__main__":
    threading.Thread(target=poll, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 8080), Web)
    print("orders on 127.0.0.1:8080; mcp", "on" if mcp_server.enabled() else "off (MCP_SECRET)", flush=True)
    srv.serve_forever()
