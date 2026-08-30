# PagoTotal Intelligence — Control Tower

Sistema demostrable de monitoreo y diagnóstico para una plataforma de orquestación de pagos. Observa un stream simulado, detecta caídas relevantes de conversión, aísla su causa raíz y muestra evidencia, impacto económico y una recomendación para revisión humana.

Creado para el reto **La torre de control** de NextWave Hackathon. El sistema no modifica rutas de pago ni ejecuta remediaciones.

## Qué hace

- Distingue caídas reales de conversión frente a ruido estadístico.
- Diagnostica por merchant, provider, método, país, banco emisor y motivo de rechazo.
- Explica desde cuándo ocurre, a quién afecta, cuánto cuesta y por qué lo cree.
- Separa y prioriza incidencias simultáneas.
- Reconoce recurrencias con memoria de incidentes.
- Permite una inyección en vivo con **Trial by Fire**.

## Requisitos

- Python **3.11+**
- No hay dependencias obligatorias para el pipeline central.
- OpenAI es opcional para el chatbot y para redactar explicaciones.

## Demo rápida

Abre dos terminales en la raíz del repositorio.

**Terminal 1 — simulación, detección, diagnóstico y actualización de datos**

```powershell
# Solo si data/history.jsonl no existe localmente:
py src/generate_history.py --days 28 --events-per-minute 5

py src/live_dashboard.py --history data/history.jsonl --injections examples/injections.json --events-per-second 15 --refresh-seconds 10
```

**Terminal 2 — dashboard, configuración y Trial by Fire**

```powershell
py src/control_server.py --port 8001
```

Abre [http://localhost:8001/PagoTotal-Intelligence_1.html](http://localhost:8001/PagoTotal-Intelligence_1.html).

La inyección de ejemplo genera dos incidencias simultáneas. Detén cada proceso con `Ctrl+C`. En macOS/Linux usa `python3` en lugar de `py` si es necesario.

## Arquitectura

```text
historical JSONL ──> baseline + recovery model ─────────────────────┐
                                                                    │
mock stream ─> detector ─> diagnoser ─> prioritizer ─> dashboard JSON
                                                                    │
Trial by Fire / settings ─> control server ─────────────────────────┘
                                                                    │
browser dashboard <─────────────────────────────────────────────────┘
```

| Componente | Archivo | Responsabilidad |
| --- | --- | --- |
| Generador | `src/generator.py` | Emite transacciones, montos, ciclo de vida, reintentos e inyecciones. |
| Histórico | `src/generate_history.py` | Genera operación normal para el baseline. |
| Detector | `src/detector.py` | Detecta deterioros persistentes de conversión. |
| Diagnóstico | `src/diagnoser.py` | Hace drill-down y aísla el segmento responsable. |
| Costo | `src/cost_estimator.py` | Estima pérdida atribuible y recuperación esperada. |
| Prioridad | `src/prioritizer.py` | Ordena las incidencias por impacto y estrategia. |
| Memoria | `src/incident_memory.py` | Busca casos similares por similitud coseno. |
| Runtime | `src/live_dashboard.py` | Conecta el pipeline y escribe `frontend/dashboard_data.json`. |
| API/UI | `src/control_server.py` + `frontend/` | Sirve el dashboard, controles, chatbot y Trial by Fire. |

## Datos

Cada línea de los archivos `.jsonl` representa una transacción. Incluye:

```text
transaction_id, checkout_id, customer_id, attempt_number
created_at, provider_request_at, provider_response_at, completed_at
merchant, provider, country, payment_method, issuing_bank
status, decline_code, amount, currency, amount_usd, processing_time_ms
```

Los estados que cuentan para conversión son `approved`, `declined`, `failed` y `expired`. Las dimensiones de diagnóstico son:

```text
merchant × provider × payment_method × country × issuing_bank × decline_code
```

## Detección y diagnóstico

El detector compara una ventana móvil del stream contra una conversión esperada calculada desde el histórico. Por defecto exige:

| Regla | Valor |
| --- | ---: |
| Ventana | 300 s |
| Evaluación | 30 s |
| Persistencia | 3 evaluaciones |
| Mínimo actual | 30 intentos |
| Caída mínima | 5 pp |
| Umbral estadístico | Z = 3.0 |

El diagnóstico solo profundiza a un subsegmento si sigue siendo anómalo, tiene volumen suficiente y explica al menos 60% de las aprobaciones perdidas de su segmento padre. Si la evidencia no alcanza, reporta incertidumbre en vez de inventar una causa.

Los parámetros se ajustan en **Settings**. Se guardan en `data/runtime_config.json` y se aplican en la siguiente actualización del proceso vivo.

## Costo de incidencia

Se usan importes reales, no ticket promedio:

```text
aprobaciones perdidas = max(0, intentos × conversión esperada − aprobaciones reales)

GMV no recuperado esperado =
Σ(monto × proporción atribuible × (1 − probabilidad de recuperación))
```

La probabilidad de recuperación se aprende de reintentos exitosos históricos por checkout, con fallbacks por método, motivo de rechazo, país y hora.

El dashboard muestra la estimación de la **ventana de diagnóstico actual**:

```text
costo en ventana = costo estimado por hora × duración de la ventana / 60
```

Ejemplo: USD 3,600/h durante una ventana de 5 minutos produce USD 300. No es un acumulado desde que inició el incidente ni una conciliación contable final.

Una incidencia puede mostrar costo cero si aún no hay evidencia suficiente, no existen aprobaciones perdidas atribuibles o el importe se redondea visualmente a cero.

## Dashboard

El selector superior adapta toda la interfaz:

- **Technical:** stream, umbrales, evidencia estadística y árbol de causa raíz.
- **Financial:** exposición económica, costo por hora y prioridad.
- **Simple:** qué pasó, a quién afecta y qué debe revisarse.

La estrategia de prioridad se configura en **Settings → Prioritization strategy**. Puede ponderar impacto económico, urgencia, caída de conversión e importancia de cada merchant; no cambia la detección.

## Trial by Fire

El botón **Trial by Fire** crea una incidencia nueva en vivo sin reiniciar el proceso. El juez puede seleccionar merchant, provider, país, método, banco emisor, motivo de rechazo, tasa de aprobación, proporción de tráfico y duración.

La API guarda la inyección en `data/live_injections.json`. El generador la recarga y el detector/diagnóstico reaccionan solo a los eventos generados; no conocen la causa configurada en el formulario.

Para una prueba limpia, inicia el runtime sin los casos predefinidos:

```powershell
Set-Content -Path data/empty_injections.json -Value "[]"
py src/live_dashboard.py --history data/history.jsonl --injections data/empty_injections.json --events-per-second 15 --refresh-seconds 10
```

Usa una proporción de tráfico de al menos 30%, una tasa de aprobación baja y una duración igual o mayor a la ventana para obtener evidencia suficiente.

## Memoria de incidentes

Los diagnósticos suficientes se guardan en `data/incident_memory.json`. La memoria compara dimensiones, motivo de rechazo, hora y severidad mediante un vecino cercano por similitud coseno. Cuando encuentra una recurrencia, el dashboard muestra el caso previo y su resolución.

Para sembrar la memoria:

```powershell
py src/generate_history.py --days 60 --events-per-minute 5
py src/generate_incident_batch.py --scenarios examples/incident_training_scenarios.json --hours 1.75 --events-per-second 15
py src/build_dashboard.py --history data/history.jsonl --transactions data/incident_training_batch.jsonl
```

## OpenAI y chatbot (opcional)

El proyecto funciona sin OpenAI. La API se usa únicamente para redactar diagnósticos ya calculados y responder el chatbot con agregados locales de histórico, stream, incidencias y memoria. No decide alertas, costos ni causas raíz.

```powershell
py -m pip install -r requirements.txt
$env:OPENAI_API_KEY = "tu_clave"
py src/control_server.py --port 8001 --chat-model gpt-5
```

La clave se lee de `OPENAI_API_KEY` en el entorno del servidor. **No se carga automáticamente un archivo `.env`**. Nunca subas una clave al repositorio ni al frontend.

Para usar OpenAI al redactar explicaciones del stream:

```powershell
py src/live_dashboard.py --history data/history.jsonl --injections examples/injections.json --use-openai --model gpt-5
```

## Ejecución offline y pruebas

Para reconstruir el dashboard a partir de un stream capturado:

```powershell
py src/build_dashboard.py --history data/history.jsonl --transactions data/live_transactions.jsonl
py src/control_server.py --port 8001
```

Para ejecutar pruebas:

```powershell
py -m unittest discover -s tests -v
```

## Estructura y límites

```text
src/         Pipeline y servidor local
frontend/    Dashboard y snapshot de datos
examples/    Escenarios e inyecciones reproducibles
data/        Datos generados localmente
tests/       Pruebas unitarias
```

Los JSONL históricos pueden ser grandes; genéralos localmente. Esta es una simulación, los costos son estimaciones operativas y toda recomendación requiere una decisión humana.
