"""Turn a deterministic diagnosis into executive and operational explanations.

Without flags, this module uses safe deterministic templates. With --use-openai
it asks an LLM only to rewrite supplied facts into Spanish; it cannot diagnose
or choose remediation because those are already decided in diagnoser.py.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_diagnosis(path: str) -> dict[str, Any]:
    content = Path(path).read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("The diagnosis file is empty.")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return json.loads(content.splitlines()[-1])


def format_segment(segment: dict[str, str]) -> str:
    labels = {
        "merchant": "merchant", "provider": "provider", "payment_method": "método",
        "country": "país", "issuing_bank": "banco emisor",
    }
    return ", ".join(f"{labels.get(key, key)}={value}" for key, value in segment.items())


def format_recurrence_note(recurrence: dict[str, Any] | None) -> str:
    if not recurrence:
        return ""
    previous = recurrence["previous_incident"]
    when = previous.get("resolved_at") or previous.get("observed_at")
    when_label = when[:10] if when else "una ocasión anterior"
    similarity_pct = round(recurrence["similarity"] * 100)
    note = previous.get("resolution_note")
    tail = f"; en esa ocasión: {note}." if note else "."
    return f" Este patrón ya se había observado ({similarity_pct}% de similitud) el {when_label}{tail}"


def deterministic_explanation(diagnosis: dict[str, Any], recurrence: dict[str, Any] | None = None) -> dict[str, Any]:
    segment = diagnosis.get("root_cause_segment", {})
    metrics = diagnosis.get("root_metrics", {})
    decline = diagnosis.get("dominant_decline")
    confidence = diagnosis.get("confidence", "low")
    enough = diagnosis.get("evidence_sufficient", False)
    target = format_segment(segment)
    observed = metrics.get("observed_conversion")
    expected = metrics.get("expected_conversion")
    cost = metrics.get("expected_unrecovered_amount_usd", 0)
    window = diagnosis.get("incident_window", {})
    recurrence = recurrence if recurrence is not None else diagnosis.get("recurrence")

    if not enough:
        executive = "Se confirmó una caída de conversión, pero la evidencia no permite atribuirla a una causa única."
        operational = diagnosis.get("reason", "La pérdida está distribuida entre varios segmentos.")
    else:
        executive = f"Incidente en {target}: GMV no recuperado esperado de USD {cost:,.2f} en la ventana analizada."
        operational = (
            f"La conversión observada fue {observed:.1%}, frente a {expected:.1%} esperada "
            f"({metrics.get('conversion_drop_pp', 0):.1f} puntos porcentuales menos) en {target}."
        )
        if decline:
            operational += (
                f" El motivo de rechazo con mayor exceso fue {decline['decline_reason']} "
                f"({decline['share_of_excess_declines']:.0%} de los rechazos adicionales)."
            )
        operational += format_recurrence_note(recurrence)
    return {
        "mode": "deterministic",
        "incident_id": diagnosis.get("incident_id"),
        "executive_summary": executive,
        "operational_explanation": operational,
        "recommended_action": diagnosis.get("recommended_action"),
        "confidence": confidence,
        "evidence_sufficient": enough,
        "window_started_at": window.get("started_at"),
        "window_ended_at": window.get("ended_at"),
        "profiles": {
            "technical": operational,
            "financial": f"El GMV no recuperado esperado es USD {cost:,.2f} en esta ventana; la proyección por hora está en las métricas del incidente.",
            "simple": f"Hay un problema en {target}: están aprobándose menos pagos de lo normal. El equipo debe revisar la acción recomendada.",
        },
    }


EXPLANATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["executive_summary", "operational_explanation", "recommended_action", "uncertainty_note", "profiles"],
    "properties": {
        "executive_summary": {"type": "string"},
        "operational_explanation": {"type": "string"},
        "recommended_action": {"type": "string"},
        "uncertainty_note": {"type": "string"},
        "profiles": {
            "type": "object", "additionalProperties": False,
            "required": ["technical", "financial", "simple"],
            "properties": {"technical": {"type": "string"}, "financial": {"type": "string"}, "simple": {"type": "string"}},
        },
    },
}


def openai_explanation(diagnosis: dict[str, Any], model: str, recurrence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Use OpenAI only as a factual Spanish copywriter over the diagnosis JSON."""
    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError("Install the OpenAI Python SDK first: py -m pip install openai") from error
    instructions = """Eres redactor para operaciones de pagos. Redacta únicamente con los hechos
del JSON recibido. No inventes dimensiones, causas, cifras, fechas ni acciones. No cambies la
acción recomendada. Si evidence_sufficient es false, dilo claramente. Responde en español.
En profiles devuelve: technical para un operador, financial para negocio y simple sin jerga para cualquier persona.
Si el JSON incluye "recurrence", menciona brevemente en operational_explanation que este patrón ya
ocurrió antes (con la fecha y similitud dadas) — solo si ese campo está presente."""
    payload = dict(diagnosis)
    recurrence = recurrence if recurrence is not None else diagnosis.get("recurrence")
    if recurrence:
        payload["recurrence"] = recurrence
    client = OpenAI()
    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=json.dumps(payload, ensure_ascii=False),
        text={"format": {"type": "json_schema", "name": "incident_explanation", "strict": True, "schema": EXPLANATION_SCHEMA}},
    )
    explanation = json.loads(response.output_text)
    return {
        "mode": "openai",
        "incident_id": diagnosis.get("incident_id"),
        **explanation,
        "confidence": diagnosis.get("confidence"),
        "evidence_sufficient": diagnosis.get("evidence_sufficient"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create safe executive and operational incident explanations.")
    parser.add_argument("--diagnosis", required=True, help="Diagnosis JSON or JSONL produced by diagnoser.py.")
    parser.add_argument("--use-openai", action="store_true", help="Use OpenAI to rewrite the supplied diagnosis facts.")
    parser.add_argument("--model", default="gpt-5", help="OpenAI model used only with --use-openai.")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    diagnosis = load_diagnosis(arguments.diagnosis)
    result = openai_explanation(diagnosis, arguments.model) if arguments.use_openai else deterministic_explanation(diagnosis)
    print(json.dumps(result, ensure_ascii=False, indent=2))
