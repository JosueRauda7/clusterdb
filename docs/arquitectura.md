# Arquitectura de ClusterDB Monitor

Documento de referencia del diseño, los componentes y el flujo de failover.

---

## 1. Principios de diseño

1. **Failover real, no simulación.** La única forma de mover el primario es que
   Patroni lo decida con el quórum de etcd. El dashboard no puede cambiar roles.
2. **Un solo punto de conexión.** Aplicaciones, generador, respaldo y monitor
   usan HAProxy; nunca se conectan directamente a `postgres-master` o
   `postgres-replica`.
3. **Métricas independientes de la base monitoreada.** El monitor guarda sus
   datos en SQLite, en un volumen propio, para sobrevivir a la caída de
   PostgreSQL.
4. **Separación entre disponibilidad del servicio y estado de los nodos.** Una
   réplica puede caer sin que el servicio se caiga; un primario puede estar vivo
   pero HAProxy no entregarlo. Se miden por separado.
5. **Nada destructivo por defecto.** La restauración está bloqueada; el dashboard
   no detiene contenedores; no se monta el socket de Docker.
6. **Versiones fijas y red aislada.** Todo el stack funciona sin Internet una vez
   descargadas las imágenes.

---

## 2. Vista de componentes

| Componente | Tecnología | Responsabilidad |
|---|---|---|
| `etcd1/2/3` | etcd 3.5.17 | Almacén distribuido de configuración y líder de Patroni. Quórum de 3. |
| `postgres-master` | Patroni 4.1.5 + PostgreSQL 16.4 | Nodo con bootstrap habilitado. Puede ser primario o réplica según Patroni. |
| `postgres-replica` | Patroni 4.1.5 + PostgreSQL 16.4 | Nodo sin bootstrap. Se clona con `basebackup` y sigue al primario. |
| `haproxy` | HAProxy 2.9.9 | Enruta TCP 5432 solo al nodo que responde `200` en `/primary`. |
| `postgres-backup` | Python + `pg_dump`/`pg_restore` | Respaldos lógicos, verificación SHA-256/TOC, retención y restauración temporal. |
| `monitor` | FastAPI + psycopg2 + SQLite | Sondeo cada 2 s, detección de incidentes, KPIs y API interna. |
| `txgen` | Python + psycopg2 | Genera ventas idempotentes y registra intentos en JSONL. |
| `dashboard` | Streamlit + Plotly + pandas | Presentación de estado, KPIs, gráficos, eventos y laboratorio. |

---

## 3. Redes y puertos

### `clusterdb-net` (interna)

- `driver: bridge`, `internal: true`.
- Conecta a los 10 servicios.
- No tiene salida a Internet: en la presentación no se depende de la red.
- Los nombres DNS (`etcd1`, `postgres-master`, `haproxy`, etc.) se resuelven por
  el DNS interno de Docker.

### `clusterdb-edge` (solo publicación)

- `driver: bridge`.
- Solo se conectan `haproxy` y `dashboard`.
- Existe porque Docker Desktop **no publica puertos** de contenedores conectados
  únicamente a una red `internal: true`. Se comprobó con un contenedor de prueba.
- No da acceso a PostgreSQL, etcd, monitor ni respaldos.

### Puertos publicados

| Host (`127.0.0.1`) | Contenedor | Servicio |
|---|---|---|
| `15432` | `haproxy:5432` | PostgreSQL por HAProxy |
| `7000` | `haproxy:7000` | Estadísticas de HAProxy |
| `8501` | `dashboard:8501` | Dashboard |

Los puertos del monitor (`8000`), del respaldo (`8080`), de Patroni (`8008`) y de
etcd (`2379`/`2380`) **no** se publican al host.

---

## 4. Patroni y etcd

### Configuración principal

| Parámetro | Valor | Motivo |
|---|---|---|
| `ttl` | `20` | Detecta la caída del líder en decenas de segundos, adecuado para demo. |
| `loop_wait` | `5` | Frecuencia de comprobación de Patroni. |
| `retry_timeout` | `5` | Reintentos antes de declarar fallo. |
| `synchronous_mode` | `true` | Un standby síncrono confirma las transacciones. |
| `synchronous_mode_strict` | `false` | Permite promover si no hay standby síncrono; evita bloqueo total. |
| `maximum_lag_on_failover` | `1048576` (1 MB) | Evita promover una réplica demasiado atrasada. |
| `use_pg_rewind` | `true` | Reincorpora al antiguo primario sin reconstruir todo. |
| `use_slots` | `true` | Slots de replicación para conservar WAL. |
| `remove_data_directory_on_rewind_failure` | `true` | Si `pg_rewind` falla, reconstruye desde el primario. |
| `wal_level` | `replica` | Necesario para replicación. |
| `wal_log_hints` | `on` | Requerido por `pg_rewind`. |
| `hot_standby` | `on` | Consultas en réplica. |

La configuración se renderiza desde `docker/patroni/patroni.yml.tmpl` con
`render_config.py`.

### Bootstrap controlado

Solo `postgres-master` define `PATRONI_BOOTSTRAP_ENABLED=true`. El renderizador
**elimina la sección `bootstrap`** en la réplica. Así se evita que la réplica gane
la primera elección y que los nombres queden invertidos en el primer arranque.

El bootstrap ejecuta `bootstrap-db.sh`, que crea:

- Roles `app`, `backup`, `monitor` y `replicator` con contraseñas del entorno.
- Esquema `clientes`, `productos`, `ventas`, `detalle_ventas`, `ha_heartbeat`.
- Datos de demostración y una función/trigger para mantener totales.
- Permisos mínimos: `app` escribe ventas; `backup` y `monitor` leen lo necesario.

### Quórum

- 3 miembros de etcd ⇒ tolera la caída de **1** sin perder quórum.
- Si se caen 2, Patroni no puede renovar el liderazgo; PostgreSQL deja de aceptar
  escrituras coordinadas. Es el comportamiento deseado para evitar split-brain.
- Los datos de etcd persisten en `clusterdb-etcd1-data`, `clusterdb-etcd2-data` y
  `clusterdb-etcd3-data`.

---

## 5. HAProxy: un solo punto de entrada

```text
frontend postgres_frontend  (bind *:5432)
        │
        ▼
backend postgres_primary
  server postgres-master  postgres-master:5432  check port 8008
  server postgres-replica postgres-replica:5432  check port 8008
  option httpchk GET /primary
  http-check expect status 200
```

- El chequeo **no** es un simple TCP check: consulta la API REST de Patroni en el
  puerto `8008`.
- Solo el nodo que responde `200` en `/primary` recibe tráfico.
- `on-marked-down shutdown-sessions` cierra conexiones al nodo que deja de ser
  primario.
- `resolvers docker_dns` usa `127.0.0.11` para no quedarse con una IP vieja si un
  contenedor se recrea.
- `fall 2`, `rise 2`, `inter 2s` implican una detección de segundos.

**Consecuencia:** cuando Patroni promueve la réplica, HAProxy deja de enviar
tráfico al antiguo primario y empieza a enviarlo al nuevo. La aplicación no
cambia la cadena de conexión.

---

## 6. Flujo de failover

1. Se detiene el contenedor del primario (`docker compose stop`).
2. Patroni del primario deja de renovar su clave en etcd.
3. Pasado el `ttl`, el quórum elige al standby con mayor prioridad y menos lag.
4. El standby se promueve (`pg_promote`), incrementa el timeline y empieza a
   aceptar escrituras.
5. La API `/primary` del nuevo primario responde `200`; HAProxy lo marca `UP` y
   envía las conexiones nuevas.
6. El monitor detecta la indisponibilidad y luego la recuperación, cierra el
   incidente y registra RTO, MTTR y RPO.
7. Al reiniciar el nodo caído, Patroni lo arranca como réplica:
   - Si es viable, `pg_rewind` lo reconcilia con el nuevo primario.
   - Si no, se reconstruye con `basebackup`.
8. El nodo vuelve a `streaming` y aparece en `pg_stat_replication`.
9. La verificación activa escribe un latido y comprueba que exista en ambos
   nodos; además, comprueba que haya un solo primario.

### Protección contra split-brain

- El liderazgo se decide por quórum en etcd, no por tiempo local.
- El antiguo primario, al no poder renovar el liderazgo, se degrada y deja de
  aceptar escrituras.
- HAProxy solo enruta al nodo con `/primary` en `200`.
- La verificación final consulta ambos nodos y falla si hay dos primarios.

---

## 7. Monitor

- Sondeo cada `MONITOR_POLL_SECONDS` (2 s).
- **Disponibilidad del servicio:** abre una conexión a `haproxy:5432` con el
  usuario `monitor`, ejecuta una consulta de escritura/lectura y exige que el nodo
  no esté en recuperación. Si falla, registra indisponibilidad.
- **Estado de nodos:** consulta `http://postgres-master:8008/patroni` y
  `http://postgres-replica:8008/patroni` para rol, estado, timeline y lag.
- **Incidentes:** con `FAILURE_THRESHOLD_SAMPLES=1` porque un failover real puede
  durar menos que dos sondeos. Cada incidente guarda inicio, fin, duración, nodo
  caído, nuevo primario y datos de RPO.
- **RPO observado:** antes del failover se conoce el último `ha_heartbeat`
  confirmado; tras la promoción se comprueba si ese registro existe en el nuevo
  primario. Si existe, la pérdida medida es 0 registros. Si no, se marca pérdida
  confirmada y **no se inventa** una ventana en segundos.
- **Transacciones:** el monitor lee `attempts.jsonl` del generador de forma
  incremental (offset en SQLite) y calcula tasas de éxito/fallo.
- **Persistencia:** `/data/monitor.db` en el volumen `clusterdb-monitor-data`,
  independiente de PostgreSQL. Retención de 30 días para muestras.
- **API interna:** `/health`, `/api/status`, `/api/kpis`, `/api/timeseries`,
  `/api/events`, `/api/backups`, `/api/actions/verify-replication`,
  `/api/actions/backup`, `/api/actions/restore`.

---

## 8. Respaldo y restauración

- `postgres-backup` usa `pg_dump -Fc` contra HAProxy (primario actual).
- Guarda en `clusterdb-backup-data` (`/backups`).
- Calcula SHA-256 y ejecuta `pg_restore --list` para validar el TOC.
- Retención por días (`7`) y por cantidad (`14`).
- `BACKUP_ON_START=true` genera un respaldo al arrancar, tras 25 s.
- La restauración:
  - Requiere `ALLOW_RESTORE=true` en `postgres-backup` **y** en `monitor`.
  - Crea `cluster_demo_restore_YYYYMMDD_HHMMSS`.
  - Verifica conteos de tablas.
  - Nunca escribe en `cluster_demo`.
  - Conserva las 2 bases temporales más recientes.

El respaldo es **independiente de la replicación**: si el primario cambia, el
servicio sigue apuntando a HAProxy y respalda al nuevo primario.

---

## 9. Dashboard

- Streamlit `1.41.1`, Plotly `5.24.1`, pandas `2.2.3`.
- Refresco automático cada `DASHBOARD_REFRESH_SECONDS` (5 s) con
  `st.fragment(run_every=...)`.
- Consume solo la API del monitor; no habla con PostgreSQL ni con Docker.
- Tema oscuro y textos en español.
- Tarjetas de estado, 9 KPIs, 6 gráficos, tabla de eventos y laboratorio.
- Botones seguros: verificar replicación, solicitar respaldo y restaurar (este
  último deshabilitado salvo `ALLOW_RESTORE=true` y confirmación explícita).
- **No** hay botones para detener o iniciar nodos.

---

## 10. Volúmenes

| Volumen | Contenido | Sobrevive a `down` | Se borra con `down -v` |
|---|---|---|---|
| `clusterdb-etcd1-data` | Datos de etcd1 | Sí | Sí |
| `clusterdb-etcd2-data` | Datos de etcd2 | Sí | Sí |
| `clusterdb-etcd3-data` | Datos de etcd3 | Sí | Sí |
| `clusterdb-pg-master-data` | PGDATA del nodo master | Sí | Sí |
| `clusterdb-pg-replica-data` | PGDATA del nodo replica | Sí | Sí |
| `clusterdb-backup-data` | Archivos `.dump` | Sí | Sí |
| `clusterdb-monitor-data` | `monitor.db` (SQLite) | Sí | Sí |
| `clusterdb-generator-data` | `stats.json` y `attempts.jsonl` | Sí | Sí |

---

## 11. Decisiones y compromisos

| Decisión | Motivo | Compromiso |
|---|---|---|
| Solo `postgres-master` bootstrapea | Evitar inversión de roles en el primer arranque | Si se borra el volumen del master, el clúster se reconstruye desde cero. |
| Red interna + `clusterdb-edge` | Aislamiento y publicación de puertos en Docker Desktop | Solo dos servicios están en el segmento de publicación. |
| Monitoreo por HAProxy | Mide el servicio tal como lo ve la aplicación | Una caída de HAProxy cuenta como indisponibilidad. |
| SQLite para métricas | Independencia total de PostgreSQL | No es un TSDB distribuido; es suficiente para una demo local. |
| `synchronous_mode_strict: false` | Evitar bloqueo total si no hay standby | El RPO 0 es el resultado medido, no una garantía. |
| `FAILURE_THRESHOLD_SAMPLES=1` | Un failover puede durar < 2 s | Puede registrar incidentes muy breves. |
| Restauración solo a base temporal | Proteger la base activa | No sustituye un procedimiento de recuperación productivo. |

---

## 12. Limitaciones de alcance

- Una sola laptop; no hay redundancia de hardware ni de centro de datos.
- No hay PITR ni archivado de WAL a almacenamiento externo.
- No hay autenticación TLS entre contenedores; la red es local y aislada.
- No hay balanceo de lectura: HAProxy solo enruta al primario.
- El dashboard no está pensado para múltiples usuarios ni para exposición
  pública.
