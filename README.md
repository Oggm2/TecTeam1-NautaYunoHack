# TecTeam1-NautaYunoHack

## Stream simulado de transacciones

El generador emite eventos en formato JSON Lines con las dimensiones necesarias
para diagnóstico, monto local (`amount`, `currency`) y monto comparable
(`amount_usd`). Cada intento incluye `checkout_id`, `customer_id`,
`attempt_number` y la relación con el intento fallido anterior, de modo que se
puedan identificar reintentos exitosos. También modela el ciclo de vida: `created_at`,
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

## GMV no recuperado esperado

Además del GMV bruto perdido, el sistema estima el GMV que probablemente no se
recupere tras un rechazo. Para cada fallo atribuible al incidente, suma el
monto real de la transacción —no usa ticket promedio— y lo descuenta por la
probabilidad de que el checkout se recupere en las siguientes 24 horas:

```text
GMV no recuperado esperado = Σ(monto × atribución al incidente × (1 − P(recuperación)))
```

La primera versión de `recovery_estimator.py` aprende tasas históricas
suavizadas, con fallbacks por método de pago, motivo de rechazo, país y hora.
El entrenamiento usa un checkout fallido que luego logra un pago aprobado
dentro del horizonte. La puntuación de cada rechazo es inmediata; las etiquetas
se incorporan al histórico una vez que pasa el horizonte. Cambia el horizonte
con `--recovery-horizon-hours` en `detector.py`, `diagnoser.py` o
`build_dashboard.py`.

El dashboard prioriza por GMV no recuperado esperado por hora y conserva el
GMV bruto perdido en menor tamaño para dar contexto antes del ajuste de
recuperación.

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

## Memoria de incidentes con ML

`incident_memory.py` compara cada diagnóstico nuevo contra `data/incident_memory.json`
usando un modelo de **k-nearest-neighbors (k=1) por similitud coseno**, no
comparación exacta de segmentos. Cada incidente se convierte en un vector de
features: qué dimensiones están involucradas y sus valores (one-hot), el
motivo de rechazo dominante, hora del día y día de la semana codificados
cíclicamente (seno/coseno, para que las 23:00 y las 00:00 se traten como
cercanas) y la severidad (log del costo/hora). Esto generaliza mejor que el
solapamiento exacto: dos incidentes con el mismo patrón (misma dimensión
afectada, hora similar, mismo motivo de rechazo) pueden coincidir aunque el
merchant o banco exacto sea distinto. No requiere numpy/scikit-learn — con
la cantidad de incidentes que maneja este proyecto, una búsqueda por fuerza
bruta en Python puro es suficiente y evita una dependencia extra antes de
una demo en vivo.

Cuando hay coincidencia, esa recurrencia ahora se inyecta también en
`explainer.py`: la explicación operativa menciona la fecha y % de similitud
del incidente anterior, y su nota de resolución si existe — ya no es
necesario ir a la vista de Incident Memory para enterarte de que "esto ya
pasó antes".

La memoria también **aprende entre corridas**: al final de `build_dashboard.py`,
cada incidente con evidencia suficiente se guarda (o actualiza) en
`data/incident_memory.json` vía `incident_memory.upsert()`. La próxima vez
que corras el pipeline, esos incidentes ya son candidatos de recurrencia.
`live_dashboard.py` **no** persiste automáticamente durante una corrida en
vivo (evita que un incidente activo se compare consigo mismo unos segundos
después); usa `build_dashboard.py` para cerrar el ciclo de aprendizaje.

### Poblar la memoria con varios incidentes de una sola vez

`generate_incident_batch.py` genera un lote de transacciones (sin esperar en
tiempo real) con varios incidentes distintos inyectados en distintos puntos
del rango, usando un archivo de escenarios con el mismo formato que
`examples/injections.json` (ver `examples/incident_training_scenarios.json`
para un ejemplo con 5 escenarios variados). Luego corre `build_dashboard.py`
sobre ese lote una sola vez para diagnosticar todos los incidentes y
sembrar `incident_memory.json` con ejemplos reales y variados, en vez de
correr `live_dashboard.py` una vez por escenario:

```powershell
py src/generate_history.py --days 60 --events-per-minute 5
py src/generate_incident_batch.py --scenarios examples/incident_training_scenarios.json --hours 1.75 --events-per-second 15
py src/build_dashboard.py --transactions data/incident_training_batch.jsonl
```

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

Para usar OpenAI exclusivamente al redactar diagnósticos ya confirmados:

```bash
python3 src/live_dashboard.py --injections examples/injections.json --use-openai --model gpt-5
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
