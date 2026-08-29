"""Separate simultaneous incidents by dimension-signature overlap and rank them.

Two diagnoses are the same incident only if every dimension they share holds
the same value — that is the operational definition of "affected segments
overlap" from the brief. Diagnoses that share no dimension, or disagree on a
shared one, are kept as distinct incidents even if they were active at the
same time.

Urgency prefers a measured growth rate: USD/minute of estimated loss between
the earliest and latest diagnosis seen for an incident. With only a single
snapshot (the common case — most incidents get diagnosed once) there is no
growth to measure, so urgency falls back to |z-score|, the best available
proxy for how far and how fast the incident has already moved past baseline.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def segments_overlap(a: dict[str, str], b: dict[str, str]) -> bool:
    shared = set(a) & set(b)
    return bool(shared) and all(a[key] == b[key] for key in shared)


def group_incidents(diagnoses: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Union-find diagnoses whose root-cause segments overlap into one incident."""
    parents = list(range(len(diagnoses)))

    def find(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parents[ri] = rj

    for i in range(len(diagnoses)):
        for j in range(i + 1, len(diagnoses)):
            if segments_overlap(diagnoses[i].get("root_cause_segment", {}), diagnoses[j].get("root_cause_segment", {})):
                union(i, j)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, diagnosis in enumerate(diagnoses):
        groups.setdefault(find(index), []).append(diagnosis)
    return list(groups.values())


def segment_key(segment: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(segment.items()))


def representative(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the canonical diagnosis for a merged incident.

    Independent alerts (different seed dimensions) often converge on the same
    root-cause segment, but each drill-down is a fresh, small-sample walk and
    can pick up one incidental extra dimension by chance. A majority vote on
    the exact segment cancels that noise; fewest dimensions and confidence
    break remaining ties in favor of the more general, better-evidenced read.
    """
    sufficient = [d for d in group if d.get("evidence_sufficient")]
    pool = sufficient or group
    votes = Counter(segment_key(d.get("root_cause_segment", {})) for d in pool)

    def score(d: dict[str, Any]) -> tuple[int, int, int, datetime]:
        key = segment_key(d.get("root_cause_segment", {}))
        return (
            votes[key],
            -len(key),
            CONFIDENCE_RANK.get(d.get("confidence", "low"), 0),
            parse_timestamp(d.get("diagnosed_at")) or datetime.min.replace(tzinfo=UTC),
        )

    return max(pool, key=score)


def urgency_score(readings: list[dict[str, Any]]) -> tuple[float, str]:
    timed = sorted(
        ((parse_timestamp(r.get("diagnosed_at")), r.get("root_metrics", {}).get("expected_unrecovered_amount_usd", 0.0)) for r in readings),
        key=lambda pair: pair[0] or datetime.min.replace(tzinfo=UTC),
    )
    if len(timed) >= 2 and timed[0][0] and timed[-1][0] and timed[-1][0] > timed[0][0]:
        elapsed_minutes = (timed[-1][0] - timed[0][0]).total_seconds() / 60
        growth = (timed[-1][1] - timed[0][1]) / elapsed_minutes
        return max(growth, 0.0), "growth_rate_usd_per_min"
    latest = readings[-1]
    return abs(latest.get("root_metrics", {}).get("z_score", 0.0)), "z_score_proxy"


@dataclass
class PrioritizedIncident:
    incident_key: str
    latest_diagnosis: dict[str, Any]
    readings: int
    cost_per_hour_usd: float
    urgency: float
    urgency_basis: str
    priority_score: float
    priority_rank: int = 0


def prioritize(diagnoses: list[dict[str, Any]]) -> list[PrioritizedIncident]:
    """Rank incidents by expected unrecovered GMV per hour x urgency.

    Urgency caps its contribution at +100% of cost so a single incident with
    an extreme z-score can't outrank one costing an order of magnitude more —
    cost stays the primary signal, urgency just breaks ties and surfaces
    incidents that are actively worsening.
    """
    incidents: list[PrioritizedIncident] = []
    for group in group_incidents(diagnoses):
        latest = representative(group)
        metrics = latest.get("root_metrics", {})
        cost_per_hour = metrics.get("expected_unrecovered_amount_per_hour_usd") or metrics.get("gross_lost_amount_per_hour_usd") or 0.0
        urgency, basis = urgency_score(group)
        normalizer = 500.0 if basis == "growth_rate_usd_per_min" else 8.0
        urgency_multiplier = 1 + min(urgency / normalizer, 1.0)
        signature = " ∧ ".join(f"{k}={v}" for k, v in sorted(latest.get("root_cause_segment", {}).items()))
        incidents.append(PrioritizedIncident(
            incident_key=signature or latest.get("incident_id", "unknown"),
            latest_diagnosis=latest,
            readings=len(group),
            cost_per_hour_usd=round(cost_per_hour, 2),
            urgency=round(urgency, 4),
            urgency_basis=basis,
            priority_score=round(cost_per_hour * urgency_multiplier, 2),
        ))
    incidents.sort(key=lambda incident: incident.priority_score, reverse=True)
    for rank, incident in enumerate(incidents, start=1):
        incident.priority_rank = rank
    return incidents


def load_jsonl(path: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                records.append(json.loads(line))
    return records


def to_json(incidents: list[PrioritizedIncident]) -> list[dict[str, Any]]:
    return [
        {
            "incident_key": incident.incident_key,
            "priority_rank": incident.priority_rank,
            "priority_score": incident.priority_score,
            "cost_per_hour_usd": incident.cost_per_hour_usd,
            "urgency": incident.urgency,
            "urgency_basis": incident.urgency_basis,
            "readings": incident.readings,
            "diagnosis": incident.latest_diagnosis,
        }
        for incident in incidents
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Separate and rank simultaneous incidents from diagnoser.py output.")
    parser.add_argument("--diagnoses", required=True, help="JSONL file with one diagnosis object per line.")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    print(json.dumps(to_json(prioritize(load_jsonl(arguments.diagnoses))), indent=2))
