"""Stable identities shared by lifecycle, financial exposure and knowledge.

An alert is a statistical reading; an incident is the operational object that
survives changing alert membership and a refining diagnostic drill-down.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any


DEFAULT_TENANT = "pagototal-demo"


def canonical_signature(value: dict[str, Any] | str | None) -> str:
    if isinstance(value, dict):
        return "|".join(f"{key}={value[key]}" for key in sorted(value))
    return str(value or "unknown")


def make_incident_id(
    first_detected_at: datetime | str, signature: dict[str, Any] | str | None,
    tenant_id: str = DEFAULT_TENANT,
) -> str:
    """Create a deterministic immutable id for one observed incident cycle."""
    if isinstance(first_detected_at, datetime):
        timestamp = first_detected_at.astimezone(UTC).isoformat()
    else:
        timestamp = str(first_detected_at)
    material = f"{tenant_id}|{timestamp}|{canonical_signature(signature)}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"inc_{digest}"
