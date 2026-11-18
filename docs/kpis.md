# KPIs y métricas

Este documento define **qué mide** ClusterDB Monitor, **cómo se calcula** y qué
significa cada valor del dashboard. La regla principal es simple: si no hay datos
suficientes, se muestra **“Sin datos suficientes”**; no se rellenan valores.

---

## 1. Fuentes de datos

| Fuente | Dónde vive | Qué aporta |
|---|---|---|
| Muestras del monitor | tabla `samples` (SQLite) | Disponibilidad, latencia, rol del primario, lag, heartbeat, IDs de venta. |
| Incidentes | tabla `incidents` (SQLite) | Inicio, fin, duración, nodo caído, nuevo primario, RPO. |
| Failovers | tabla `failovers` (SQLite) | Cambio de primario y recuperación medida. |
| Eventos | tabla `events` (SQLite) | Historial legible para el dashboard. |
| Transacciones | `tx_events` (SQLite) + `attempts.jsonl` del generador | Intentos OK/fallidos y su latencia. |
| Respaldos | API de `postgres-backup` | Último respaldo, tamaño, SHA-256, verificación, historial. |

El monitor consulta PostgreSQL **a través de HAProxy** y la API REST de Patroni
para el estado de los nodos. No usa el socket de Docker.

### Ventana de análisis

El dashboard permite elegir:

- 15 minutos, 1 hora (por defecto), 6 horas, 24 horas o **todo lo observado**.
- En la API, `window_minutes=0` significa “todo el historial”.

Todos los KPIs de ventana (disponibilidad, SLA, MTTR, MTBF, fallos, transacciones)
se calculan **solo con datos dentro de esa ventana**. El RTO y el RPO usan el
último failover registrado.

---

## 2. Definición de los 9 KPIs

### 2.1 Disponibilidad

**Qué es:** porcentaje del tiempo observado en que el servicio PostgreSQL estuvo
disponible para escrituras a través de HAProxy.

**Cómo se calcula:**

```text
disponibilidad = tiempo_disponible / tiempo_observado × 100
```

- Cada muestra cubre el intervalo hasta la siguiente.
- Un hueco mayor a `max(10 s, 4 × intervalo_de_sondeo)` se recorta para no contar
  tiempo en que el monitor no estaba observando.
- Se requieren al menos **10 muestras**; si no, `pct = null` y se explica el
  motivo.

**Por qué no es “muestras OK / total”:** una caída de 30 s no vale lo mismo que
una muestra aislada de 2 s. La ponderación por tiempo refleja mejor la
disponibilidad real.

**Lectura:** una ventana corta que contiene un failover muestra una disponibilidad
baja; al acumular tiempo sin fallos, el porcentaje sube. Siempre hay que leerlo
junto con la ventana y con el tiempo de caída.

---

### 2.2 SLA

**Qué es:** cumplimiento del objetivo de disponibilidad configurado
(`SLA_TARGET_PERCENT`, por defecto `99.9 %`).

**Cómo se calcula:**

```text
tiempo_permitido = tiempo_observado × (100 − objetivo) / 100
cumple           = disponibilidad ≥ objetivo
margen_pp        = disponibilidad − objetivo
presupuesto      = tiempo_permitido − tiempo_caído
```

- `presupuesto` negativo significa que ya se consumió el tiempo de caída
  permitido para esa ventana.
- Si no hay disponibilidad calculable, `met = null` y se muestra el motivo.

**Importante:** el SLA es un **objetivo**, no una medición. En una demo de pocos
minutos, cualquier failover agota el presupuesto de 99.9 %. El dashboard muestra
el margen real y no oculta el incumplimiento.

---

### 2.3 MTTR — Mean Time To Repair

**Qué es:** tiempo medio de reparación de los incidentes **resueltos** en la
ventana.

```text
MTTR = promedio(duración de incidentes resueltos)
```

- Un incidente abierto cuenta en “fallos” pero no en el promedio.
- Si no hay incidentes resueltos: `null` + “Sin incidentes resueltos en la
  ventana”.

**No confundir con RTO:** MTTR es lo que tardó la recuperación observada; RTO es
el objetivo máximo aceptable.

---

### 2.4 MTBF — Mean Time Between Failures

**Qué es:** tiempo operativo medio entre fallos dentro de la ventana.

```text
MTBF = (tiempo_observado − suma(duraciones de incidentes)) / número_de_fallos
```

- Requiere al menos **1 minuto** de observación y al menos un fallo.
- Si no hay fallos: `null` + “Sin fallos detectados en la ventana”.
- Si la ventana es muy corta: “Sin datos suficientes”.

**Lectura:** en una demo de minutos, el MTBF es pequeño porque hay poco tiempo
operativo acumulado; no debe interpretarse como una tasa de producción.

---

### 2.5 RTO observado

**Qué es:** tiempo que tardó el servicio en volver a estar disponible tras el
último failover.

```text
RTO_observado = recovery_s del último failover
cumple        = RTO_observado ≤ RTO_TARGET_SECONDS (30 s)
```

- Se mide desde que el monitor detecta el incidente hasta que vuelve a haber un
  primario escribible a través de HAProxy.
- Si no hay failover con recuperación medida: `null` + motivo.
- El objetivo (`30 s`) se muestra aparte; **nunca** se presenta como resultado.

---

### 2.6 RPO observado

**Qué es:** pérdida de transacciones **confirmadas** durante el failover.

**Cómo se verifica:**

1. Antes del failover, el monitor conoce el último `ha_heartbeat` confirmado en el
   primario.
2. Tras la promoción, busca ese mismo `id` en el nuevo primario.
3. Si existe: `lost_records = 0` y `observed_seconds = 0`.
4. Si no existe: `lost_records ≥ 1`, estado `perdida_confirmada` y
   `observed_seconds = null`.

**Por qué no se reporta una ventana en segundos cuando hay pérdida:** sin los
datos del nodo que falló no se puede saber cuántos segundos de transacciones se
perdieron. Inventar ese número sería incorrecto. El objetivo de `60 s` se muestra
solo como referencia.

**En la validación final:** `0` registros perdidos, verificado en el nodo
promovido.

---

### 2.7 Último failover

**Qué es:** el último cambio de primario registrado.

Muestra:

- Primario anterior y nuevo.
- Fecha/hora de detección.
- Recuperación medida (`recovery_s`).
- Si el RTO se cumplió.
- Si el RPO fue verificado y cuántos registros se perdieron.

Si no hay failovers: “Aún no se ha registrado un failover real”.

---

### 2.8 Fallos detectados

**Qué es:** incidentes de disponibilidad en la ventana.

```text
detectados = total de incidentes
resueltos  = incidentes con duración medida
abiertos   = detectados − resueltos
```

- Un incidente se abre cuando el servicio falla y se cierra cuando vuelve.
- Con `FAILURE_THRESHOLD_SAMPLES=1` se registra el primer sondeo fallido; un
  failover real puede durar menos que dos sondeos.
- Los reinicios planificados del monitor también pueden aparecer como eventos,
  pero no necesariamente como fallo del servicio.

---

### 2.9 Transacciones OK / fallidas

Dos lecturas complementarias:

| Valor | Fuente | Significado |
|---|---|---|
| `window_ok` / `window_failed` | tabla `tx_events` filtrada por ventana | Intentos de venta dentro de la ventana seleccionada. |
| `total_ok` / `total_failed` | `stats.json` del generador | Acumulado desde que arrancó el generador. |

- El generador usa una `idempotency_key` UUID por venta lógica y
  `ON CONFLICT DO NOTHING`: un reintento no duplica la venta.
- Cada intento se registra en `attempts.jsonl`; el monitor lo ingesta de forma
  incremental y evita duplicados por `attempt_id`.
- Una transacción fallida durante el failover es esperable: la conexión se corta
  mientras HAProxy cambia de nodo.

---

## 3. Los 6 gráficos

| Gráfico | Qué muestra | Fuente |
|---|---|---|
| Disponibilidad del servicio (%) | Serie temporal con línea de SLA. | `samples` agrupadas por bucket. |
| Latencia del servicio (ms) | Latencia promedio y máxima por intervalo. | `samples.latency_ms`. |
| Retraso de replicación (MB) | Lag máximo por intervalo. | `samples.replication_lag_bytes`. |
| Transacciones por intervalo | OK y fallidas por bucket. | `tx_events`. |
| Recuperación de failovers (RTO) | Barras de recuperación con línea de objetivo. | `failovers`. |
| Tamaño de respaldos (KB) | Evolución del tamaño de los `.dump`. | API de respaldos. |

Los buckets se adaptan a la ventana: 30 s para ≤ 30 min, 60 s para ≤ 6 h, 300 s
para ≤ 24 h y 900 s para ventanas mayores o “todo lo observado”. **No se
interpolan puntos**: si no hay muestras, el gráfico muestra “Sin datos
suficientes”.

---

## 4. Estados del servicio y de los nodos

El dashboard separa dos conceptos:

| Tarjeta | Significado |
|---|---|
| Servicio HAProxy | ¿Se puede escribir a través de HAProxy en este momento? |
| Nodo primario | Nodo que Patroni reporta como `primary` y que HAProxy entrega. |
| Nodo réplica | Nodo que Patroni reporta como `replica` y su estado. |
| Replicación | `streaming`, lag en bytes y estado síncrono. |
| Respaldos | Último respaldo verificado y próximo intento. |
| Generador TX | Estado del generador y conteo de ventas. |

Una réplica caída no implica servicio caído. Un primario vivo que HAProxy no
entrega sí implica servicio no disponible, porque la aplicación usa HAProxy.

---

## 5. Manejo de “sin datos suficientes”

Se devuelve `null` y un motivo cuando:

- No hay muestras.
- Hay menos de 10 muestras para calcular disponibilidad.
- No hay incidentes resueltos (MTTR).
- No hay fallos o hay menos de 1 minuto de observación (MTBF).
- No hay failover con recuperación medida (RTO).
- No hay verificación de RPO posterior a un failover (RPO).
- No hay disponibilidad calculable (SLA).

El dashboard muestra el motivo en la tarjeta correspondiente. Esto es deliberado:
un KPI sin base de datos no debe verse como `0`, `100 %` ni como el objetivo
configurado.

---

## 6. Resultados medidos en la validación final

Fecha: **2026-11-06** (hora local de la laptop, zona `America/El_Salvador`).

| KPI | Resultado | Nota |
|---|---|---|
| Failover | `postgres-master` → `postgres-replica` | Detectado por Patroni y por el monitor. |
| RTO / recuperación | **4.7 s** | Objetivo 30 s; cumple. |
| RPO observado | **0 registros perdidos** | Verificado con el último `ha_heartbeat`. |
| MTTR | **4.7 s** | Un incidente resuelto. |
| Fallos detectados | **1** | El failover provocado. |
| Reincorporación | **4.4 s** | El nodo caído volvió como réplica en streaming. |
| Split-brain | **No** | Un solo primario al final. |
| Replicación | `streaming`, lag **0 bytes** | Verificación activa 6/6 OK. |
| Respaldo | **21,553 bytes**, SHA-256, TOC **47 entradas** | Verificado. |

### Sobre disponibilidad y SLA en la demo

La disponibilidad se calcula sobre la ventana observada. En la validación, la
ventana era de pocos minutos y contenía el failover, por lo que la disponibilidad
medida fue inferior al 99.9 %. En la fotografía de cierre
(`2026-11-06T11:13:19Z`, 215 muestras) era **98.94 %** con **4.704 s** de caída y
un margen de SLA de **−0.9649 pp**. Eso **no es un error**: es el resultado real
de una ventana corta con una caída. Al dejar correr el stack sin fallos, el
porcentaje sube y el margen del SLA mejora. Para la presentación se recomienda:

- Usar la ventana “Todo lo observado” para mostrar la evolución.
- Explicar que el SLA es un objetivo operativo de largo plazo.
- Mostrar el RTO y el RPO medidos como evidencia del failover.

---

## 7. Relación entre las métricas

```text
Fallo del primario
      │
      ├── El servicio deja de estar disponible  →  incidente
      │                                           ├── duración → MTTR
      │                                           └── tiempo entre fallos → MTBF
      │
      ├── Patroni promueve la réplica
      │        └── tiempo hasta servicio disponible → RTO observado
      │
      └── ¿Se perdió la última transacción confirmada?
               ├── No → RPO observado = 0 registros
               └── Sí → RPO = pérdida confirmada (sin inventar segundos)

Disponibilidad y SLA se calculan sobre el tiempo observado, no sobre el número
de eventos.
```

---

## 8. Limitaciones de las métricas

- El monitor sondea cada 2 s: una caída más corta que el intervalo puede no
  medirse con precisión exacta.
- El RTO se mide hasta que el monitor ve el servicio recuperado; incluye la
  latencia del sondeo.
- El MTBF en ventanas cortas es poco representativo.
- La disponibilidad depende de HAProxy; una caída de HAProxy cuenta como
  indisponibilidad.
- El RPO de replicación (0 registros) es distinto del RPO de respaldos (intervalo
  de 6 h). No se mezclan en el dashboard.
