# Data Dictionary — PagoTotal Intelligence

This document describes the data structures produced and consumed by the pipeline:

```text
transaction → alert → diagnosis → prioritized incident → dashboard entry
```

All timestamps are ISO 8601 in UTC. USD fields are floats; `*_per_hour_usd` fields annualize the amount observed in the analyzed window.

## 1. Transaction

Produced by `generator.py::create_event()`. One JSONL line represents one payment attempt.

| Field | Description |
| --- | --- |
| `transaction_id` | Unique payment-attempt UUID. |
| `checkout_id` | Identifier shared by retries for the same checkout. |
| `customer_id` | Synthetic buyer identifier; no real PII is used. |
| `attempt_number` | Retry sequence number for the checkout. |
| `original_failed_attempt_id` | Previous failed attempt followed by this retry, or null. |
| `created_at`, `provider_request_at`, `provider_response_at`, `completed_at` | Payment lifecycle timestamps. |
| `merchant`, `provider`, `country`, `payment_method`, `issuing_bank` | Dimensions used by detection and diagnosis. |
| `amount`, `currency`, `amount_usd` | Transaction value in local and comparable USD currency. |
| `status` | `approved`, `declined`, `processing`, `failed`, `cancelled`, or `expired`. |
| `decline_code` | Provider/bank decline code; null for approved payments. |
| `processing_time_ms` | End-to-end processing duration in milliseconds. |

Terminal statuses used for conversion are `approved`, `declined`, `failed`, and `expired`.

## 2. Alert

Produced by `detector.py::DetectionEngine.evaluate()` when a monitored segment has a sustained statistically significant conversion drop.

| Field | Description |
| --- | --- |
| `alert_id` / `alert_signature` | Unique alert and stable segment signature. |
| `detection_dimensions` | The two dimensions that triggered detection. |
| `evidence.segment` | Segment values that triggered the alert. |
| `window_started_at` / `window_ended_at` | Evaluated rolling window. |
| `observed_conversion` / `expected_conversion` | Actual versus historical expected conversion. |
| `conversion_drop_pp` / `z_score` | Conversion deterioration and statistical significance. |
| `baseline_attempts` / `baseline_source` | Historical sample size and fallback level. |
| `estimated_lost_approvals` | Estimated excess lost approvals. |
| `gross_lost_amount_usd` | Attributed gross lost GMV. |
| `expected_unrecovered_amount_usd` | Attributed GMV unlikely to recover through retry. |

## 3. Diagnosis

Produced by `diagnoser.py::diagnose()`. It starts from alert evidence and performs hierarchical drill-down.

| Field | Description |
| --- | --- |
| `incident_id`, `alert_id`, `diagnosed_at` | Incident identity and timestamp. |
| `evidence_sufficient` | False when the system cannot isolate a defensible cause. |
| `reason` | Explanation of insufficient evidence or non-concentrated loss. |
| `root_cause_segment` | Final dimensions isolated by drill-down. |
| `incident_window` | Window used to calculate incident metrics. |
| `root_metrics` | Conversion, attempts, loss, recovery, and cost for the root segment. |
| `drill_down_path` | Ordered selected dimensions and their contribution to parent loss. |
| `payment_method_impact` | Informational payment-method breakdown. |
| `dominant_decline` | Decline reason with the largest excess relative to history. |
| `recommended_action` | Human-review recommendation; it is never executed. |
| `recurrence` | Similar historical incident, if memory finds one. |
| `explanation` | Deterministic or OpenAI-written English explanation from computed facts. |

A drill-down row includes `value`, `dimension`, `contribution_to_parent_loss`, `is_anomalous`, and `siblings`. The default minimum contribution is 60%.

## 4. Prioritized incident and dashboard entry

`prioritizer.py` groups diagnosis readings into one operational incident and ranks them.

| Field | Description |
| --- | --- |
| `incident_key` | Stable identity derived from alert IDs. |
| `priority_rank` / `priority_score` | Position and configurable priority score. |
| `cost_per_hour_usd` | Expected unrecovered GMV per hour. |
| `window_expected_unrecovered_gmv_usd` | Estimated incidence cost in the current diagnosis window. |
| `gross_lost_amount_per_hour_usd` | Gross loss before expected retry recovery. |
| `urgency` / `urgency_basis` | Growth rate or statistical proxy used for urgency. |
| `confidence_pct` | Display confidence derived from diagnosis evidence. |
| `severity` / `status` | Dashboard classification and lifecycle state. |
| `priority_factors` | Active preset, weight distribution, and merchant multiplier. |

`frontend/dashboard_data.json` contains `kpis`, `chart`, `incidents`, `resolved`, and `analytics`. Analytics includes historical and live conversion, average ticket, and average processing time. Live processing time is measured in closed three-minute buckets.

## 5. Incident memory

`data/incident_memory.json` stores prior diagnosed incidents.

| Field | Description |
| --- | --- |
| `root_cause_segment` | Stored dimension signature. |
| `observed_at` / `resolved_at` | Observation or human-confirmed resolution time. |
| `duration_minutes` / `cost_per_hour_usd` | Historical incident impact. |
| `resolution_note` | Human-entered resolution context, when available. |

The memory model uses nearest-neighbor cosine similarity over dimensions, decline reason, cyclic time features, and severity. It is a recurrence aid, not proof of the current root cause.

## 6. Runtime configuration

`data/runtime_config.json` is managed through `PUT /api/config` by `control_server.py`.

Detection fields: `window_seconds`, `evaluation_seconds`, `persistence`, `min_attempts`, `min_history_attempts`, `min_drop_pp`, and `z_threshold`.

Priority fields: `priority_preset`, `priority_weights`, and `merchant_multipliers`.

## 7. Live Trial by Fire

`data/live_injections.json` contains judge-created live injections. Each record has an ID, name, filters, decline reason, approval rate, traffic share, duration, and activation timestamp. The generator reads it dynamically; the detector and diagnoser receive only resulting transactions.
