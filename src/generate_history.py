"""Create normal historical payment data used to build the conversion baseline."""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from generator import create_event, retry_events_for


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate normal historical payments in JSON Lines format.")
    parser.add_argument("--days", type=int, default=28, help="Historical days to generate (default: 28).")
    parser.add_argument("--events-per-minute", type=int, default=5, help="Average events per minute (default: 5).")
    parser.add_argument("--end-at", type=parse_datetime, help="UTC ISO timestamp; defaults to now.")
    parser.add_argument("--output", default="data/history.jsonl", help="Output JSONL path.")
    parser.add_argument("--seed", type=int, default=2026, help="Seed for reproducible output.")
    args = parser.parse_args()
    if args.days <= 0 or args.events_per_minute <= 0:
        parser.error("--days and --events-per-minute must be positive.")
    return args


def generate_history(days: int, events_per_minute: int, end_at: datetime, output: Path, seed: int) -> int:
    rng = random.Random(seed)
    output.parent.mkdir(parents=True, exist_ok=True)
    start_at = end_at - timedelta(days=days)
    total = 0
    with output.open("w", encoding="utf-8") as destination:
        current = start_at.replace(second=0, microsecond=0)
        while current < end_at:
            lower = max(1, round(events_per_minute * 0.7))
            upper = max(lower, round(events_per_minute * 1.3))
            timestamps = sorted(current + timedelta(seconds=rng.random() * 60) for _ in range(rng.randint(lower, upper)))
            for timestamp in timestamps:
                if timestamp >= end_at:
                    continue
                event = create_event(timestamp, 0, rng, [], include_processing=False)
                destination.write(json.dumps(event, separators=(",", ":")) + "\n")
                total += 1
                # Retry records may appear later in the file, but carry their
                # own timestamps and are sorted by consumers when order matters.
                for _, retry in retry_events_for(event, rng, delay_seconds=rng.uniform(60, 6 * 3600)):
                    destination.write(json.dumps(retry, separators=(",", ":")) + "\n")
                    total += 1
            current += timedelta(minutes=1)
    return total


if __name__ == "__main__":
    arguments = parse_args()
    ending = arguments.end_at or datetime.now(UTC)
    count = generate_history(arguments.days, arguments.events_per_minute, ending, Path(arguments.output), arguments.seed)
    print(f"Generated {count} normal historical transactions in {arguments.output}")
