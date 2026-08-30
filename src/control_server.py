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
import incident_lifecycle
import incident_memory
from urllib.parse import unquote

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
    def __init__(self, *args, directory: str, config_path: Path, trial_path: Path, lifecycle_path: Path, memory_path: Path, operational_events_path: Path, chat_service: ChatService, **kwargs):
        self.config_path = config_path
        self.trial_path = trial_path
        self.lifecycle_path = lifecycle_path
        self.memory_path = memory_path
        self.operational_events_path = operational_events_path
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

    def _operational_events(self) -> list[dict]:
        if not self.operational_events_path.exists():
            return []
        try:
            raw = json.loads(self.operational_events_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return raw if isinstance(raw, list) else []

    def _body(self) -> dict:
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def _incident_action(self, suffix: str) -> tuple[str, str] | None:
        prefix = "/api/incidents/"
        if not self.path.startswith(prefix) or not self.path.endswith(suffix):
            return None
        key = unquote(self.path[len(prefix):-len(suffix)]).strip("/")
        return (key, suffix.strip("/")) if key else None

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
        if self.path == "/api/operational-events":
            self._json(HTTPStatus.OK, {"events": self._operational_events()})
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
        action = self._incident_action("/monitor")
        if action:
            try:
                key, _ = action
                state = incident_lifecycle.load(self.lifecycle_path)
                record = incident_lifecycle.transition_to_monitoring(state, key, self._body())
                incident_lifecycle.save(self.lifecycle_path, state)
                self._json(HTTPStatus.OK, {"incident": record})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        action = self._incident_action("/resolve")
        if action:
            try:
                key, _ = action
                state = incident_lifecycle.load(self.lifecycle_path)
                record = incident_lifecycle.resolve_by_operator(state, key, self._body())
                incident_lifecycle.save(self.lifecycle_path, state)
                memory = incident_memory.load(self.memory_path)
                memory = incident_memory.upsert(memory, incident_lifecycle.record_for_memory(record))
                incident_memory.save(self.memory_path, memory)
                self._json(HTTPStatus.OK, {"incident": record, "memory_recorded": True})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if self.path == "/api/operational-events":
            try:
                payload = self._body()
                allowed_filters = {"merchant", "provider", "payment_method", "country", "issuing_bank"}
                filters = payload.get("filters", {})
                if not isinstance(filters, dict) or set(filters) - allowed_filters:
                    raise ValueError("invalid operational-event filters")
                source = str(payload.get("source") or "manual context").strip()[:80]
                title = str(payload.get("title") or "Operational signal").strip()[:160]
                detail = str(payload.get("detail") or "").strip()[:1000]
                verification = str(payload.get("verification") or "observed")
                if not source or not title or verification not in {"observed", "hypothesis", "confirmed"}:
                    raise ValueError("invalid operational-event fields")
                event = {"id": str(uuid.uuid4()), "at": str(payload.get("at") or datetime.now(UTC).isoformat()), "source": source, "title": title, "detail": detail, "verification": verification, "filters": filters}
                events = self._operational_events()
                events.append(event)
                self.operational_events_path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.operational_events_path.with_suffix(".tmp")
                temp.write_text(json.dumps(events, indent=2), encoding="utf-8")
                temp.replace(self.operational_events_path)
                self._json(HTTPStatus.CREATED, event)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
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
                    "decline_reason": reason, "activated_at": datetime.now(UTC).isoformat(),
                    "direct_alert": True}
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
    parser.add_argument("--lifecycle", default="data/incident_lifecycle.json")
    parser.add_argument("--memory", default="data/incident_memory.json")
    parser.add_argument("--operational-events", default="data/operational_events.json")
    parser.add_argument("--chat-model", default="gpt-5")
    args = parser.parse_args()
    chat_service = ChatService(model=args.chat_model)
    handler = lambda *a, **kw: Handler(*a, directory=args.frontend, config_path=Path(args.config), trial_path=Path(args.trial_injections), lifecycle_path=Path(args.lifecycle), memory_path=Path(args.memory), operational_events_path=Path(args.operational_events), chat_service=chat_service, **kw)
    print(f"Dashboard controls available at http://localhost:{args.port}")
    ThreadingHTTPServer(("", args.port), handler).serve_forever()
