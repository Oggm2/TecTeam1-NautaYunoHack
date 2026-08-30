# PagoTotal Intelligence — Control Tower

PagoTotal Intelligence is a demonstrable monitoring and diagnosis system for a payment-orchestration platform. It observes a simulated transaction stream, detects material conversion drops, isolates their root cause, and presents evidence, estimated economic impact, and a recommendation for human review.

Created for the **Control Tower** NextWave Hackathon challenge. It never changes payment routing or executes remediation.

## What it does

- Distinguishes real conversion drops from normal statistical noise.
- Diagnoses by merchant, provider, payment method, country, issuing bank, and decline reason.
- Explains when an incident began, who it affects, how much it costs, and why the system believes it.
- Separates and prioritizes simultaneous incidents.
- Recognizes recurring patterns through incident memory.
- Supports a live, judge-defined **Trial by Fire** injection.

## Quick start

Requirements: Python **3.11+**. The core pipeline uses only the Python standard library.

Open two terminals at the repository root.

**Terminal 1 — simulation, detection, diagnosis, and dashboard data**

```powershell
# Run only if data/history.jsonl does not exist locally.
py src/generate_history.py --days 28 --events-per-minute 5

py src/live_dashboard.py --history data/history.jsonl --injections examples/injections.json --events-per-second 15 --refresh-seconds 10
```

**Terminal 2 — dashboard server, settings, and Trial by Fire**

```powershell
py src/control_server.py --port 8001
```

Open [http://localhost:8001/PagoTotal-Intelligence_1.html](http://localhost:8001/PagoTotal-Intelligence_1.html). Stop each process with `Ctrl+C`. On macOS/Linux, use `python3` instead of `py` when needed.

## Architecture

```text
historical JSONL ──> baseline + recovery model ─────────────────────┐
                                                                    │
mock stream ─> detector ─> diagnoser ─> prioritizer ─> dashboard JSON
                                                                    │
Trial by Fire / settings ─> control server ─────────────────────────┘
                                                                    │
browser dashboard <─────────────────────────────────────────────────┘
```

| Component | File | Responsibility |
| --- | --- | --- |
| Generator | `src/generator.py` | Emits transactions, amounts, lifecycle events, retries, and controlled injections. |
| History | `src/generate_history.py` | Generates normal operation for the baseline. |
| Detector | `src/detector.py` | Detects persistent conversion deterioration. |
| Diagnoser | `src/diagnoser.py` | Performs drill-down and isolates the responsible segment. |
| Cost | `src/cost_estimator.py` | Estimates attributable loss and expected recovery. |
| Priority | `src/prioritizer.py` | Ranks incidents using configurable strategy. |
| Memory | `src/incident_memory.py` | Finds similar resolved incidents with cosine similarity. |
| Runtime | `src/live_dashboard.py` | Connects the pipeline and writes `frontend/dashboard_data.json`. |
| API/UI | `src/control_server.py` + `frontend/` | Serves the dashboard, settings, chatbot, and Trial by Fire. |

## Detection and diagnosis

The detector compares a rolling live window with historical expected conversion. By default it requires:

| Rule | Default |
| --- | ---: |
| Window | 300 s |
| Evaluation cadence | 30 s |
| Persistence | 3 evaluations |
| Minimum current volume | 30 attempts |
| Minimum drop | 5 pp |
| Statistical threshold | Z = 3.0 |

A subsegment becomes part of the root-cause path only when it remains anomalous, has sufficient volume, and explains at least 60% of its parent segment’s lost approvals. If evidence is insufficient, the system reports uncertainty rather than inventing a cause.

Settings are saved in `data/runtime_config.json` and applied by the live runtime on its next refresh.

## Incidence cost

The calculation uses actual failed-payment amounts, not an average ticket:

```text
lost approvals = max(0, attempts × expected conversion − actual approvals)

expected unrecovered GMV =
Σ(amount × incident-attribution share × (1 − recovery probability))
```

Recovery probability is learned from historical successful retries by checkout, with fallbacks by payment method, decline reason, country, and hour.

The dashboard displays an estimate for the **current diagnosis window**:

```text
window cost = estimated cost per hour × window duration / 60
```

For example, USD 3,600/hour over a five-minute window is USD 300. This is not a running total and not final financial reconciliation.

## Dashboard modes and Trial by Fire

The global audience selector adapts the complete dashboard:

- **Technical:** stream, statistical thresholds, evidence, and root-cause drill-down.
- **Financial:** economic exposure, cost per hour, and ranked work.
- **Simple:** a direct explanation of what happened, who is affected, and what to review.

**Trial by Fire** creates a new live incident without restarting the runtime. The judge selects merchant, provider, country, payment method, issuing bank, decline reason, approval rate, traffic share, and duration. The injection is stored in `data/live_injections.json`; the detector and diagnoser only receive the resulting events.

For a clean trial without preconfigured incidents:

```powershell
Set-Content -Path data/empty_injections.json -Value "[]"
py src/live_dashboard.py --history data/history.jsonl --injections data/empty_injections.json --events-per-second 15 --refresh-seconds 10
```

## OpenAI and chatbot (optional)

The project works without OpenAI. OpenAI is used only to write a diagnosis already computed from facts and to answer chatbot questions using local aggregates. It does not decide alerts, costs, or root causes.

```powershell
py -m pip install -r requirements.txt
$env:OPENAI_API_KEY = "your_key"
py src/control_server.py --port 8001 --chat-model gpt-5
```

The server reads `OPENAI_API_KEY` from its environment; it does **not** automatically load a `.env` file. Never commit an API key or expose it in the frontend.

## Offline execution and tests

```powershell
py src/build_dashboard.py --history data/history.jsonl --transactions data/live_transactions.jsonl
py -m unittest discover -s tests -v
```

Historical JSONL files can be large and should be generated locally. This is a payment simulation: all impact figures are operational estimates, and every recommendation requires human approval.
