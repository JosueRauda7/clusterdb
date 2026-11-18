# Pruebas y resultados

Registro de las pruebas ejecutadas sobre ClusterDB Monitor, con comandos,
resultados esperados y resultados medidos. La validación final se ejecutó el
**2026-11-06** en Windows 11 + Docker Desktop/WSL2, sobre un stack reconstruido
desde cero (`docker compose down -v` y `docker compose up -d --build`).

> Los valores de disponibilidad, SLA y MTBF dependen de la ventana observada. Los
> resultados de este documento son una **fotografía** de la ejecución indicada;
> al seguir corriendo el stack, esos valores cambian. Los valores de RTO, RPO,
> reincorporación y respaldo sí quedan registrados por evento.

---

## 1. Resumen de resultados

| Área | Prueba | Resultado |
|---|---|---|
| Unitarias | 30 pruebas de KPIs y persistencia SQLite | **30/30 OK** |
| Configuración | `docker compose config --quiet` | **OK** |
| Scripts | Sintaxis de los 9 scripts Bash | **OK** |
| Integral | Verificación de 10 comprobaciones (PowerShell) | **10/10 OK** |
| Integral | Verificación de 10 comprobaciones (Bash) | **10/10 OK** |
| Failover | Detener el primario y esperar la promoción | **4.704 s** de recuperación |
| RTO | Objetivo 30 s | **Cumple** |
| RPO | Pérdida de transacciones confirmadas | **0 registros** |
| Reincorporación | Nodo caído vuelve como réplica en streaming | **4.42 s** |
| Split-brain | Dos nodos primarios | **No; un solo primario** |
| Replicación | Verificación activa (latido en ambos nodos) | **6/6 comprobaciones OK** |
| Respaldo | `pg_dump -Fc` + SHA-256 + TOC | **21,553 bytes; 47 entradas TOC** |
| Restauración | PowerShell a base temporal | **0.444 s; conteos OK** |
| Restauración | Bash a base temporal | **0.446 s; conteos OK** |
| Dashboard | Smoke test con `AppTest` | **0 excepciones; 6 gráficos; 1 tabla** |
| Seguridad | Puertos publicados solo en `127.0.0.1` | **OK** |
| Seguridad | Socket de Docker montado | **Ninguno** |

---

## 2. Entorno de validación

| Elemento | Valor |
|---|---|
| Sistema operativo | Windows 11 (Docker Desktop con backend WSL2) |
| Docker Compose | v2 (`docker compose`) |
| Proyecto Compose | `clusterdb-monitor` |
| Contenedores | 10 |
| PostgreSQL | 16.4 (`postgres:16.4-bookworm`) |
| Patroni | 4.1.5 |
| etcd | `quay.io/coreos/etcd:v3.5.17` |
| HAProxy | `haproxy:2.9.9-alpine` |
| Python | 3.12 (`python:3.12-slim-bookworm`) |
| Streamlit / Plotly / pandas | 1.41.1 / 5.24.1 / 2.2.3 |
| Inicio de la ventana validada | `2026-11-06T11:05:55Z` |
| Failover provocado | `2026-11-06T11:06:17Z` |
| Última fotografía de KPIs | `2026-11-06T11:13:19Z` |

Topología final: `postgres-replica` es el primario y `postgres-master` es la
réplica en streaming. Los scripts **no asumen nombres fijos**: leen el rol real
desde Patroni.

---

## 3. Pruebas unitarias

### Comando

```powershell
.\tests\run-unit-tests.ps1
```

```bash
./tests/run-unit-tests.sh
```

Se ejecutan en un contenedor `python:3.12-slim-bookworm` aislado, con
`--network none` y el repositorio montado en solo lectura. No requieren
PostgreSQL ni Docker Compose levantado (solo Docker en ejecución).

### Resultado

```text
Ran 30 tests in 0.672s
OK
```

### Cobertura

| ID | Clase | Qué comprueba |
|---|---|---|
| U-01 | `AvailabilityTests` | Sin muestras devuelve `null` y un motivo. |
| U-02 | `AvailabilityTests` | Menos de 10 muestras → “Sin datos suficientes”. |
| U-03 | `AvailabilityTests` | Disponibilidad ponderada por tiempo, no por número de muestras. |
| U-04 | `AvailabilityTests` | Disponibilidad del 100 % sin caídas. |
| U-05 | `AvailabilityTests` | Hueco largo (reinicio) recortado a `max(10 s, 4 × sondeo)`. |
| U-06 | `AvailabilityTests` | Muestras desordenadas se ordenan antes de calcular. |
| U-07 | `IncidentMetricsTests` | Sin incidentes: MTTR y MTBF `null` con motivo. |
| U-08 | `IncidentMetricsTests` | MTTR es el promedio de duraciones resueltas. |
| U-09 | `IncidentMetricsTests` | Un incidente abierto cuenta como fallo pero no en MTTR. |
| U-10 | `IncidentMetricsTests` | MTBF exige al menos 1 minuto de observación. |
| U-11 | `IncidentMetricsTests` | MTBF usa tiempo operativo menos tiempo caído. |
| U-12 | `SlaTests` | Sin disponibilidad, SLA `null` y motivo. |
| U-13 | `SlaTests` | SLA cumplido y presupuesto restante positivo. |
| U-14 | `SlaTests` | SLA incumplido y presupuesto negativo. |
| U-15 | `RtoRpoTests` | RTO sin failover devuelve `null` y motivo. |
| U-16 | `RtoRpoTests` | RTO cumple y no cumple según el objetivo. |
| U-17 | `RtoRpoTests` | RPO sin verificación no inventa pérdida. |
| U-18 | `RtoRpoTests` | RPO con 0 registros perdidos. |
| U-19 | `RtoRpoTests` | Pérdida confirmada no inventa segundos. |
| U-20 | `RtoRpoTests` | El objetivo configurado no se reporta como pérdida. |
| U-21 | `StoreTestCase` | Esquema y consultas sobre base vacía. |
| U-22 | `StoreTestCase` | Inserción y lectura de la última muestra. |
| U-23 | `StoreTestCase` | Series por rango y agregación por bucket. |
| U-24 | `StoreTestCase` | Ciclo de vida de un incidente. |
| U-25 | `StoreTestCase` | Registro y actualización de failovers. |
| U-26 | `StoreTestCase` | Filtro de eventos por tipo y ventana. |
| U-27 | `StoreTestCase` | Idempotencia de eventos de transacción. |
| U-28 | `StoreTestCase` | Ingesta incremental de `attempts.jsonl`. |
| U-29 | `StoreTestCase` | Metadatos clave/valor. |
| U-30 | `StoreTestCase` | Purga de muestras antiguas sin borrar las recientes. |

Las pruebas de persistencia usan un directorio temporal y una base SQLite nueva
por prueba: **no tocan** el volumen `clusterdb-monitor-data`.

---

## 4. Validación de configuración

### 4.1 Docker Compose

```powershell
docker compose config --quiet
```

Resultado: **sin errores**. El archivo define 10 servicios, 2 redes y 8
volúmenes con nombre.

Comprobaciones adicionales:

| Comprobación | Resultado |
|---|---|
| Imágenes con versión fija (sin `latest`) | OK |
| `clusterdb-net` con `internal: true` | OK |
| Solo `haproxy` y `dashboard` en `clusterdb-edge` | OK |
| Puertos publicados solo en `127.0.0.1` | OK |
| Sin montaje de `/var/run/docker.sock` | OK |
| `restart: unless-stopped` en servicios de larga duración | OK |
| Healthchecks definidos | OK |
| Volúmenes persistentes con nombre | OK |

### 4.2 Sintaxis de scripts Bash

```bash
docker run --rm -v "$PWD/scripts:/scripts:ro" postgres:16.4-bookworm `
  bash -n /scripts/common.sh /scripts/init-env.sh /scripts/up.sh `
  /scripts/down.sh /scripts/status.sh /scripts/backup.sh `
  /scripts/restore-backup.sh /scripts/failover-test.sh /scripts/verify.sh
```

Resultado: **sin errores de sintaxis** en los 9 scripts.

---

## 5. Verificación integral

### Comando

```powershell
.\scripts\verify.ps1
```

```bash
./scripts/verify.sh
```

Es **no destructiva**: no detiene nodos ni restaura bases. Escribe un latido en
la tabla `ha_heartbeat` y lo busca en la réplica para verificar la replicación
activa. Opcionalmente ejecuta un respaldo con `-RunBackup` / `--backup`.

### Resultado de la última ejecución

| # | Comprobación | Resultado | Detalle medido |
|---|---|---|---|
| 1 | Contenedores y healthchecks | **OK** | 10/10 activos y saludables |
| 2 | Quórum etcd (3/3) | **OK** | 3 endpoints saludables |
| 3 | Topología Patroni | **OK** | `postgres-master=replica/running; postgres-replica=primary/running` |
| 4 | Replicación activa | **OK** | 6 comprobaciones en 0.038 s |
| 5 | API del monitor y muestras | **OK** | 212 muestras; disponibilidad 98.92 % en esa ventana |
| 6 | Respaldo verificado (SHA/TOC) | **OK** | `cluster_demo_20261106T110633Z.dump`, 21,553 bytes, 47 entradas TOC |
| 7 | Dashboard en `127.0.0.1:8501` | **OK** | HTTP 200 |
| 8 | HAProxy stats en `127.0.0.1:7000` | **OK** | HTTP 200 |
| 9 | PostgreSQL en `127.0.0.1:15432` | **OK** | El puerto acepta conexiones |
| 10 | Sin split-brain | **OK** | Un solo primario |

```text
Verificacion correcta: 10/10 comprobaciones OK.
```

La versión Bash se ejecutó después del failover y también devolvió **10/10**,
leyendo correctamente los roles invertidos.

---

## 6. Failover real

### Comando

```powershell
.\scripts\failover-test.ps1 -Yes
```

```bash
./scripts/failover-test.sh --yes
```

### Procedimiento ejecutado

1. El script leyó el primario real: `postgres-master`.
2. Ejecutó `docker compose stop postgres-master`.
3. Esperó a que Patroni promoviera `postgres-replica` y HAProxy entregara un
   primario escribible.
4. Esperó a que el monitor cerrara el incidente y calculara RTO/RPO.
5. Reinició `postgres-master` y comprobó que volviera como réplica en streaming.
6. Ejecutó la verificación activa de replicación.
7. Comprobó que exista **un solo primario**.

### Resultado medido

| Métrica | Valor |
|---|---|
| Primario anterior | `postgres-master` |
| Primario nuevo | `postgres-replica` |
| Detección de promoción por el script | 7.04 s (incluye el sondeo del script) |
| **Recuperación medida por el monitor (RTO)** | **4.704 s** |
| Objetivo RTO | 30 s |
| ¿Cumple RTO? | **Sí** |
| Pérdida confirmada (RPO) | **0 registros** |
| Fallos detectados | **1** |
| MTTR | **4.704 s** |
| Reincorporación del nodo caído | **4.42 s** |
| Estado final del nodo caído | `replica`, `running`, `streaming` |
| Lag final | **0 bytes** |
| Split-brain | **No** |
| Verificación activa | **6/6 comprobaciones OK** |

Salida resumida del monitor:

```text
Failover:        postgres-master -> postgres-replica
Recuperacion:    4.7 s
RTO objetivo:    30 s (cumple=True)
RPO observado:   0 registro(s) perdido(s)
Fallos totales:  1
Reincorporacion confirmada en 4.42 s.
Topologia final: un solo primario; no hay split-brain.
```

> El script mide 7.04 s porque su bucle consulta la API cada segundo y agrega el
> tiempo de `docker stop`. El valor autoritativo del RTO es el del monitor
> (**4.704 s**), que usa sus propias muestras cada 2 s.

### Eventos registrados

| Hora (UTC) | Evento | Descripción |
|---|---|---|
| 11:06:17 | `SERVICE_DOWN` | El servicio deja de estar disponible. |
| 11:06:22 | `RPO_VERIFIED` | 0 registros confirmados perdidos. |
| 11:06:22 | `SERVICE_RECOVERED` | Servicio disponible; nuevo primario. |
| 11:06:26 | `NODE_UP` | El nodo caído vuelve a estar activo como réplica. |
| 11:06:26 | `NODE_REJOINED` | Reincorporado bajo coordinación de Patroni. |
| 11:06:33 | `BACKUP_COMPLETED` | Respaldo verificado de 21,553 bytes. |

---

## 7. Respaldo y restauración

### 7.1 Respaldo

```powershell
.\scripts\backup.ps1 -Wait
```

Resultado:

| Campo | Valor |
|---|---|
| Archivo | `cluster_demo_20261106T110633Z.dump` |
| Formato | custom (`pg_dump -Fc`) |
| Tamaño | **21,553 bytes** |
| Duración | 0.145 s |
| SHA-256 | `f238a6a84c0750fc3be7d633226370ad01ef0556e7fce8457dc528aec36f61f1` |
| Verificación | **ok (47 entradas TOC)** |
| Estado | `success` |
| Destino | Volumen `clusterdb-backup-data` |

El respaldo se ejecutó contra HAProxy, no contra un nodo fijo. Por eso funcionó
después del failover, cuando el primario era `postgres-replica`.

### 7.2 Restauración controlada (PowerShell)

Se habilitó temporalmente `ALLOW_RESTORE=true` y se recrearon
`postgres-backup`, `monitor` y `dashboard`. Después se restauró el **último**
respaldo:

```powershell
.\scripts\restore-backup.ps1 -Latest -Yes
```

Resultado:

```text
Base temporal:  cluster_demo_restore_20261106_111221
Archivo:        cluster_demo_20261106T110633Z.dump
Duracion:       0.444 s
Conteos verificados:
    clientes: 20
    productos: 12
    ventas: 93
    detalle_ventas: 93
```

### 7.3 Restauración controlada (Bash)

```bash
./scripts/restore-backup.sh --latest --yes
```

Resultado:

```text
Base temporal:  cluster_demo_restore_20261106_111226
Archivo:        cluster_demo_20261106T110633Z.dump
Duracion:       0.446 s
Conteos verificados:
    clientes: 20
    productos: 12
    ventas: 93
    detalle_ventas: 93
```

### 7.4 Prueba de que la base activa no se sobrescribe

Consulta directa al primario después de las dos restauraciones:

```text
activa_ventas=269
cluster_demo_restore_20261106_111221
cluster_demo_restore_20261106_111226
temporal_ventas=93
```

- La base activa `cluster_demo` tenía **269 ventas** (el generador siguió
  escribiendo).
- La base temporal tenía **93 ventas**, las del momento del respaldo.
- Son bases distintas: la restauración **no** sobrescribió `cluster_demo`.

### 7.5 Restauración deshabilitada de nuevo

```powershell
docker compose up -d --no-deps --force-recreate postgres-backup monitor dashboard
```

Comprobación:

```text
backup_ALLOW_RESTORE=false
monitor_ALLOW_RESTORE=false
```

Estado final: la restauración vuelve a estar bloqueada por defecto.

---

## 8. Smoke test del dashboard

### Comando

```powershell
.\tests\run-dashboard-smoke.ps1
```

```bash
./tests/run-dashboard-smoke.sh
```

Ejecuta el `AppTest` oficial de Streamlit **dentro del contenedor `dashboard`**,
con acceso real a la API interna del monitor.

### Resultado

```text
excepciones=0
graficos_plotly=6
dataframes=1
errores_ui=0
DASHBOARD_SMOKE_OK
```

Comprueba que la aplicación renderiza las 6 gráficas Plotly, la tabla de eventos
y no lanza excepciones.

### Regresión encontrada y corregida

Durante la validación, el smoke test detectó un error real: la serie de
disponibilidad podía contener un bucket sin datos (`availability_pct=None`) y el
cálculo del rango del eje Y fallaba con:

```text
TypeError: '<' not supported between instances of 'NoneType' and 'float'
```

Se corrigió `render_charts` para filtrar los buckets sin datos antes de graficar
y calcular el rango. El smoke test se dejó automatizado para evitar que la
regresión vuelva a aparecer.

---

## 9. Métricas medidas por el monitor

Fotografía al **2026-11-06T11:13:19Z**, ventana “todo lo observado”, con el
failover dentro de la ventana:

| KPI | Valor medido | Interpretación |
|---|---|---|
| Muestras | 215 | Sondeo cada 2 s. |
| Tiempo observado | 441.7 s | Desde el reinicio limpio del monitor. |
| Disponibilidad | **98.9351 %** | 4.704 s de caída sobre 441.7 s. |
| Tiempo caído | **4.704 s** | El incidente del failover. |
| MTTR | **4.704 s** | Un incidente resuelto. |
| MTBF | **436.996 s** | Tiempo operativo entre fallos. |
| Fallos detectados | **1** | Resuelto, 0 abiertos. |
| RTO observado | **4.704 s** | Objetivo 30 s; cumple. |
| RPO observado | **0 registros** | Verificado en el nodo promovido. |
| SLA 99.9 % | **NO CUMPLE** | Margen −0.9649 pp en esta ventana. |
| Transacciones en ventana | 216 OK / 1 fallida | La fallida ocurre durante el cambio de nodo. |
| Transacciones acumuladas | 282 OK / 5 fallidas | Incluye reintentos y cortes de conexión. |

### Por qué el SLA no se cumple

No es un fallo del cálculo. En una ventana de 441.7 s, el presupuesto de caída
para 99.9 % es de solo **0.442 s**. Un failover real de 4.704 s consume ese
presupuesto varias veces. El SLA de 99.9 % es un objetivo de largo plazo
(≈ 43 minutos de caída al mes), no una meta alcanzable en una demo de minutos.

El dashboard muestra el margen real y la ventana usada. Al dejar correr el stack
sin nuevos fallos, la disponibilidad sube y el margen mejora gradualmente. **No
se reporta el 99.9 % configurado como si fuera una medición.**

---

## 10. Pruebas de seguridad y aislamiento

| ID | Comprobación | Comando / evidencia | Resultado |
|---|---|---|---|
| S-01 | Sin socket de Docker montado | Búsqueda de `docker.sock` y `/var/run` en `docker-compose.yml` | Sin coincidencias |
| S-02 | Red de datos interna | `clusterdb-net` con `internal: true` | OK |
| S-03 | Publicación solo local | `docker inspect` de puertos | `127.0.0.1:8501` únicamente en dashboard |
| S-04 | Servicios internos sin puertos | `monitor`, `backup`, `patroni`, `etcd` | Sin puertos publicados |
| S-05 | Secretos fuera de Git | `.env` en `.gitignore`; `.env.example` sin credenciales reales | OK |
| S-06 | Restauración bloqueada | `ALLOW_RESTORE=false` en ambos contenedores | OK |
| S-07 | Versiones fijas | 10 servicios con imagen versionada; ninguno `latest` | OK |
| S-08 | Contenedores no root donde aplica | `monitor`, `dashboard`, `backup`, `txgen` con usuario dedicado | OK |

---

## 11. Reproducción desde cero

```powershell
# 1. Detener y borrar todos los volúmenes (destructivo)
.\scripts\down.ps1 -Volumes -Yes

# 2. Construir y levantar
.\scripts\up.ps1

# 3. Esperar a que la réplica termine el clonado (unos 2 minutos)
.\scripts\status.ps1

# 4. Pruebas unitarias
.\tests\run-unit-tests.ps1

# 5. Verificación integral
.\scripts\verify.ps1

# 6. Smoke test del dashboard
.\tests\run-dashboard-smoke.ps1

# 7. Failover real
.\scripts\failover-test.ps1 -Yes

# 8. Respaldo verificado
.\scripts\backup.ps1 -Wait

# 9. Verificación final
.\scripts\verify.ps1
```

Para la restauración, habilitar temporalmente:

```powershell
$env:ALLOW_RESTORE = 'true'
docker compose up -d --no-deps --force-recreate postgres-backup monitor dashboard
.\scripts\restore-backup.ps1 -Latest -Yes
Remove-Item Env:\ALLOW_RESTORE
docker compose up -d --no-deps --force-recreate postgres-backup monitor dashboard
```

En Linux/WSL/Git Bash, usar los equivalentes `.sh`.

---

## 12. Limitaciones de las pruebas

- **Una sola máquina.** No se prueba la caída del host ni la pérdida de la
  laptop completa; no hay DR geográfico.
- **Ventanas cortas.** Disponibilidad, SLA y MTBF son sensibles al tiempo
  observado. Los valores de este documento no son una tasa de producción.
- **Sondeo cada 2 s.** Una caída más corta que el intervalo puede no medirse con
  precisión exacta; el RTO incluye la latencia del sondeo.
- **RPO medido, no garantizado.** La replicación síncrona es no estricta
  (`synchronous_mode_strict: false`). El resultado de 0 registros es el de esta
  prueba, no una garantía universal.
- **Respaldo lógico.** `pg_dump` no es PITR; el RPO del respaldo es el intervalo
  de respaldo (6 h por defecto), distinto del RPO de replicación.
- **Restauración a base temporal.** No sustituye un procedimiento productivo de
  recuperación; la base activa nunca se sobrescribe.
- **Dependencia de HAProxy.** El monitor mide el servicio a través de HAProxy; si
  HAProxy falla, se registra indisponibilidad aunque PostgreSQL siga sano. Es la
  definición de servicio del proyecto.
- **Sin TLS entre contenedores.** La red es local y aislada; no se prueba
  seguridad de red en tránsito.
- **Sin pruebas de carga.** El generador produce un volumen modesto de ventas;
  no se evalúa rendimiento bajo carga alta.

---

## 13. Conclusión

Las pruebas cubren los tres niveles que importan para la demo:

1. **Lógica y persistencia** (30 pruebas unitarias, aisladas y repetibles).
2. **Integración real** (10 comprobaciones sobre contenedores, etcd, Patroni,
   HAProxy, monitor, respaldos y puertos).
3. **Comportamiento ante fallos** (failover real, reincorporación, anti
   split-brain, respaldo y restauración).

El resultado principal es verificable y reproducible: el sistema se recuperó en
**4.704 s**, sin pérdida de transacciones confirmadas y sin split-brain, y el
nodo caído volvió como réplica en **4.42 s**.
