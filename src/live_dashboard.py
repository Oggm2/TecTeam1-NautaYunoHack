"""Run the full pipeline continuously against a real-time generated stream.

Unlike build_dashboard.py (which replays a transaction file captured earlier
and writes the dashboard JSON once), this drives generator.create_event at
real wall-clock pace, feeds it straight into the detector, diagnoses new
alerts as they fire, and rewrites frontend/dashboard_data.json every
--refresh-seconds. The frontend polls that file, so the dashboard reflects
incidents as the detector actually finds them instead of one static snapshot.

Ctrl+C to stop. Captured transactions are also appended to
data/live_transactions.jsonl so build_dashboard.py can replay the same run
later if needed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import build_dashboard as bd
import detector
import explainer
import generator
import incident_memory
import prioritizer
from detector import parse_timestamp
from recovery_estimator import RecoveryEstimator

RUNTIME_FIELDS = ("window_seconds", "evaluation_seconds", "persistence", "min_attempts", "min_history_attempts", "min_drop_pp", "z_threshold")


def write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a live transaction stream and keep the dashboard JSON continuously up to date.")
    parser.add_argument("--history", default="data/history.jsonl")
    parser.add_argument("--injections", default="examples/injections.json")
    parser.add_argument("--live-injections", default="data/live_injections.json", help="Runtime judge-controlled injections read without restarting the stream.")
    parser.add_argument("--events-per-second", type=float, default=15.0)
    parser.add_argument("--refresh-seconds", type=float, default=10.0, help="How often to re-diagnose and rewrite the dashboard JSON.")
    parser.add_argument("--retain-seconds", type=int, default=1800, help="How much transaction history to keep in memory for chart/diagnosis context.")
    parser.add_argument("--window-seconds", type=int, default=300)
    parser.add_argument("--evaluation-seconds", type=int, default=30)
    parser.add_argument("--persistence", type=int, default=3)
    parser.add_argument("--min-attempts", type=int, default=30)
    parser.add_argument("--min-history-attempts", type=int, default=200)
    parser.add_argument("--min-drop-pp", type=float, default=5.0)
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--diag-min-attempts", type=int, default=20)
    parser.add_argument("--min-contribution", type=float, default=0.60)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--recovery-horizon-hours", type=float, default=24.0)
    parser.add_argument("--chart-minutes", type=int, default=30)
    parser.add_argument("--memory", default="data/incident_memory.json")
    parser.add_argument("--output", default="frontend/dashboard_data.json")
    parser.add_argument("--transactions-out", default="data/live_transactions.jsonl")
    parser.add_argument("--cost-tracker", default="data/active_incident_costs.json", help="Persist active incident cost accumulation across process restarts.")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--use-openai", action="store_true", help="Use OpenAI only to redact confirmed diagnoses.")
    parser.add_argument("--model", default="gpt-5", help="OpenAI model used with --use-openai.")
    parser.add_argument("--runtime-config", default="data/runtime_config.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    history_events = bd.load_jsonl(args.history)
    detector_store = bd.build_store(history_events, detector.SEGMENT_DEFINITIONS)
    recovery_estimator = RecoveryEstimator(horizon_hours=args.recovery_horizon_hours).fit(history_events)
    detector_args = argparse.Namespace(
        window_seconds=args.window_seconds, evaluation_seconds=args.evaluation_seconds,
        persistence=args.persistence, min_attempts=args.min_attempts,
        min_history_attempts=args.min_history_attempts, min_drop_pp=args.min_drop_pp,
        z_threshold=args.z_threshold,
    )
    engine = detector.DetectionEngine(detector_store, detector_args, recovery_estimator)
    injections = generator.load_injections(args.injections)
    live_injections_path = Path(args.live_injections)
    live_injections_mtime: int | None = None
    live_injections: list[generator.Injection] = []
    memory = incident_memory.load(args.memory)
    priority_config = prioritizer.normalize_priority_config()

    def reload_controls() -> bool:
        nonlocal priority_config
        try:
            controls = json.loads(Path(args.runtime_config).read_text(encoding="utf-8"))
            if any(controls[key] <= 0 for key in RUNTIME_FIELDS):
                return False
            changed = any(getattr(detector_args, key) != controls[key] for key in RUNTIME_FIELDS)
            for key in RUNTIME_FIELDS:
                setattr(detector_args, key, controls[key])
            priority_config = prioritizer.normalize_priority_config(controls)
            return changed
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return False

    def reload_live_injections() -> None:
        nonlocal live_injections_mtime, live_injections
        mtime = live_injections_path.stat().st_mtime_ns if live_injections_path.exists() else None
        if mtime == live_injections_mtime:
            return
        try:
            live_injections = generator.load_injections(str(live_injections_path)) if mtime is not None else []
            live_injections_mtime = mtime
            print(f"loaded {len(live_injections)} live trial injection(s)", file=sys.stderr)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"could not load live injections: {error}", file=sys.stderr)

    rng = random.Random(args.seed)
    interval = 1 / args.events_per_second
    wall_start = datetime.now(UTC)
    loop_start = time.monotonic()

    retained: list[dict[str, Any]] = []
    active_alerts: dict[str, dict[str, Any]] = {}
    tracker_path = Path(args.cost_tracker)
    try:
        loaded_tracker = json.loads(tracker_path.read_text(encoding="utf-8"))
        loss_tracker: dict[str, dict[str, Any]] = loaded_tracker if isinstance(loaded_tracker, dict) else {}
    except (OSError, json.JSONDecodeError):
        loss_tracker = {}
    seen_alert_ids: set[str] = set()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    transactions_path = Path(args.transactions_out)
    transactions_path.parent.mkdir(parents=True, exist_ok=True)
    transactions_sink = transactions_path.open("w", encoding="utf-8")

    emitted = 0
    last_refresh = time.monotonic() - args.refresh_seconds
    last_evaluation = time.monotonic() - detector_args.evaluation_seconds
    active_diagnoses: list[dict[str, Any]] = []
    print(f"live dashboard running at {args.events_per_second}/s, refreshing every {args.refresh_seconds}s — Ctrl+C to stop", file=sys.stderr)
    try:
        while True:
            target_time = loop_start + emitted * interval
            wait = target_time - time.monotonic()
            if wait > 0:
                time.sleep(wait)

            now = datetime.now(UTC)
            elapsed = (now - wall_start).total_seconds()
            reload_live_injections()
            event = generator.create_event(now, elapsed, rng, [*injections, *live_injections], include_processing=False)
            engine.add(event)
            retained.append(event)
            transactions_sink.write(json.dumps(event, separators=(",", ":")) + "\n")
            emitted += 1

            cutoff = now - timedelta(seconds=args.retain_seconds)
            while retained and (retained_ts := parse_timestamp(retained[0].get("completed_at"))) and retained_ts < cutoff:
                retained.pop(0)

            if time.monotonic() - last_refresh < args.refresh_seconds:
                continue
            last_refresh = time.monotonic()
            transactions_sink.flush()

            if reload_controls():
                # Rebuild so persistence deque sizes reflect the new setting.
                engine = detector.DetectionEngine(detector_store, detector_args, recovery_estimator)
                for retained_event in retained:
                    engine.add(retained_event)
                last_evaluation = time.monotonic() - detector_args.evaluation_seconds
                print(f"[{now.isoformat()}] applied runtime detection controls", file=sys.stderr)

            if time.monotonic() - last_evaluation >= detector_args.evaluation_seconds:
                last_evaluation = time.monotonic()
                alerts = engine.evaluate(now)
                new_alerts = [alert for alert in alerts if alert["alert_id"] not in seen_alert_ids]
                for alert in new_alerts:
                    seen_alert_ids.add(alert["alert_id"])
                    active_alerts[alert["alert_signature"]] = alert

                # Replace each alert's evidence with the newest rolling window.
                active_alerts = {
                    signature: {**alert, "evidence": engine.active_evidence[signature]}
                    for signature, alert in active_alerts.items()
                    if signature in engine.active_evidence
                }
                active_diagnoses = bd.diagnose_alerts(
                    list(active_alerts.values()), retained, history_events, args, memory,
                ) if active_alerts else []
                if args.use_openai:
                    for diagnosis in active_diagnoses:
                        diagnosis["explanation"] = explainer.openai_explanation(diagnosis, args.model)
                if new_alerts:
                    new_alert_ids = {alert["alert_id"] for alert in new_alerts}
                    for diagnosis in active_diagnoses:
                        if diagnosis.get("alert_id") in new_alert_ids:
                            print(f"[{now.isoformat()}] new incident: {diagnosis.get('root_cause_segment')}", file=sys.stderr)

            prioritized = prioritizer.prioritize(active_diagnoses, priority_config) if active_diagnoses else []
            entries = bd.build_incident_entries(prioritized, memory)
            bd.update_accumulated_unrecovered_gmv(entries, loss_tracker, now)
            tracker_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(tracker_path, json.dumps(loss_tracker, indent=2))
            chart = bd.build_chart(retained, list(active_alerts.values()), history_events, args)
            # Keep the dashboard's timeline tied to this process, even during
            # the first minute when there is only one aggregate data point.
            chart["stream_started_at"] = wall_start.isoformat()
            kpis = bd.build_kpis(
                entries, chart, transactions_window=len(retained), recovery_horizon_hours=args.recovery_horizon_hours,
            )
            dashboard = {
                "generated_at": now.isoformat(), "kpis": kpis, "chart": chart,
                "incidents": entries, "resolved": memory,
                "analytics": {"historical": bd.build_dataset_stats(history_events), "live": bd.build_dataset_stats(retained)},
            }
            write_atomic(output_path, json.dumps(dashboard, indent=2))
            print(f"[{now.isoformat()}] refreshed dashboard — {len(retained)} tx retained, {len(entries)} incident(s)", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        transactions_sink.close()


if __name__ == "__main__":
    main()
