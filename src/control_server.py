"""Serve the dashboard and safely persist runtime detection controls."""
from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from chat_service import ChatService

ALLOWED = {"window_seconds", "evaluation_seconds", "persistence", "min_attempts", "min_history_attempts", "min_drop_pp", "z_threshold"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str, config_path: Path, chat_service: ChatService, **kwargs):
        self.config_path = config_path
        self.chat_service = chat_service
        super().__init__(*args, directory=directory, **kwargs)

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/config":
            self._json(HTTPStatus.OK, json.loads(self.config_path.read_text(encoding="utf-8")))
            return
        super().do_GET()

    def do_PUT(self) -> None:
        if self.path != "/api/config":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if set(payload) != ALLOWED or any(not isinstance(value, (int, float)) or value <= 0 for value in payload.values()):
                raise ValueError("invalid configuration")
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.config_path)
            self._json(HTTPStatus.OK, payload)
        except (ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid configuration"})

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            question = payload.get("question", "")
            audience = payload.get("audience", "technical")
            history = payload.get("history", [])
            if not isinstance(question, str) or not question.strip() or len(question) > 2000:
                raise ValueError("question must be non-empty and at most 2000 characters")
            if not isinstance(history, list) or not isinstance(audience, str):
                raise ValueError("invalid chat payload")
            self._json(HTTPStatus.OK, self.chat_service.ask(question.strip(), audience, history))
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RuntimeError as error:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The chat service could not complete this request."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--frontend", default="frontend")
    parser.add_argument("--config", default="data/runtime_config.json")
    parser.add_argument("--chat-model", default="gpt-5")
    args = parser.parse_args()
    chat_service = ChatService(model=args.chat_model)
    handler = lambda *a, **kw: Handler(*a, directory=args.frontend, config_path=Path(args.config), chat_service=chat_service, **kw)
    print(f"Dashboard controls available at http://localhost:{args.port}")
    ThreadingHTTPServer(("", args.port), handler).serve_forever()
