# Diccionario de datos — PagoTotal Intelligence

Documenta cada estructura de datos que produce o consume el pipeline, en el orden en que
una transacción las va atravesando: **transacción → alerta → diagnóstico → incidente
priorizado → entrada del dashboard → `dashboard_data.json`**, más los archivos de
soporte (`incident_memory.json`, `runtime_config.json`).

Convenciones generales:
- Todos los timestamps son ISO 8601 con offset UTC (`...+00:00`), generados con
  `datetime.now(UTC).isoformat()` o `event["completed_at"]`.
- Los montos en USD (`*_usd`) son floats de 2 decimales; los "por hora" (`*_per_hour_usd`)
  son el mismo monto extrapolado a partir de la duración real de la ventana analizada.
- Las "dimensiones" son siempre un subconjunto de: `merchant`, `provider`, `payment_method`,
  `country`, `issuing_bank`.

---

## 1. Transacción (evento atómico)

Producida por `generator.py::create_event()` (tiempo real u offline). Es la unidad mínima
que fluye por todo el sistema — el histórico, el stream en vivo y las capturas son todos
archivos `.jsonl` de este mismo esquema.

| Campo | Tipo | Descripción |
|---|---|---|
| `transaction_id` | str (UUID) | Identificador único del intento. |
| `checkout_id` | str (UUID) | Identificador del checkout — se repite entre un intento y sus reintentos. |
| `customer_id` | str (UUID) | Identificador del comprador (no se usa para PII real, es sintético). |
| `attempt_number` | int | 1 para el primer intento; se incrementa en cada reintento del mismo `checkout_id`. |
| `is_retry` | bool | `true` si este evento es un reintento de un intento fallido anterior. |
| `original_failed_attempt_id` | str \| null | `transaction_id` del intento que este reintento sigue (`null` si `attempt_number == 1`). |
| `created_at` | str (ISO) | Cuándo se creó el intento. |
| `merchant` | str | `PagoModa` \| `TravelNow` \| `TechStore`. |
| `provider` | str | `stripe` \| `adyen` \| `dlocal`. |
| `payment_method` | str | `card` \| `pse` \| `wallet` \| `pix` \| `cash_in_store`. |
| `country` | str | `MX` \| `CO` \| `BR`. |
| `issuing_bank` | str | Banco emisor, depende de `country` (ver `BANKS_BY_COUNTRY` en `generator.py`). |
| `amount` | float | Monto en moneda local. |
| `currency` | str | `MXN` \| `COP` \| `BRL`. |
| `amount_usd` | float | Monto convertido a USD (tasa fija en `generator.py`). |
| `status` | str | `approved` \| `declined` \| `failed` \| `cancelled` \| `expired` \| `processing`. |
| `decline_code` | str \| null | Código bancario (`"05"`…`"91"`) si `status == declined`; `null` si aprobado. |
| `decline_reason` | str \| null | Motivo legible — ver tabla de motivos de rechazo, abajo. |
| `provider_request_at` | str (ISO) | Cuándo se mandó la petición al provider. |
| `provider_response_at` | str (ISO) \| null | Cuándo respondió el provider (`null` si `status == processing`). |
| `processing_time_ms` | int | Latencia simulada del provider. |
| `completed_at` | str (ISO) \| null | Cuándo terminó el intento (`null` si `status == processing`). Es el timestamp que usa **todo** el resto del pipeline para ventanas y baseline. |

### Motivos de rechazo (`decline_reason`)

| Código | Motivo | Origen |
|---|---|---|
| `05` | `do_not_honor` | Rechazo bancario genérico. |
| `14` | `invalid_card` | Número de tarjeta inválido. |
| `41` | `lost_card` | Tarjeta reportada perdida. |
| `43` | `stolen_card` | Tarjeta reportada robada. |
| `51` | `insufficient_funds` | Fondos insuficientes. |
| `54` | `expired_card` | Tarjeta vencida. |
| `57` | `transaction_not_permitted` | Transacción no permitida para esa tarjeta/cuenta. |
| `91` | `issuer_unavailable` | Banco emisor no responde. |
| — | `provider_error` | No es rechazo bancario — `status == failed`. |
| — | `user_cancelled` | `status == cancelled`. |
| — | `payment_expired` | `status == expired`. |

---

## 2. Alerta

Producida por `detector.py::DetectionEngine.evaluate()` cuando una combinación de
dimensiones cruza el umbral estadístico (z-score + caída mínima) sostenido por
`persistence` evaluaciones consecutivas.

```
{
  "alert_id": "uuid",
  "type": "conversion_drop",
  "detected_at": "ISO",
  "persistence_evaluations": 3,
  "confidence": "high" | "medium",          // "high" si z_score ≤ -4
  "evidence": { ...ver abajo... }
}
```

**`evidence`** — la semilla del diagnóstico; siempre nace de una de las 4 combinaciones
de `SEGMENT_DEFINITIONS`: `provider×country`, `merchant×country`, `issuing_bank×country`,
`payment_method×country`.

| Campo | Tipo | Descripción |
|---|---|---|
| `detection_dimensions` | list[str] | Las 2 dimensiones que sembraron la alerta. |
| `segment` | dict | Valores de esas dimensiones (ej. `{"provider":"stripe","country":"BR"}`). |
| `window_started_at` / `window_ended_at` | str (ISO) | Ventana deslizante evaluada (5 min por defecto). |
| `attempts` / `approved` | int | Volumen de la ventana. |
| `observed_conversion` / `expected_conversion` | float (0-1) | Observado vs. baseline histórico. |
| `conversion_drop_pp` | float | Puntos porcentuales de caída. |
| `z_score` | float | Desviaciones estándar respecto al baseline. |
| `baseline_attempts` / `baseline_source` | int / str | Tamaño de muestra histórica y de qué nivel salió (`same_weekday_hour_segment` → `all_time_segment` → `same_weekday_hour_country` → `all_time_country`). |
| `estimated_lost_approvals` | float | Aprobaciones perdidas en la ventana. |
| `gross_lost_amount_usd` / `gross_lost_amount_per_hour_usd` | float | GMV bruto perdido (sin ajustar por recuperación). |
| `expected_unrecovered_amount_usd` / `..._per_hour_usd` | float | GMV que probablemente **no** se recupera por reintento — la cifra que prioriza el sistema. |
| `expected_recovery_rate` | float (0-1) | Probabilidad promedio de recuperación de esta pérdida. |
| `recovery_model_source` / `recovery_model_sample_size` | str / int | De qué nivel de `RecoveryEstimator` salió la tasa. |

---

## 3. Diagnóstico

Producido por `diagnoser.py::diagnose()`. Toma una alerta + las transacciones capturadas
+ el histórico, y hace drill-down jerárquico.

| Campo | Tipo | Descripción |
|---|---|---|
| `incident_id` | str (UUID) | Identificador único de este diagnóstico. |
| `alert_id` | str | Referencia a la alerta que lo originó. |
| `diagnosed_at` | str (ISO) | Cuándo se calculó. |
| `evidence_sufficient` | bool | `false` si ninguna dimensión concentra suficiente pérdida → el sistema **admite que no sabe**. |
| `confidence` | str | `high` \| `medium` \| `low`. |
| `reason` | str | Solo presente si `evidence_sufficient == false`: por qué no se pudo aislar una causa. |
| `root_cause_segment` | dict | Las dimensiones finales aisladas (semilla + drill-down). |
| `incident_window` | dict | `{started_at, ended_at}` de la ventana analizada. |
| `root_metrics` | dict | Las mismas métricas de `evidence` (ver arriba) pero recalculadas sobre `root_cause_segment`. |
| `drill_down_path` | list | Un elemento por cada dimensión que el algoritmo agregó, en orden. Ver abajo. |
| `payment_method_impact` | list | Desglose de impacto por método de pago dentro del segmento — igual forma que un elemento de `drill_down_path`, pero informativo, no parte del camino elegido. |
| `dominant_decline` | dict \| null | `{decline_reason, excess_declines, share_of_excess_declines}` — el motivo de rechazo con mayor exceso vs. el histórico. `null` si no hay uno claro. |
| `recommended_action` | str | Texto generado por `diagnoser.py::recommendation()` según qué dimensión domina (provider / issuing_bank / payment_method / genérico). Nunca se ejecuta, solo se sugiere. |
| `recurrence` | dict \| null | Resultado de `incident_memory.match()` — ver sección 6. |
| `explanation` | dict | Ver sección 4. |

**Cada elemento de `drill_down_path`** (y de `payment_method_impact`) trae todas las
métricas de `root_metrics` para ese sub-segmento, más:

| Campo extra | Tipo | Descripción |
|---|---|---|
| `value` | str | Valor de la dimensión en este paso (ej. `"Banorte"`). |
| `dimension` | str | Qué dimensión es (`merchant`, `provider`, `payment_method`, `issuing_bank`). |
| `segment` | dict | Segmento acumulado hasta este paso. |
| `contribution_to_parent_loss` | float | Qué % de la pérdida del padre explica este sub-segmento (solo en `drill_down_path`; se necesita ≥60% para que el algoritmo lo acepte). |
| `is_anomalous` | bool | Si este sub-segmento por sí solo cruza el umbral estadístico. |
| `is_selected` | bool | Si fue el elegido entre sus hermanos (`siblings`). |
| `siblings` | list | Los demás valores de esa misma dimensión, para comparar "normal" vs. "anómalo" (solo en `drill_down_path`). |

---

## 4. Explicación

Producida por `explainer.py` (`deterministic_explanation()` o `openai_explanation()`),
siempre a partir de los hechos ya calculados arriba — nunca decide la causa ni la acción.

| Campo | Tipo | Descripción |
|---|---|---|
| `mode` | str | `deterministic` \| `openai`. |
| `executive_summary` | str | Una línea con el impacto en dinero. |
| `operational_explanation` | str | Detalle completo: conversión, motivo de rechazo, y nota de recurrencia si aplica. |
| `recommended_action` | str | Copia de `diagnosis.recommended_action` (la IA no puede cambiarla). |
| `confidence` / `evidence_sufficient` | str / bool | Copia del diagnóstico. |
| `window_started_at` / `window_ended_at` | str (ISO) | Copia de `incident_window`. |
| `profiles.technical` | str | Para un operador (= `operational_explanation`). |
| `profiles.financial` | str | Para negocio — enfocado en el GMV no recuperado. |
| `profiles.simple` | str | Sin jerga técnica, para cualquier persona. |

---

## 5. Incidente priorizado

Producido por `prioritizer.py::prioritize()`. Agrupa diagnósticos cuyo `root_cause_segment`
se solapa (mismas dimensiones, mismos valores) y los ordena.

| Campo | Tipo | Descripción |
|---|---|---|
| `incident_key` | str | Firma legible del segmento (`"country=MX ∧ issuing_bank=Banorte ∧ ..."`). |
| `priority_rank` | int | 1 = más urgente/costoso. |
| `priority_score` | float | `cost_per_hour_usd × urgency_multiplier`. |
| `cost_per_hour_usd` | float | = `expected_unrecovered_amount_per_hour_usd` (o `gross_lost_amount_per_hour_usd` si el primero no está). |
| `urgency` | float | Tasa de crecimiento del daño (USD/min) si hay ≥2 lecturas del mismo incidente; si no, `\|z_score\|` como proxy. |
| `urgency_basis` | str | `growth_rate_usd_per_min` \| `z_score_proxy` — de dónde salió `urgency`. |
| `readings` | int | Cuántos diagnósticos distintos se agruparon en este incidente. |
| `diagnosis` | dict | El diagnóstico representativo del grupo (voto por mayoría de segmento exacto, ver `prioritizer.py::representative()`). |

---

## 6. Recurrencia (memoria, k-NN)

Producida por `incident_memory.py::match()`. Compara el diagnóstico actual contra
`data/incident_memory.json` con **k-nearest-neighbors (k=1) por similitud coseno** sobre
un vector de features (dimensiones afectadas, motivo de rechazo, hora/día cíclicos,
severidad). `null` si ningún registro cruza `minimum_similarity` (0.55 por defecto).

```
{
  "similarity": 0.80,        // 0-1
  "previous_incident": { ...registro de memoria, ver sección 8... },
  "method": "knn_cosine_v1"
}
```

---

## 7. Entrada de incidente del dashboard

Producida por `build_dashboard.py::build_incident_entries()` — es lo que realmente
consume el frontend, uniendo priorización + severidad + memoria en un solo objeto.

| Campo | Tipo | Descripción |
|---|---|---|
| `incident_id` / `alert_id` / `incident_key` | str | Ver secciones 3 y 5. |
| `priority_rank` / `priority_score` | int / float | Ver sección 5. |
| `cost_per_hour_usd` | float | GMV no recuperado esperado por hora (la cifra que prioriza). |
| `gross_lost_amount_per_hour_usd` | float | GMV bruto perdido por hora (contexto, sin ajustar). |
| `urgency` / `urgency_basis` / `readings` | — | Ver sección 5. |
| `severity` | str | `crit` \| `high` \| `warn` — ver `build_dashboard.py::classify_severity()`. |
| `status` | str | `active` \| `investigating` (`investigating` si `evidence_sufficient == false`). |
| `confidence_pct` | int (0-100) | De `drill_down_path[-1].contribution_to_parent_loss`, o 90/70/40 según `confidence` si no hubo drill-down. |
| `root_cause_segment` / `root_cause_label` | dict / str | Segmento final y su versión legible (`"card × MX × Banorte × PagoModa"`). |
| `duration_minutes` | float | Duración de la ventana del incidente. |
| `recurrence` | dict \| null | Ver sección 6. |
| `diagnosis` | dict | El diagnóstico completo (sección 3), embebido. |

---

## 8. `data/incident_memory.json`

```
{ "incidents": [ {...}, {...} ] }
```

Cada registro:

| Campo | Tipo | Descripción |
|---|---|---|
| `incident_id` | str | UUID (generado) o un id legible para los registros sembrados a mano. |
| `root_cause_segment` | dict | Igual forma que en todos los demás esquemas. |
| `decline_reason` | str \| null | Motivo dominante de ese incidente. |
| `resolved_at` | str (ISO) | **Solo** en registros sembrados a mano — implica que alguien confirmó la resolución. |
| `observed_at` | str (ISO) | En registros que el sistema agregó solo (`incident_memory.record_from_entry()`) — el sistema nunca afirma que algo "se resolvió", solo que lo observó. |
| `duration_minutes` | float | Duración de ese incidente. |
| `cost_per_hour_usd` | float | Su costo en su momento. |
| `resolution_note` | str \| null | Texto libre — **solo existe si un humano lo escribió**; los registros que agrega el sistema automáticamente siempre traen `null` (nunca inventa cómo se resolvió algo). |

Se llena de dos formas: (a) sembrado a mano, (b) `build_dashboard.py` hace
`incident_memory.upsert()` + `save()` al final de cada corrida, por cada incidente con
`evidence_sufficient == true`. `live_dashboard.py` **no** escribe aquí durante una corrida
en vivo (evitaría que un incidente activo se compare consigo mismo).

---

## 9. `data/runtime_config.json`

Controles de detección ajustables mientras `live_dashboard.py` corre, vía
`PUT /api/config` en `control_server.py`. Los 7 campos son obligatorios y deben ser
números positivos:

```
{
  "window_seconds": 300, "evaluation_seconds": 30, "persistence": 3,
  "min_attempts": 30, "min_history_attempts": 200,
  "min_drop_pp": 5.0, "z_threshold": 3.0
}
```

---

## 10. `frontend/dashboard_data.json` (esquema completo)

Lo que escribe `build_dashboard.py` / `live_dashboard.py` y lo único que lee el frontend.

| Campo top-level | Tipo | Descripción |
|---|---|---|
| `generated_at` | str (ISO) | Cuándo se generó este snapshot — el frontend lo usa para saber si hay algo nuevo que pintar. |
| `kpis` | dict | Ver abajo. |
| `chart` | dict | Serie de tiempo para el gráfico de conversión. |
| `incidents` | list | Lista de entradas de la sección 7, ya ordenadas por `priority_rank`. |
| `resolved` | list | Contenido crudo de `data/incident_memory.json` (sección 8) al momento de la corrida. |
| `analytics` | dict | Agregados `historical` / `live`, cada uno con `attempts`, `approved`, `conversion_pct`, `average_ticket_usd`, `by_country`, `by_provider`, `by_payment_method`. |

**`kpis`**

| Campo | Tipo |
|---|---|
| `current_conversion_pct` / `expected_conversion_pct` | float |
| `transactions_window` | int |
| `active_incidents` / `critical_count` / `high_count` / `investigating_count` | int |
| `expected_unrecovered_gmv_per_hour_usd` | float |
| `gross_lost_gmv_per_hour_usd` | float |
| `recovery_horizon_hours` | float |

**`chart`**

| Campo | Tipo |
|---|---|
| `period_seconds` | int — tamaño de cada punto (60 = 1 min). |
| `window_seconds` / `sustain_evaluations` | int — mismos parámetros que el detector, para dibujar el umbral. |
| `points[]` | `{t, observed_pct, expected_pct, threshold_pct, state}` — `state` es `ok` \| `breach` \| `incident`. |

---

## Resumen de archivos en disco

| Archivo | Quién lo escribe | Contenido |
|---|---|---|
| `data/history.jsonl` | `generate_history.py` | Transacciones "normales" (sin inyecciones) — baseline. |
| `data/live_transactions.jsonl` | `generator.py` / `live_dashboard.py` | Stream capturado, incluye reintentos. |
| `data/incident_training_batch.jsonl` | `generate_incident_batch.py` | Lote offline con varios incidentes distintos, para poblar memoria rápido. |
| `data/alerts.jsonl` | `build_dashboard.py` | Una alerta (sección 2) por línea. |
| `data/diagnoses.jsonl` | `build_dashboard.py` | Un diagnóstico (sección 3, sin `explanation`) por línea. |
| `data/priorities.json` | `build_dashboard.py` | Lista de incidentes priorizados (sección 5) en JSON. |
| `data/incident_memory.json` | sembrado a mano + `build_dashboard.py` | Ver sección 8. |
| `data/runtime_config.json` | `control_server.py` (`PUT /api/config`) | Ver sección 9. |
| `frontend/dashboard_data.json` | `build_dashboard.py` / `live_dashboard.py` | Ver sección 10 — lo único que lee el navegador. |
