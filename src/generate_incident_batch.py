"""Generate a batch of transactions spanning several hours with multiple
distinct incidents injected at different points in time.

Unlike running live_dashboard.py for real, this writes events at simulated
timestamps without sleeping — a whole multi-hour analysis window is ready in
seconds. Feed the output to build_dashboard.py (as --transactions) to detect
and diagnose every injected incident in one pass and seed
data/incident_memory.json with varied, real incidents instead of running the
live pipeline once per scenario.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from generator import create_event, load_injections


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch-generate a multi-incident transaction window for offline analysis.")
    parser.add_argument("--scenarios", required=True, help="Injection scenarios JSON (same format as examples/injections.json).")
    parser.add_argument("--hours", type=float, default=1.75, help="Total simulated duration, ending now.")
    parser.add_argument("--events-per-second", type=float, default=15.0)
    parser.add_argument("--seed", type=int, default=99)
    parser.add_argument("--output", default="data/incident_training_batch.jsonl")
    args = parser.parse_args()
    if args.hours <= 0 or args.events_per_second <= 0:
        parser.error("--hours and --events-per-second must be positive.")
    return args


def main() -> None:
    args = parse_args()
    injections = load_injections(args.scenarios)
    rng = random.Random(args.seed)
    total_seconds = args.hours * 3600
    interval = 1 / args.events_per_second
    end_at = datetime.now(UTC)
    start_at = end_at - timedelta(seconds=total_seconds)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output_path.open("w", encoding="utf-8") as sink:
        elapsed = 0.0
        while elapsed <= total_seconds:
            timestamp = start_at + timedelta(seconds=elapsed)
            event = create_event(timestamp, elapsed, rng, injections, include_processing=False)
            sink.write(json.dumps(event, separators=(",", ":")) + "\n")
            written += 1
            elapsed += interval
    print(f"wrote {written} transactions spanning {args.hours}h ending {end_at.isoformat()} to {output_path}")


if __name__ == "__main__":
    main()
