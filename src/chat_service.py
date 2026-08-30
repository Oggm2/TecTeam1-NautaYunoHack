"""Read-only conversational analytics over the payment-control-tower data.

The LLM never receives ``history.jsonl`` or ``live_transactions.jsonl``.  It
can only request one of the narrow aggregate tools defined below; Python
executes that request locally and returns the resulting metrics to the model.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import incident_memory
from detector import CONVERSION_STATUSES, parse_timestamp


FILTER_KEYS = ("merchant", "provider", "country", "payment_method", "issuing_bank")
MAX_HISTORY_MESSAGES = 6

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "name": "get_overview", "description": "Obtiene el estado global actual, KPIs, conversión y analítica agregada.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "query_segment", "description": "Consulta conversiones y GMV agregados de un segmento. Úsala para comparar merchant, provider, país, método o banco contra el histórico.", "parameters": {"type": "object", "properties": {
        "filters": {"type": "object", "description": "Dimensiones exactas a filtrar. Omite las que no apliquen.", "properties": {key: {"type": "string"} for key in FILTER_KEYS}, "additionalProperties": False},
        "live_minutes": {"type": "integer", "description": "Ventana del stream vivo, entre 1 y 120 minutos. Omítela para todo el stream retenido."}
    }, "additionalProperties": False}},
    {"type": "function", "name": "get_incidents", "description": "Lista los incidentes activos priorizados, con segmento, caída, costo, confianza y recomendación.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "get_incident_details", "description": "Obtiene la evidencia de un incidente concreto. Primero usa get_incidents para conocer su incident_id.", "parameters": {"type": "object", "properties": {"incident_id": {"type": "string"}}, "required": ["incident_id"], "additionalProperties": False}},
    {"type": "function", "name": "search_incident_memory", "description": "Busca incidentes resueltos similares en la memoria. Puede filtrarse por dimensiones conocidas.", "parameters": {"type": "object", "properties": {
        "filters": {"type": "object", "properties": {key: {"type": "string"} for key in FILTER_KEYS}, "additionalProperties": False}
    }, "additionalProperties": False}},
]


class DataRepository:
    """Caches local JSONL data while the control server process is alive."""

    def __init__(self, history_path: str = "data/history.jsonl", live_path: str = "data/live_transactions.jsonl", dashboard_path: str = "frontend/dashboard_data.json", memory_path: str = "data/incident_memory.json") -> None:
        self.history_path, self.live_path = Path(history_path), Path(live_path)
        self.dashboard_path, self.memory_path = Path(dashboard_path), Path(memory_path)
        self._history_mtime: int | None = None
        self._history: list[dict[str, Any]] = []
        self._live_mtime: int | None = None
        self._live: list[dict[str, Any]] = []

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        raw = path.read_bytes()
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        return [json.loads(line) for line in raw.decode(encoding).splitlines() if line.strip()]

    def _events(self, source: str) -> list[dict[str, Any]]:
        path = self.history_path if source == "history" else self.live_path
        current_mtime = path.stat().st_mtime_ns if path.exists() else None
        if source == "history" and current_mtime != self._history_mtime:
            self._history, self._history_mtime = self._read_jsonl(path), current_mtime
        if source == "live" and current_mtime != self._live_mtime:
            self._live, self._live_mtime = self._read_jsonl(path), current_mtime
        return self._history if source == "history" else self._live

    def dashboard(self) -> dict[str, Any]:
        if not self.dashboard_path.exists():
            return {}
        return json.loads(self.dashboard_path.read_text(encoding="utf-8"))

    @staticmethod
    def _matches(event: dict[str, Any], filters: dict[str, str]) -> bool:
        return all(str(event.get(key, "")).casefold() == str(value).casefold() for key, value in filters.items() if key in FILTER_KEYS)

    @staticmethod
    def _metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
        terminal = [event for event in events if event.get("status") in CONVERSION_STATUSES]
        attempts = len(terminal)
        approved = sum(event.get("status") == "approved" for event in terminal)
        amount = sum(float(event.get("amount_usd", 0) or 0) for event in terminal)
        return {
            "attempts": attempts, "approved": approved,
            "conversion_pct": round(100 * approved / attempts, 2) if attempts else None,
            "gmv_usd": round(amount, 2), "average_ticket_usd": round(amount / attempts, 2) if attempts else None,
        }

    def query_segment(self, filters: dict[str, str] | None = None, live_minutes: int | None = None) -> dict[str, Any]:
        filters = {key: str(value) for key, value in (filters or {}).items() if key in FILTER_KEYS and value}
        history = [event for event in self._events("history") if self._matches(event, filters)]
        live = [event for event in self._events("live") if self._matches(event, filters)]
        if live_minutes:
            live_minutes = max(1, min(int(live_minutes), 120))
            timestamps = [parse_timestamp(event.get("completed_at") or event.get("created_at")) for event in live]
            latest = max((stamp for stamp in timestamps if stamp), default=None)
            if latest:
                cutoff = latest - timedelta(minutes=live_minutes)
                live = [event for event in live if (parse_timestamp(event.get("completed_at") or event.get("created_at")) or cutoff) >= cutoff]
        h, l = self._metrics(history), self._metrics(live)
        delta = None if h["conversion_pct"] is None or l["conversion_pct"] is None else round(l["conversion_pct"] - h["conversion_pct"], 2)
        return {"source": "local historical baseline + local live stream", "filters": filters or "all transactions", "live_window_minutes": live_minutes, "historical": h, "live": l, "conversion_delta_pp_live_minus_historical": delta}

    def overview(self) -> dict[str, Any]:
        dashboard = self.dashboard()
        return {"source": "dashboard snapshot generated from local data", "generated_at": dashboard.get("generated_at"), "kpis": dashboard.get("kpis", {}), "analytics": dashboard.get("analytics", {})}

    def incidents(self) -> dict[str, Any]:
        entries = self.dashboard().get("incidents", [])
        compact = []
        for entry in entries:
            diagnosis = entry.get("diagnosis", {})
            metrics = diagnosis.get("root_metrics", {})
            compact.append({
                "incident_id": entry.get("incident_id"), "severity": entry.get("severity"), "status": entry.get("status"),
                "root_cause_segment": entry.get("root_cause_segment"), "conversion_drop_pp": metrics.get("conversion_drop_pp"),
                "cost_per_hour_usd": entry.get("cost_per_hour_usd"), "confidence_pct": entry.get("confidence_pct"),
                "recommended_action": diagnosis.get("recommended_action"),
            })
        return {"source": "current dashboard diagnosis", "incidents": compact}

    def incident_details(self, incident_id: str) -> dict[str, Any]:
        for entry in self.dashboard().get("incidents", []):
            if entry.get("incident_id") == incident_id:
                return {"source": "current dashboard diagnosis", "incident": entry}
        return {"source": "current dashboard diagnosis", "error": "Incident not found"}

    def memory(self, filters: dict[str, str] | None = None) -> dict[str, Any]:
        filters = {key: str(value) for key, value in (filters or {}).items() if key in FILTER_KEYS and value}
        records = incident_memory.load(str(self.memory_path))
        matched = [record for record in records if all(str(record.get("root_cause_segment", {}).get(key, "")).casefold() == value.casefold() for key, value in filters.items())]
        return {"source": "local incident memory", "filters": filters or "all resolved incidents", "matches": matched[:12], "total_matches": len(matched)}


class ChatService:
    def __init__(self, repository: DataRepository | None = None, model: str = "gpt-5") -> None:
        self.repository = repository or DataRepository()
        self.model = model

    def _tool_result(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_overview": return self.repository.overview()
        if name == "query_segment": return self.repository.query_segment(arguments.get("filters"), arguments.get("live_minutes"))
        if name == "get_incidents": return self.repository.incidents()
        if name == "get_incident_details": return self.repository.incident_details(arguments["incident_id"])
        if name == "search_incident_memory": return self.repository.memory(arguments.get("filters"))
        return {"error": f"Unknown read-only tool: {name}"}

    def ask(self, question: str, audience: str = "technical", history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not configured on the server.")
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError("Install the OpenAI Python SDK first: py -m pip install openai") from error
        audience = audience if audience in {"technical", "financial", "simple"} else "technical"
        prior = [{"role": item["role"], "content": item["content"][:1200]} for item in (history or [])[-MAX_HISTORY_MESSAGES:] if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)]
        input_items: list[Any] = [*prior, {"role": "user", "content": question[:2000]}]
        instructions = f"""Eres PagoTotal Intelligence, un asistente de operaciones de pagos. Responde en español para una audiencia {audience}.
Usa obligatoriamente las herramientas de solo lectura antes de afirmar una métrica, incidente, comparación o recurrencia. Las herramientas devuelven hechos calculados localmente; no inventes datos ni causas. Distingue histórico, stream vivo y memoria. Si el volumen es insuficiente o la herramienta no aporta evidencia, dilo claramente. Nunca ejecutes cambios de enrutamiento ni sugieras que ya se ejecutaron.
La respuesta final siempre debe ser completa: responde directamente la pregunta en la primera oración. Si no hay incidentes o coincidencias, dilo de forma explícita (por ejemplo: “No hay incidentes activos detectados todavía”). Nunca devuelvas solo un título, una lista vacía o la frase “Con la evidencia disponible”. Máximo dos herramientas por pregunta, salvo que una herramienta señale un error."""
        client = OpenAI()
        used_sources: list[str] = []
        tool_results: list[tuple[str, dict[str, Any]]] = []

        def execute_calls(calls: list[Any]) -> None:
            input_items.extend(response.output)
            for call in calls:
                try:
                    result = self._tool_result(call.name, json.loads(call.arguments))
                except (ValueError, KeyError, TypeError) as error:
                    result = {"error": f"Invalid tool arguments: {error}"}
                tool_results.append((call.name, result))
                if result.get("source"):
                    used_sources.append(result["source"])
                input_items.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result, ensure_ascii=False)})

        def fallback_answer() -> str:
            """Guarantee a useful, factual response if the model returns no final text."""
            for name, result in reversed(tool_results):
                if name == "get_incidents":
                    active = [item for item in result.get("incidents", []) if item.get("status") == "active"]
                    return "No hay incidentes activos detectados todavía." if not active else f"Hay {len(active)} incidente(s) activo(s) detectado(s)."
                if name == "search_incident_memory":
                    matches = result.get("matches", [])
                    return "No encontré incidentes previos que coincidan con esa búsqueda." if not matches else f"Encontré {len(matches)} incidente(s) registrado(s) en la memoria para esa búsqueda."
                if name == "query_segment":
                    live, historical = result.get("live", {}), result.get("historical", {})
                    return f"El segmento consultado tiene conversión viva de {live.get('conversion_pct') if live.get('conversion_pct') is not None else 'sin muestra suficiente'} frente a {historical.get('conversion_pct') if historical.get('conversion_pct') is not None else 'sin línea base suficiente'} en el histórico."
            return "No pude obtener evidencia suficiente de las fuentes locales para responder esa pregunta."

        response = client.responses.create(model=self.model, instructions=instructions, input=input_items, tools=TOOLS, tool_choice="required", parallel_tool_calls=False, store=False, max_output_tokens=700)
        for _ in range(2):
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                break
            execute_calls(calls)
            response = client.responses.create(model=self.model, instructions=instructions, input=input_items, tools=TOOLS, tool_choice="auto", parallel_tool_calls=False, store=False, max_output_tokens=700)
        # Do not end on a tool call: after the bounded tool budget, make one final
        # text-only turn so the user always gets a completed answer.
        pending_calls = [item for item in response.output if item.type == "function_call"]
        if pending_calls:
            execute_calls(pending_calls)
            response = client.responses.create(model=self.model, instructions=instructions, input=input_items, tools=TOOLS, tool_choice="none", parallel_tool_calls=False, store=False, max_output_tokens=700)
        answer = response.output_text.strip() or fallback_answer()
        return {"answer": answer, "sources": list(dict.fromkeys(used_sources)), "response_id": response.id}
