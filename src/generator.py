"""Mock payment transaction stream for the Control Tower challenge."""

from __future__ import annotations

import argparse
import heapq
import json
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


MERCHANTS = ("PagoModa", "TravelNow", "TechStore")
PROVIDERS = ("stripe", "adyen", "dlocal")
COUNTRIES = ("MX", "CO", "BR")
CURRENCIES = {"MX": "MXN", "CO": "COP", "BR": "BRL"}
USD_PER_LOCAL_CURRENCY = {"MXN": 1 / 17.2, "COP": 1 / 3950, "BRL": 1 / 5.2}
PAYMENT_METHODS = ("card", "pse", "wallet", "pix", "cash_in_store")
BANKS_BY_COUNTRY = {
    "MX": ("BBVA", "Banorte", "Santander", "Citibanamex"),
    "CO": ("Bancolombia", "Davivienda", "Banco de Bogota", "Nequi"),
    "BR": ("Itau", "Bradesco", "Nubank", "Banco do Brasil"),
}
DECLINES = {
    "05": "do_not_honor", "14": "invalid_card", "41": "lost_card", "43": "stolen_card",
    "51": "insufficient_funds", "54": "expired_card", "57": "transaction_not_permitted",
    "91": "issuer_unavailable",
}
REASON_TO_CODE = {reason: code for code, reason in DECLINES.items()}
RECOVERABLE_FAILURE_STATUSES = {"declined", "failed", "expired"}
RETRY_RECOVERY_BY_METHOD = {
    "card": 0.48, "wallet": 0.55, "pix": 0.50, "pse": 0.36, "cash_in_store": 0.12,
}
RETRY_REASON_MULTIPLIER = {
    "issuer_unavailable": 1.35, "provider_error": 1.25, "do_not_honor": 0.90,
    "insufficient_funds": 0.55, "expired_card": 0.45, "invalid_card": 0.20,
    "lost_card": 0.08, "stolen_card": 0.08, "payment_expired": 0.40,
}


@dataclass(frozen=True)
class Injection:
    """A controlled reduction of approval probability for matching events."""

    name: str
    start_after_seconds: float
    approval_rate: float
    filters: dict[str, str]
    decline_reason: str = "do_not_honor"
    duration_seconds: float | None = None
    traffic_share: float = 0.0
    activated_at: datetime | None = None

    def active_at(self, timestamp: datetime, elapsed_seconds: float) -> bool:
        if self.activated_at is not None:
            if timestamp < self.activated_at:
                return False
            return self.duration_seconds is None or timestamp <= self.activated_at + timedelta(seconds=self.duration_seconds)
        if elapsed_seconds < self.start_after_seconds:
            return False
        return self.duration_seconds is None or elapsed_seconds <= self.start_after_seconds + self.duration_seconds

    def applies_to(self, event: dict[str, Any], timestamp: datetime, elapsed_seconds: float) -> bool:
        if not self.active_at(timestamp, elapsed_seconds):
            return False
        return all(event.get(field) == value for field, value in self.filters.items())


def iso_timestamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value is not None else None


def load_injections(path: str | None) -> list[Injection]:
    if not path:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("The injection file must contain a JSON array.")
    allowed_filters = {"merchant", "provider", "payment_method", "country", "issuing_bank"}
    injections: list[Injection] = []
    for index, item in enumerate(raw):
        filters = item.get("filters", {})
        unknown = set(filters) - allowed_filters
        if unknown:
            raise ValueError(f"Injection {index} has invalid filters: {sorted(unknown)}")
        approval_rate = float(item["approval_rate"])
        if not 0 <= approval_rate <= 1:
            raise ValueError("approval_rate must be between 0 and 1.")
        reason = item.get("decline_reason", item.get("decline_code", "do_not_honor"))
        traffic_share = float(item.get("traffic_share", 0))
        if not 0 <= traffic_share <= 1:
            raise ValueError("traffic_share must be between 0 and 1.")
        activated_at = item.get("activated_at")
        injections.append(Injection(
            name=item.get("name", f"injection-{index + 1}"),
            start_after_seconds=float(item.get("start_after_seconds", 0)),
            duration_seconds=float(item["duration_seconds"]) if item.get("duration_seconds") is not None else None,
            approval_rate=approval_rate, filters=filters, decline_reason=reason, traffic_share=traffic_share,
            activated_at=datetime.fromisoformat(str(activated_at).replace("Z", "+00:00")).astimezone(UTC) if activated_at else None,
        ))
    return injections


def weighted_choice(rng: random.Random, options: tuple[str, ...], weights: tuple[float, ...]) -> str:
    return rng.choices(options, weights=weights, k=1)[0]


def baseline_approval_rate(event: dict[str, Any], timestamp: datetime) -> float:
    rate = 0.93
    rate += {"card": 0.02, "pse": -0.015, "wallet": 0.025, "pix": 0.03, "cash_in_store": -0.03}[event["payment_method"]]
    rate += {"MX": 0.01, "CO": -0.01, "BR": 0.005}[event["country"]]
    if timestamp.hour in {2, 3, 4, 5}:
        rate -= 0.025
    return max(0.70, min(0.99, rate))


def amount_for(country: str, rng: random.Random) -> float:
    return round(rng.lognormvariate(mu=0, sigma=0.65) * {"MX": 650, "CO": 95_000, "BR": 180}[country], 2)


def make_candidate(timestamp: datetime, rng: random.Random) -> dict[str, Any]:
    country = weighted_choice(rng, COUNTRIES, (0.40, 0.25, 0.35))
    method_weights = {
        "MX": (0.70, 0.0, 0.18, 0.0, 0.12),
        "CO": (0.58, 0.22, 0.15, 0.0, 0.05),
        "BR": (0.47, 0.0, 0.18, 0.30, 0.05),
    }
    currency = CURRENCIES[country]
    amount = amount_for(country, rng)
    return {
        "transaction_id": str(uuid.uuid4()), "created_at": iso_timestamp(timestamp),
        "checkout_id": str(uuid.uuid4()), "customer_id": str(uuid.uuid4()),
        "attempt_number": 1, "is_retry": False, "original_failed_attempt_id": None,
        "merchant": weighted_choice(rng, MERCHANTS, (0.35, 0.25, 0.40)),
        "provider": weighted_choice(rng, PROVIDERS, (0.40, 0.35, 0.25)),
        "payment_method": weighted_choice(rng, PAYMENT_METHODS, method_weights[country]),
        "country": country, "issuing_bank": rng.choice(BANKS_BY_COUNTRY[country]),
        "amount": amount, "currency": currency,
        "amount_usd": round(amount * USD_PER_LOCAL_CURRENCY[currency], 2),
    }


def add_lifecycle(event: dict[str, Any], timestamp: datetime, rng: random.Random, status: str) -> None:
    request_at = timestamp + timedelta(milliseconds=rng.randint(25, 700))
    processing_time_ms = max(30, round(rng.lognormvariate(6.3, 0.55)))
    event["provider_request_at"] = iso_timestamp(request_at)
    event["processing_time_ms"] = processing_time_ms
    if status == "processing":
        event["provider_response_at"] = None
        event["completed_at"] = None
        return
    response_at = request_at + timedelta(milliseconds=processing_time_ms)
    completed_at = response_at + timedelta(milliseconds=rng.randint(5, 250))
    event["provider_response_at"] = iso_timestamp(response_at)
    event["completed_at"] = iso_timestamp(completed_at)


def set_decline(event: dict[str, Any], reason: str | None, rng: random.Random) -> None:
    if reason in REASON_TO_CODE:
        event["decline_code"] = REASON_TO_CODE[reason]
        event["decline_reason"] = reason
    else:
        code = rng.choice(tuple(DECLINES))
        event["decline_code"] = code
        event["decline_reason"] = DECLINES[code]


def force_candidate_dimensions(event: dict[str, Any], injection: Injection, rng: random.Random) -> None:
    """Give an active live trial enough representative traffic for statistical detection."""
    filters = injection.filters
    country = filters.get("country")
    if country:
        event["country"] = country
        event["currency"] = CURRENCIES[country]
        event["amount"] = amount_for(country, rng)
        event["amount_usd"] = round(event["amount"] * USD_PER_LOCAL_CURRENCY[event["currency"]], 2)
    for field, value in filters.items():
        event[field] = value


def create_event(timestamp: datetime, elapsed_seconds: float, rng: random.Random,
                 injections: list[Injection], include_processing: bool = True) -> dict[str, Any]:
    event = make_candidate(timestamp, rng)
    forced = [item for item in injections if item.active_at(timestamp, elapsed_seconds) and item.traffic_share and rng.random() < item.traffic_share]
    if forced:
        force_candidate_dimensions(event, min(forced, key=lambda item: item.approval_rate), rng)
    rate = baseline_approval_rate(event, timestamp)
    active = [item for item in injections if item.applies_to(event, timestamp, elapsed_seconds)]
    selected = min(active, key=lambda item: item.approval_rate) if active else None
    if selected:
        rate = selected.approval_rate
        event["simulation_injection"] = selected.name
    if include_processing and not selected and rng.random() < 0.01:
        status = "processing"
    elif rng.random() < rate:
        status = "approved"
    elif selected:
        status = "declined"
    else:
        status = weighted_choice(rng, ("declined", "failed", "cancelled", "expired"), (0.91, 0.04, 0.03, 0.02))
    event["status"] = status
    add_lifecycle(event, timestamp, rng, status)
    if status == "declined":
        set_decline(event, selected.decline_reason if selected else None, rng)
    elif status == "failed":
        event["decline_code"], event["decline_reason"] = "provider_error", "provider_error"
    elif status == "cancelled":
        event["decline_code"], event["decline_reason"] = "user_cancelled", "user_cancelled"
    elif status == "expired":
        event["decline_code"], event["decline_reason"] = "payment_expired", "payment_expired"
    else:
        event["decline_code"], event["decline_reason"] = None, None
    return event


def recovery_probability(event: dict[str, Any], timestamp: datetime) -> float:
    """Simulation-only chance that a failed checkout completes on its next attempt."""
    probability = RETRY_RECOVERY_BY_METHOD.get(str(event.get("payment_method")), 0.35)
    probability *= RETRY_REASON_MULTIPLIER.get(str(event.get("decline_reason")), 1.0)
    if timestamp.hour in {0, 1, 2, 3, 4, 5}:
        probability *= 0.78
    return min(0.92, max(0.02, probability))


def retry_events_for(event: dict[str, Any], rng: random.Random, delay_seconds: float) -> list[tuple[float, dict[str, Any]]]:
    """Create one linked retry for a subset of terminal failures.

    The generator uses a compressed retry delay in the live demo.  Historical
    generation can use a longer delay; either way the events retain a shared
    checkout ID so recovery is observable.
    """
    if event.get("status") not in RECOVERABLE_FAILURE_STATUSES or event.get("is_retry"):
        return []
    retry_probability = min(0.90, recovery_probability(event, parse_event_time(event)) + 0.18)
    if rng.random() >= retry_probability:
        return []
    initial_time = parse_event_time(event)
    retry_at = initial_time + timedelta(seconds=delay_seconds)
    retried = {
        key: event.get(key) for key in (
            "checkout_id", "customer_id", "merchant", "provider", "payment_method", "country",
            "issuing_bank", "amount", "currency", "amount_usd",
        )
    }
    retried.update({
        "transaction_id": str(uuid.uuid4()), "created_at": iso_timestamp(retry_at),
        "attempt_number": int(event.get("attempt_number", 1)) + 1, "is_retry": True,
        "original_failed_attempt_id": event.get("transaction_id"),
    })
    # Some customers choose a different, readily available digital method.
    if rng.random() < 0.28 and retried["payment_method"] in {"card", "cash_in_store", "pse"}:
        retried["payment_method"] = rng.choice(("card", "wallet", "pix" if retried["country"] == "BR" else "wallet"))
    success = rng.random() < recovery_probability(event, initial_time)
    retried["status"] = "approved" if success else "declined"
    add_lifecycle(retried, retry_at, rng, retried["status"])
    if success:
        retried["decline_code"], retried["decline_reason"] = None, None
    else:
        set_decline(retried, event.get("decline_reason"), rng)
    return [(delay_seconds, retried)]


def parse_event_time(event: dict[str, Any]) -> datetime:
    value = event.get("completed_at") or event.get("created_at")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def stream(events_per_second: float, total_events: int | None, seed: int | None, injections: list[Injection]) -> None:
    rng = random.Random(seed)
    interval, started_at, emitted, retry_sequence = 1 / events_per_second, time.monotonic(), 0, 0
    pending_retries: list[tuple[float, int, dict[str, Any]]] = []
    while total_events is None or emitted < total_events:
        target_time = started_at + emitted * interval
        next_retry_at = pending_retries[0][0] if pending_retries else float("inf")
        if (wait := min(target_time, next_retry_at) - time.monotonic()) > 0:
            time.sleep(wait)
        while pending_retries and pending_retries[0][0] <= time.monotonic():
            _, _, retry = heapq.heappop(pending_retries)
            print(json.dumps(retry, separators=(",", ":")), flush=True)
        if time.monotonic() < target_time:
            continue
        now = datetime.now(UTC)
        event = create_event(now, time.monotonic() - started_at, rng, injections)
        print(json.dumps(event, separators=(",", ":")), flush=True)
        for delay, retry in retry_events_for(event, rng, delay_seconds=rng.uniform(3, 20)):
            retry_sequence += 1
            heapq.heappush(pending_retries, (time.monotonic() + delay, retry_sequence, retry))
        emitted += 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit mocked payment transactions as JSON Lines.")
    parser.add_argument("--events-per-second", type=float, default=1.0)
    parser.add_argument("--count", type=int, help="Stop after this many events (default: run indefinitely).")
    parser.add_argument("--seed", type=int, help="Optional seed for reproducible tests.")
    parser.add_argument("--injections", help="Path to an anomaly configuration JSON file.")
    args = parser.parse_args()
    if args.events_per_second <= 0:
        parser.error("--events-per-second must be positive.")
    if args.count is not None and args.count < 0:
        parser.error("--count cannot be negative.")
    return args


if __name__ == "__main__":
    arguments = parse_args()
    try:
        stream(arguments.events_per_second, arguments.count, arguments.seed, load_injections(arguments.injections))
    except BrokenPipeError:
        sys.exit(0)
