"""Serve the dashboard and safely persist runtime detection controls."""
from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ALLOWED = {"window_seconds", "evaluation_seconds", "persistence", "min_attempts", "min_history_attempts", "min_drop_pp", "z_threshold"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str, config_path: Path, **kwargs):
        self.config_path = config_path
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--frontend", default="frontend")
    parser.add_argument("--config", default="data/runtime_config.json")
    args = parser.parse_args()
    handler = lambda *a, **kw: Handler(*a, directory=args.frontend, config_path=Path(args.config), **kw)
    print(f"Dashboard controls available at http://localhost:{args.port}")
    ThreadingHTTPServer(("", args.port), handler).serve_forever()
