"""Mock payment transaction stream for the Control Tower challenge."""

from __future__ import annotations

import argparse
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


@dataclass(frozen=True)
class Injection:
    """A controlled reduction of approval probability for matching events."""

    name: str
    start_after_seconds: float
    approval_rate: float
    filters: dict[str, str]
    decline_reason: str = "do_not_honor"
    duration_seconds: float | None = None

    def applies_to(self, event: dict[str, Any], elapsed_seconds: float) -> bool:
        if elapsed_seconds < self.start_after_seconds:
            return False
        if self.duration_seconds is not None and elapsed_seconds > self.start_after_seconds + self.duration_seconds:
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
        injections.append(Injection(
            name=item.get("name", f"injection-{index + 1}"),
            start_after_seconds=float(item.get("start_after_seconds", 0)),
            duration_seconds=float(item["duration_seconds"]) if item.get("duration_seconds") is not None else None,
            approval_rate=approval_rate, filters=filters, decline_reason=reason,
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


def create_event(timestamp: datetime, elapsed_seconds: float, rng: random.Random,
                 injections: list[Injection], include_processing: bool = True) -> dict[str, Any]:
    event = make_candidate(timestamp, rng)
    rate = baseline_approval_rate(event, timestamp)
    active = [item for item in injections if item.applies_to(event, elapsed_seconds)]
    selected = min(active, key=lambda item: item.approval_rate) if active else None
    if selected:
        rate = selected.approval_rate
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


def stream(events_per_second: float, total_events: int | None, seed: int | None, injections: list[Injection]) -> None:
    rng = random.Random(seed)
    interval, started_at, emitted = 1 / events_per_second, time.monotonic(), 0
    while total_events is None or emitted < total_events:
        target_time = started_at + emitted * interval
        if (wait := target_time - time.monotonic()) > 0:
            time.sleep(wait)
        now = datetime.now(UTC)
        print(json.dumps(create_event(now, time.monotonic() - started_at, rng, injections), separators=(",", ":")), flush=True)
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
