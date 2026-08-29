# TecTeam1-NautaYunoHack

## Stream simulado de transacciones

El generador emite eventos en formato JSON Lines con las dimensiones necesarias
para diagnóstico, monto local (`amount`, `currency`) y monto comparable
(`amount_usd`). También modela el ciclo de vida: `created_at`,
`provider_request_at`, `provider_response_at`, `completed_at` y
`processing_time_ms`.

```powershell
py src/generator.py --events-per-second 5
```

Para una demostración reproducible con dos incidentes simultáneos:

```powershell
py src/generator.py --events-per-second 10 --injections examples/injections.json
```

Las inyecciones se definen en JSON. Cada una admite `start_after_seconds`,
`approval_rate`, `filters` (merchant, provider, payment_method, country,
issuing_bank), `decline_reason` y opcionalmente `duration_seconds`.

## Histórico normal para construir el baseline

Genera 28 días de datos sin incidentes en `data/history.jsonl`:

```powershell
py src/generate_history.py --days 28 --events-per-minute 5
```

El histórico no contiene eventos en `processing`; son resultados finales y se
puede calcular la conversión base directamente. Usa `--seed` para reproducir
exactamente el mismo dataset.

## Motor de detección

El detector construye una conversión esperada desde el histórico para cada
segmento y compara una ventana móvil del stream. Evalúa `provider × country`,
`merchant × country`, `issuing_bank × country` y `payment_method × country`.
Sólo emite una alerta cuando hay volumen suficiente, una caída mínima, una
desviación de al menos 3 sigmas y tres evaluaciones consecutivas anómalas.

Primero crea el histórico y luego conecta ambos procesos:

```powershell
py src/generate_history.py --days 28 --events-per-minute 5
py src/generator.py --events-per-second 50 --injections examples/injections.json |
  py src/detector.py --history data/history.jsonl --window-seconds 120 --evaluation-seconds 30
```

El detector escribe alertas JSON Lines en la consola. Para la configuración
productiva recomendada usa sus valores por defecto: ventana de 5 minutos,
evaluación cada minuto y tres evaluaciones persistentes.

Agrega `--verbose` al detector para ver el número de transacciones analizadas en
cada evaluación; estas líneas se muestran en la consola pero no se guardan como
alertas.

## Motor de diagnóstico

`diagnoser.py` recibe una alerta y las transacciones capturadas durante el
stream. Calcula las aprobaciones perdidas contra el baseline y hace drill-down
por merchant, provider, método y banco emisor. Sólo incorpora un subsegmento a
la causa raíz si explica al menos 60% de la pérdida de su segmento padre, tiene
volumen suficiente y también es estadísticamente anómalo. Si no ocurre, reporta
evidencia insuficiente en vez de inventar una causa.

Para ejecutarlo, guarda la salida del detector en `data/alerts.jsonl` y captura
el stream en `data/live_transactions.jsonl`. En PowerShell puedes capturar ambos
sin detener el pipeline:

```powershell
py src/generator.py --events-per-second 50 --injections examples/injections.json |
  Tee-Object -FilePath data/live_transactions.jsonl -Encoding utf8 |
  py src/detector.py --history data/history.jsonl --window-seconds 60 --evaluation-seconds 15 --persistence 2 --min-attempts 30 --min-history-attempts 50 |
  Tee-Object -FilePath data/alerts.jsonl
```

Después de que se genere una alerta, detén el stream con `Ctrl+C` y ejecuta:

```powershell
py src/diagnoser.py --history data/history.jsonl --transactions data/live_transactions.jsonl --alert data/alerts.jsonl
```

El resultado es un JSON con la causa raíz, ruta de drill-down, impacto,
código de rechazo dominante, nivel de confianza y acción recomendada.

## Motor de explicación

`explainer.py` convierte el diagnóstico estructurado en dos mensajes: un resumen
ejecutivo con el impacto económico y una explicación para operaciones con la
evidencia y la acción sugerida. Sin argumentos extra utiliza plantillas
deterministas, por lo que no inventa información ni necesita una API.

```powershell
py src/explainer.py --diagnosis data/diagnosis.json
```

Opcionalmente puede usar OpenAI únicamente como redactor de los hechos ya
calculados. Configura `OPENAI_API_KEY`, instala el SDK oficial y elige el modelo
que tengan disponible:

```powershell
py -m pip install openai
$env:OPENAI_API_KEY = "tu_api_key"
py src/explainer.py --diagnosis data/diagnosis.json --use-openai --model gpt-5
```

La salida de IA se fuerza a un esquema JSON con resumen ejecutivo, explicación
operativa, acción recomendada y nota de incertidumbre. No se usa la IA para
detectar ni diagnosticar incidentes.

## Dashboard en vivo

`live_dashboard.py` conecta todo el pipeline en tiempo real: genera transacciones
a ritmo real (`generator.create_event`), las alimenta al detector, diagnostica
cada alerta apenas aparece, prioriza incidentes simultáneos, revisa recurrencia
contra `data/incident_memory.json` y reescribe `frontend/dashboard_data.json`
cada `--refresh-seconds`. El frontend hace polling (cada 8s) en vez de cargar
una sola foto estática, así que el dashboard se actualiza solo mientras el
script sigue corriendo.

```bash
python3 src/generate_history.py --days 28 --events-per-minute 5
python3 src/live_dashboard.py --injections examples/injections.json
```

En otra terminal:

```bash
cd frontend && python3 -m http.server 8000
```

Abre `http://localhost:8000/PagoTotal-Intelligence_1.html` y déjalo corriendo —
los incidentes aparecerán solos cuando el detector los encuentre (con los
parámetros por defecto, ~5-8 minutos). `Ctrl+C` detiene `live_dashboard.py`;
el frontend simplemente deja de recibir actualizaciones nuevas.

Para un snapshot único a partir de un stream ya capturado (sin dejarlo
corriendo), usa `build_dashboard.py` como se describe arriba — sigue siendo
útil para reproducir un análisis puntual o para debugging.
