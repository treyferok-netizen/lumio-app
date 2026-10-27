"""Runs the Lumio bot on an ordinary server (VPS) instead of Yandex Cloud Functions.

- Telegram: long polling (the server only connects out to Telegram, no webhook needed).
- Mini App orders: small HTTP server on 127.0.0.1:8080, put behind Caddy for HTTPS.
- YandexGPT: through YANDEX_API_KEY (an API key of the Yandex Cloud service account).

All business logic lives in index.py; this file only feeds it events.
"""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import index

MAX_BODY = 12 * 1024 * 1024


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
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return None
        raw = self.rfile.read(n) if n else b""
        return {"httpMethod": method, "queryStringParameters": qs, "headers": dict(self.headers),
                "body": base64.b64encode(raw).decode(), "isBase64Encoded": True}

    def do_OPTIONS(self):
        self._send({"statusCode": 204, "headers": index.CORS, "body": ""})

    def do_POST(self):
        ev = self._event("POST")
        if ev is None:
            return self._send(index.respond(413, {"ok": False, "error": "too large"}))
        if ev["queryStringParameters"].get("src") != "order":
            return self._send(index.respond(404, {"ok": False}))
        self._send(index.handler(ev, CTX))

    def do_GET(self):
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
    print("orders on 127.0.0.1:8080", flush=True)
    srv.serve_forever()
