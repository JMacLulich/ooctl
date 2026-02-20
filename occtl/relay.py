from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, tmux

SESSION_RE = re.compile(r"^[a-zA-Z0-9._:-]+$")


def _send_json(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _parse_auth_token(header_value: str) -> str:
    if not header_value:
        return ""
    parts = header_value.strip().split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def serve(*, host: str, port: int, token: str) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *_args) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/health":
                _send_json(self, 200, {"status": "ok", "service": "oc-relay"})
                return
            _send_json(self, 404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/continue":
                _send_json(self, 404, {"error": "not found"})
                return

            sent_token = _parse_auth_token(self.headers.get("Authorization", ""))
            if not token or sent_token != token:
                _send_json(self, 401, {"error": "unauthorized"})
                return

            content_len = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                _send_json(self, 400, {"error": "invalid json"})
                return

            session = str(payload.get("session") or config.get_focus() or "").strip()
            text = str(payload.get("text") or "continue").strip()
            press_enter = bool(payload.get("press_enter", True))

            if not session:
                _send_json(self, 400, {"error": "session required"})
                return
            if not SESSION_RE.match(session):
                _send_json(self, 400, {"error": "invalid session"})
                return
            if not tmux.has_session(session):
                _send_json(self, 404, {"error": "session not found"})
                return

            if text:
                tmux.send_keys(f"{session}:main", [text])
            if press_enter:
                tmux.send_keys(f"{session}:main", ["Enter"])

            _send_json(
                self,
                200,
                {
                    "ok": True,
                    "session": session,
                    "sent_text": text,
                    "pressed_enter": press_enter,
                },
            )

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"relay listening on http://{host}:{port}")
    server.serve_forever()
