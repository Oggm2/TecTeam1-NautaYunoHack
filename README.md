# SentiPay — Control Tower

SentiPay is a demonstrable monitoring and diagnosis system for a payment-orchestration platform. It observes a simulated transaction stream, detects material conversion drops, localizes their impact, and presents evidence, estimated economic impact, and a recommendation for human review. A concentrated segment is not presented as a confirmed cause until an operator or external evidence validates it.

Created for the **Control Tower** NextWave Hackathon challenge. It never changes payment routing or executes remediation.

## What it does

- Distinguishes real conversion drops from normal statistical noise.
- Localizes impact by merchant, provider, payment method, country, issuing bank, and decline reason; distinguishes localized impact, likely source, and confirmed cause.
- Explains when an incident began, who it affects, how much it costs, and why the system believes it.
- Compares matched route cohorts and proposes only capped, human-approved routing experiments with fraud and cost guardrails.
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

Open the [SentiPay dashboard](http://localhost:8001/PagoTotal-Intelligence_1.html). The filename is retained for backward-compatible local URLs; the product is branded as SentiPay. Stop each process with `Ctrl+C`. On macOS/Linux, use `python3` instead of `py` when needed.

## Architecture

```text
Demo now
mock stream ─> detector ─> diagnosis ─> lifecycle/ledger ─> dashboard JSON
                    ↑              ↑        ↑           ↑
  historical JSONL ─┘       route comparison  operational events   incident memory
Trial by Fire / controls ───────────────> control server ─> dashboard

Production target
Kafka / PubSub / Kinesis ─> Flink / stream state ─> ClickHouse / BigQuery
Postgres (configuration, audit, incidents) ─> authenticated APIs ─> UI / Slack / Jira
OpenTelemetry + provider / 3DS / fraud / routing webhooks ─> evidence graph
```

| Component | File | Responsibility |
| --- | --- | --- |
| Generator | `src/generator.py` | Emits transactions, amounts, lifecycle events, retries, and controlled injections. |
| History | `src/generate_history.py` | Generates normal operation for the baseline. |
| Detector | `src/detector.py` | Detects persistent conversion deterioration. |
| Diagnoser | `src/diagnoser.py` | Performs drill-down and localizes the affected segment. |
| Counterfactual recommender | `src/counterfactual_routing.py` | Compares issuer-matched live route cohorts and proposes only a human-approved, capped experiment. |
| Cost | `src/cost_estimator.py` | Estimates attributable loss and expected recovery. |
| Priority | `src/prioritizer.py` | Ranks incidents using configurable strategy. |
| Identity + lifecycle | `src/incident_identity.py` + `src/incident_lifecycle.py` | Mints one immutable incident id; separates financial recovery from operational closure. |
| Financial ledger | `src/incident_loss_ledger.py` | Accumulates each incident exactly once using the immutable incident id. |
| Evidence graph | `src/evidence_graph.py` | Orders payment evidence, operational context, hypotheses, and confirmed cause. |
| Knowledge | `src/incident_memory.py` | Stores statistically recovered incidents for recurrence matching, preserving whether each was later operator-confirmed. |
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

A subsegment becomes part of the diagnostic path only when it remains anomalous, has sufficient volume, and explains at least 60% of its parent segment’s lost approvals. This proves **where impact is localized**, not necessarily why it happened. The Evidence Graph labels a provider, issuer, 3DS, fraud, routing, or campaign signal as a **likely source** until an operator or integrated external source confirms the cause.

Settings are saved in `data/runtime_config.json` and applied by the live runtime on its next refresh.

## Incidence cost

The calculation uses actual failed-payment amounts, not an average ticket:

```text
lost approvals = max(0, attempts × expected conversion − actual approvals)

expected unrecovered GMV =
Σ(amount × incident-attribution share × (1 − recovery probability))
```

Recovery probability is learned from historical successful retries by checkout, with fallbacks by payment method, decline reason, country, and hour.

The dashboard keeps a persistent per-incident ledger. Every 30 seconds it adds only newly completed, incident-attributable declines, so the same payment is never counted again when the rolling window moves:

```text
new estimated loss = declined amount × incident attribution × (1 − retry-success probability)
```

It shows three distinct metrics: **Accumulated Incident Loss** for active financial exposure, **Current Loss Rate** for the latest rolling-window hourly estimate, and **Total Incident Loss** for Today, This week, or This month. When conversion is healthy for the configured windows, the financial exposure ends and its loss is frozen. The incident leaves the active queue, appears under **Recovered** in incident history, and no longer contributes to the active-incident count. An operator can later document its cause, action, and validation to move it to **Resolved**. Recovered incidents retain their frozen cost, last observed loss rate, and time to recovery. This is still an estimate of unrecovered payment value, not final financial reconciliation or platform revenue.

### Incident identity and governed knowledge

At first sustained detection the lifecycle mints an immutable `incident_id` from tenant, first-detected timestamp, and canonical detection signature. That exact id is used by lifecycle, financial ledger, dashboard, chatbot, tickets, and incident memory; the changing set of correlated detector alerts is never used as a financial key.

The demo keeps three deliberately separate repositories:

- **Synthetic training incidents:** `examples/incident_training_scenarios.json` and injection fixtures; never used for recurrence recommendations.
- **Observed incidents:** `data/incident_lifecycle.json`; detected, investigating, monitoring, or statistically recovered cases.
- **Incident memory:** `data/incident_memory.json`; an incident is stored when statistical recovery is verified. Each record is labeled `statistically_recovered`, `operator_confirmed`, or `historical_resolved`, so recurrence context is never mistaken for a human-confirmed root cause. Legacy auto-seeded records remain quarantined and excluded from matching.

### Evidence graph context

`data/operational_events.json` can receive contextual events from future deploy, feature-flag, fraud, 3DS, routing, campaign, provider-status, latency, or ticket integrations. The demo API accepts the same contract at `POST /api/operational-events`; contextual events are shown chronologically and retain an `observed`, `hypothesis`, or `confirmed` verification level.

### Safe counterfactual routing recommendations

The product never says “route to another provider” from a diagnosis alone. For an incident already localized to a **merchant × country × payment-method × affected-provider** cohort, it compares the last 30 minutes of terminal transactions against alternative providers. The comparison is issuer-stratified (or exact issuer-matched if the incident is already bank-specific), weighted to the issuer mix of the affected route, and must satisfy the configured minimum sample, uplift, and z-score.

If the evidence passes, the recommendation is intentionally conditional:

> “Adyen approved 6.2 pp more comparable traffic. Recommend a 10% controlled routing experiment, only after fraud, cost, capacity, and compliance guardrails are approved.”

The comparison is observational, not causal proof. It cannot evaluate fraud, chargebacks, provider fees, FX, capacity, eligibility, or compliance from payment events alone. Those checks remain pending external integrations and human approval; the proposed test has explicit stop conditions. If no comparable cohort or material uplift exists, the system states that no routing experiment is recommended.

Policy is versionable in `data/routing_guardrails.json` and can be passed to either runtime with `--routing-policy`. It contains the comparison window, minimum sample, minimum uplift, confidence threshold, maximum traffic cap, mandatory guardrails, and stop conditions.

## Dashboard modes and Trial by Fire

The global audience selector adapts the complete dashboard:

- **Technical:** stream, statistical thresholds, evidence, and root-cause drill-down.
- **Financial:** economic exposure, cost per hour, and ranked work.
- **Simple:** a direct explanation of what happened, who is affected, and what to review.

**Trial by Fire** creates a new live incident without restarting the runtime. The judge selects merchant, provider, country, payment method, issuing bank, decline reason, approval rate, traffic share, and duration. The injection is stored in `data/live_injections.json`; the detector and diagnoser only receive the resulting events.

The UI immediately displays a clearly labeled **Manual Trial Alert** so the judge can see that the injection was accepted. Its cost is `$0` until completed declined payments are observed and the detector independently confirms the incident with sufficient statistical evidence. Trial injections can be removed from the same modal; removing one stops only its simulated traffic and does not delete incident history.

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
