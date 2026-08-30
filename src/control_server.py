"""Serve the dashboard and safely persist runtime detection controls."""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from chat_service import ChatService
import generator

DETECTION_FIELDS = {"window_seconds", "evaluation_seconds", "persistence", "min_attempts", "min_history_attempts", "min_drop_pp", "z_threshold"}
WEIGHT_FIELDS = {"financial", "urgency", "conversion_drop", "merchant"}
MERCHANTS = {"PagoModa", "TravelNow", "TechStore"}
PRESETS = {"financial", "balanced", "strategic_accounts", "conversion_protection", "custom"}
DEFAULT_CONFIG = {
    "window_seconds": 300, "evaluation_seconds": 30, "persistence": 3, "min_attempts": 30,
    "min_history_attempts": 200, "min_drop_pp": 5.0, "z_threshold": 3.0,
    "priority_preset": "balanced",
    "priority_weights": {"financial": 50, "urgency": 25, "conversion_drop": 15, "merchant": 10},
    "merchant_multipliers": {"PagoModa": 1.0, "TravelNow": 1.0, "TechStore": 1.0},
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str, config_path: Path, trial_path: Path, chat_service: ChatService, **kwargs):
        self.config_path = config_path
        self.trial_path = trial_path
        self.chat_service = chat_service
        super().__init__(*args, directory=directory, **kwargs)

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _config(self) -> dict:
        """Allow old detection-only config files to upgrade safely in memory."""
        stored = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}
        return {
            **DEFAULT_CONFIG, **stored,
            "priority_weights": {**DEFAULT_CONFIG["priority_weights"], **stored.get("priority_weights", {})},
            "merchant_multipliers": {**DEFAULT_CONFIG["merchant_multipliers"], **stored.get("merchant_multipliers", {})},
        }

    def _trial_injections(self) -> list[dict]:
        if not self.trial_path.exists():
            return []
        raw = json.loads(self.trial_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []

    @staticmethod
    def _valid(payload: object) -> bool:
        if not isinstance(payload, dict) or set(payload) != set(DEFAULT_CONFIG):
            return False
        if any(not isinstance(payload[field], (int, float)) or payload[field] <= 0 for field in DETECTION_FIELDS):
            return False
        weights, merchants = payload.get("priority_weights"), payload.get("merchant_multipliers")
        return (
            payload.get("priority_preset") in PRESETS
            and isinstance(weights, dict) and set(weights) == WEIGHT_FIELDS
            and all(isinstance(value, (int, float)) and value >= 0 for value in weights.values()) and sum(weights.values()) > 0
            and isinstance(merchants, dict) and set(merchants) == MERCHANTS
            and all(isinstance(value, (int, float)) and 0.5 <= value <= 3 for value in merchants.values())
        )

    def do_GET(self) -> None:
        if self.path == "/api/config":
            self._json(HTTPStatus.OK, self._config())
            return
        if self.path == "/api/trial-injections":
            self._json(HTTPStatus.OK, {"injections": self._trial_injections()})
            return
        super().do_GET()

    def do_PUT(self) -> None:
        if self.path != "/api/config":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if not self._valid(payload):
                raise ValueError("invalid configuration")
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.config_path)
            self._json(HTTPStatus.OK, payload)
        except (ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid configuration"})

    def do_POST(self) -> None:
        if self.path == "/api/trial-injections":
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                allowed_filters = {"merchant", "provider", "payment_method", "country", "issuing_bank"}
                filters = payload.get("filters", {})
                if not isinstance(filters, dict) or not filters or set(filters) - allowed_filters:
                    raise ValueError("select at least one valid transaction dimension")
                if any(not isinstance(value, str) or not value for value in filters.values()):
                    raise ValueError("invalid injection filter")
                approval_rate, traffic_share, duration = float(payload.get("approval_rate")), float(payload.get("traffic_share")), int(payload.get("duration_seconds"))
                reason = payload.get("decline_reason")
                if not 0 <= approval_rate <= 1 or not 0.05 <= traffic_share <= 0.9 or not 30 <= duration <= 900:
                    raise ValueError("approval rate, traffic share or duration out of range")
                if reason not in generator.REASON_TO_CODE:
                    raise ValueError("invalid decline reason")
                injection = {"id": str(uuid.uuid4()), "name": str(payload.get("name") or "Judge live trial")[:100], "filters": filters,
                    "approval_rate": approval_rate, "traffic_share": traffic_share, "duration_seconds": duration,
                    "decline_reason": reason, "activated_at": datetime.now(UTC).isoformat()}
                injections = self._trial_injections()
                injections.append(injection)
                self.trial_path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.trial_path.with_suffix(".tmp")
                temp.write_text(json.dumps(injections, indent=2), encoding="utf-8")
                temp.replace(self.trial_path)
                self._json(HTTPStatus.CREATED, injection)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
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

    def do_DELETE(self) -> None:
        prefix = "/api/trial-injections/"
        if not self.path.startswith(prefix):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        injection_id = self.path[len(prefix):]
        injections = self._trial_injections()
        kept = [injection for injection in injections if injection.get("id") != injection_id]
        if len(kept) == len(injections):
            self._json(HTTPStatus.NOT_FOUND, {"error": "injection not found"})
            return
        temp = self.trial_path.with_suffix(".tmp")
        temp.write_text(json.dumps(kept, indent=2), encoding="utf-8")
        temp.replace(self.trial_path)
        self._json(HTTPStatus.OK, {"deleted": injection_id})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--frontend", default="frontend")
    parser.add_argument("--config", default="data/runtime_config.json")
    parser.add_argument("--trial-injections", default="data/live_injections.json")
    parser.add_argument("--chat-model", default="gpt-5")
    args = parser.parse_args()
    chat_service = ChatService(model=args.chat_model)
    handler = lambda *a, **kw: Handler(*a, directory=args.frontend, config_path=Path(args.config), trial_path=Path(args.trial_injections), chat_service=chat_service, **kw)
    print(f"Dashboard controls available at http://localhost:{args.port}")
    ThreadingHTTPServer(("", args.port), handler).serve_forever()
