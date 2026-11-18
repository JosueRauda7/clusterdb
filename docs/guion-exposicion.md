# Guion de exposición — ClusterDB Monitor

Guion para una exposición de **12 a 15 minutos** con demostración real. Incluye
lista de verificación sin Internet, comandos exactos, qué decir en cada momento y
plan de contingencia si algo falla en vivo.

> Regla de oro: **no se inventan métricas**. Si un KPI no tiene datos, se muestra
> “Sin datos suficientes” y se explica por qué. Los objetivos (SLA 99.9 %, RTO
> 30 s, RPO 60 s) son metas, no resultados.

---

## 1. Antes de la exposición

### La noche anterior (con Internet)

```powershell
docker compose build
docker compose pull
.\tests\run-unit-tests.ps1          # deben pasar 30 pruebas
docker compose down                 # conserva volúmenes
```

Dejar descargadas todas las imágenes. El plano de datos es `internal: true`, así
que la demo no necesita Internet.

### 30 minutos antes (sin Internet)

```powershell
cd clusterdb-monitor
docker compose up -d                # NO usar --pull; debe arrancar con lo descargado
.\scripts\up.ps1 -NoBuild -Offline  # alternativa: espera healthchecks y verifica puertos
.\scripts\verify.ps1
```

Comprobar:

- [ ] Los **10 contenedores** están `healthy`.
- [ ] `http://127.0.0.1:8501` abre el dashboard.
- [ ] `http://127.0.0.1:7000` abre las estadísticas de HAProxy.
- [ ] El primario aparece en la tarjeta **Nodo primario**.
- [ ] La tarjeta **Replicación** dice `streaming` con lag bajo.
- [ ] Existe al menos un respaldo verificado.
- [ ] El generador de transacciones está `running`.
- [ ] El navegador está en pantalla completa, zoom 100 %, tema oscuro.

### Decisión importante antes de empezar

Si se va a hacer el failover en vivo, **reiniciar el contador de métricas** para
que el público vea el incidente completo:

```powershell
docker compose stop monitor
docker run --rm -v clusterdb-monitor-data:/data postgres:16.4-bookworm `
  sh -c "rm -f /data/monitor.db /data/monitor.db-wal /data/monitor.db-shm"
docker compose start monitor
```

Esperar 1–2 minutos a que se acumulen muestras y recién entonces comenzar. Así la
ventana “Todo lo observado” empieza limpia y el failover se ve como un cambio
claro.

> Si no se quiere borrar el historial, no hacerlo. Las métricas anteriores siguen
> siendo válidas; solo hay que explicar la ventana seleccionada.

### Pestañas sugeridas

1. Dashboard: `http://127.0.0.1:8501`
2. HAProxy stats: `http://127.0.0.1:7000`
3. Terminal en la carpeta del proyecto (PowerShell)
4. Opcional: `docs/kpis.md` abierto para consultar fórmulas

---

## 2. Distribución del tiempo

| Minutos | Bloque | Idea central |
|---|---|---|
| 0:00–1:30 | Problema y objetivo | Qué significa alta disponibilidad y qué se va a demostrar. |
| 1:30–4:00 | Arquitectura | Patroni, etcd, HAProxy y el rol de cada contenedor. |
| 4:00–6:00 | Dashboard | Estado real, 9 KPIs, ventanas y “sin datos suficientes”. |
| 6:00–9:30 | Failover real | Detener el primario, ver la promoción y la recuperación. |
| 9:30–11:30 | Reincorporación y anti split-brain | El nodo caído vuelve como réplica; verificación activa. |
| 11:30–13:00 | Respaldos | `pg_dump`, SHA-256, TOC y restauración a base temporal. |
| 13:00–15:00 | Cierre | Pruebas, limitaciones y conclusiones. |

Versión corta de **7 minutos**: diapositivas 1 y 2 en 2 minutos, dashboard 1
minuto, failover 2 minutos, reincorporación 1 minuto, cierre 1 minuto.

---

## 3. Guion detallado

### 0:00–1:30 — Problema y objetivo

> “Buenos días. Este proyecto es **ClusterDB Monitor**, un laboratorio local de
> alta disponibilidad para PostgreSQL. El problema es simple: si la base de datos
> se cae, la aplicación deja de vender. La pregunta es cuánto tarda el sistema en
> volver y cuántos datos se pierden.
>
> El objetivo no es solo levantar dos servidores: es demostrar un **failover
> real**, con quórum, promoción automática, un único punto de conexión y métricas
> medidas, no simuladas. Todo corre en una laptop con Windows 11 y Docker
> Desktop, sin depender de Internet durante la presentación.”

Mencionar que el stack completo son **10 contenedores** y que las versiones están
fijadas.

### 1:30–4:00 — Arquitectura

Apoyarse en `docs/arquitectura.md`. Recorrer de izquierda a derecha:

> “Hay tres piezas que sostienen la disponibilidad.
>
> **Primero, etcd.** Son tres miembros. Guardan la configuración y deciden quién
> es el líder. Con tres, se tolera la caída de uno; si se caen dos, se pierde el
> quórum y el sistema deja de coordinar. Eso es intencional: preferimos dejar de
> escribir antes que tener dos primarios.
>
> **Segundo, Patroni.** Corre junto a PostgreSQL en cada nodo. Si el líder deja de
> renovar su clave en etcd, Patroni promueve la réplica con más prioridad y menos
> retraso. También reincorpora al nodo caído con `pg_rewind`.
>
> **Tercero, HAProxy.** Es el único punto de conexión para la aplicación. No hace
> un chequeo TCP simple: consulta `GET /primary` en la API de Patroni. Solo el
> nodo que responde 200 recibe tráfico. Cuando cambia el primario, la aplicación
> no cambia su cadena de conexión.
>
> Alrededor están el **monitor** en FastAPI con SQLite, el **generador** de
> transacciones idempotentes, el servicio de **respaldos** con `pg_dump` y el
> **dashboard** en Streamlit. El monitor guarda sus métricas en un volumen propio:
> si PostgreSQL se cae, las métricas sobreviven.”

Si preguntan por la red: `clusterdb-net` es interna; solo HAProxy y dashboard
están además en `clusterdb-edge` para publicar `127.0.0.1:15432`, `:7000` y
`:8501`.

### 4:00–6:00 — Dashboard

Abrir `http://127.0.0.1:8501`. Señalar en orden:

1. **Estado del clúster**: servicio disponible, nodo primario, nodo réplica,
   replicación, respaldos y generador.
2. **KPIs**: disponibilidad, SLA, MTTR, MTBF, RTO, RPO, último failover, fallos y
   transacciones.
3. **Gráficos**: disponibilidad, latencia, lag, transacciones, RTO de failovers y
   tamaño de respaldos.
4. **Historial de eventos** y **laboratorio de fallos**.

Guion sugerido:

> “La disponibilidad se calcula **ponderada por tiempo**, no como muestras OK
> sobre total. Cada muestra cubre el intervalo hasta la siguiente. Si el monitor
> estuvo apagado, ese hueco no se cuenta.
>
> Aquí hay una decisión de diseño importante: el SLA del 99.9 % es un objetivo.
> En una ventana de pocos minutos, un failover de 5 segundos ya agota el
> presupuesto. El dashboard no lo esconde: muestra el margen real. Y cuando no
> hay datos suficientes, dice **‘Sin datos suficientes’** en lugar de mostrar
> cero o el objetivo como si fuera una medición.
>
> El RPO se reporta como **pérdida verificada**: antes del failover se guarda el
> último latido confirmado y después se busca en el nodo promovido. Si está, la
> pérdida es cero registros. Si no está, se reporta pérdida confirmada, sin
> inventar una cantidad de segundos.”

Cambiar la ventana a **“Todo lo observado”** y explicar que la disponibilidad y
el SLA dependen de la ventana.

### 6:00–9:30 — Failover real

Usar el script, que hace todo y deja evidencia:

```powershell
.\scripts\failover-test.ps1 -Yes
```

Si se prefiere hacerlo manual para que el público vea cada paso:

```powershell
# 1. Ver el primario actual (no asumir que es postgres-master)
docker compose exec postgres-replica patronictl -c /etc/patroni/patroni.yml list

# 2. Detener el primario actual (reemplace PRIMARIO_ACTUAL por el nombre real)
docker compose stop PRIMARIO_ACTUAL

# 3. Ver la promoción y el timeline
docker compose exec NUEVO_PRIMARIO patronictl -c /etc/patroni/patroni.yml list

# 4. Ver HAProxy: el nodo caído debe estar DOWN y el nuevo UP
#    http://127.0.0.1:7000
```

Narración:

> “Detenemos el primario de forma abrupta. Patroni deja de renovar el liderazgo en
> etcd. Pasado el TTL, el quórum elige a la réplica. La réplica se promueve,
> incrementa el timeline y empieza a aceptar escrituras. HAProxy consulta
> `/primary`, ve el 200 en el nuevo nodo y redirige las conexiones.
>
> El monitor, que sondea cada 2 segundos, detecta la caída, abre el incidente y
> lo cierra cuando vuelve a haber un primario escribible. Con eso calcula el RTO
> observado, el MTTR y verifica el RPO.”

Resultado esperado (los valores pueden variar):

- Recuperación medida por el monitor: **~4–8 s**.
- Objetivo RTO: **30 s** → cumple.
- RPO: **0 registros perdidos** verificado.
- Fallos detectados: **1**.

Volver al dashboard y mostrar:

- Tarjeta **Nodo primario** con el nuevo nodo.
- **Último failover** con hora y recuperación.
- **Historial de eventos** con `SERVICE_DOWN`, `SERVICE_RECOVERED`,
  `RPO_VERIFIED`, `NODE_REJOINED`.
- Gráfico de **RTO** con la barra del failover.

### 9:30–11:30 — Reincorporación y anti split-brain

Si se usó el script, este paso ya ocurrió. Si se hizo manual:

```powershell
docker compose start NODO_CAIDO
docker compose exec NUEVO_PRIMARIO patronictl -c /etc/patroni/patroni.yml list
```

Narración:

> “El nodo que se cayó vuelve a arrancar, pero **no vuelve como primario**.
> Patroni lo detecta, comprueba el timeline y lo reincorpora como réplica.
> `pg_rewind` reconcilia los cambios; si no es viable, se reconstruye con
> `basebackup`. Al final hay un solo primario y una réplica en streaming.
>
> La protección contra split-brain es doble: el liderazgo se decide por quórum en
> etcd, no por un reloj local, y HAProxy solo entrega el nodo que responde
> `/primary`. Además, la verificación activa escribe un latido y comprueba que
> exista en ambos nodos, y falla si encuentra dos primarios.”

Mostrar la verificación segura desde el dashboard (**Laboratorio de fallos →
Verificar replicación**) o por CLI:

```powershell
.\scripts\verify.ps1
```

Debe dar **10/10 comprobaciones OK**, incluyendo “sin split-brain” y “replicación
activa”.

### 11:30–13:00 — Respaldos

```powershell
.\scripts\backup.ps1 -Wait
```

Narración:

> “El respaldo es **independiente de la replicación**. El servicio `postgres-backup`
> se conecta por HAProxy al primario actual y ejecuta `pg_dump` en formato custom.
> No copia archivos del nodo: hace un respaldo lógico. Después calcula SHA-256 y
> ejecuta `pg_restore --list` para validar el TOC. La retención es por días y por
> cantidad.
>
> La restauración está **deshabilitada por defecto**. Cuando se habilita, solo
> crea una base temporal `cluster_demo_restore_*`, verifica los conteos y nunca
> sobrescribe `cluster_demo`. Para una recuperación productiva haría falta un
> procedimiento adicional; este proyecto lo declara como limitación.”

Si hay tiempo y se habilitó `ALLOW_RESTORE=true` antes de la demo:

```powershell
.\scripts\restore-backup.ps1 -Latest -Yes
```

Mostrar la base temporal y los conteos. Aclarar que la base activa no se tocó.

### 13:00–15:00 — Cierre

> “Resumiendo: el proyecto demuestra un failover real de PostgreSQL en una
> laptop, con quórum de etcd, promoción automática, un único punto de conexión,
> reincorporación sin split-brain, respaldos verificados y un dashboard que mide
> lo que realmente pasó.
>
> Las pruebas incluyen **30 pruebas unitarias** de los cálculos y la persistencia,
> una **verificación integral de 10 comprobaciones**, un failover real medido y
> una restauración a base temporal. Los resultados están en `docs/pruebas.md`.
>
> Las limitaciones son claras: es un laboratorio local, no producción; no hay
> PITR; la replicación síncrona es no estricta; y el SLA es un objetivo de largo
> plazo que en ventanas cortas puede no cumplirse. Preferimos mostrar eso antes
> que maquillar las métricas.”

---

## 4. Comandos de referencia rápida

| Acción | Comando |
|---|---|
| Estado completo y KPIs | `.\scripts\status.ps1` |
| Verificación integral | `.\scripts\verify.ps1` |
| Failover real + reincorporación | `.\scripts\failover-test.ps1 -Yes` |
| Respaldo verificado | `.\scripts\backup.ps1 -Wait` |
| Restauración a base temporal | `.\scripts\restore-backup.ps1 -Latest -Yes` |
| Pruebas unitarias | `.\tests\run-unit-tests.ps1` |
| Detener sin borrar datos | `.\scripts\down.ps1` |
| Reinicio desde cero | `.\scripts\down.ps1 -Volumes -Yes` y `.\scripts\up.ps1` |
| Logs de un nodo | `docker compose logs --tail=100 postgres-master` |
| Estado de Patroni | `docker compose exec NODO patronictl -c /etc/patroni/patroni.yml list` |

En Linux/WSL/Git Bash, sustituir `.\scripts\x.ps1` por `./scripts/x.sh` y
`.\tests\run-unit-tests.ps1` por `./tests/run-unit-tests.sh`.

---

## 5. Plan de contingencia

### El failover no ocurre en 60–90 segundos

1. No entrar en pánico ni reiniciar todo.
2. Revisar el quórum:

   ```powershell
   docker compose exec etcd1 etcdctl --endpoints=http://etcd1:2379,http://etcd2:2379,http://etcd3:2379 endpoint health --cluster
   ```

3. Revisar los logs:

   ```powershell
   docker compose logs --tail=100 NODO_CAIDO
   docker compose logs --tail=100 REPLICA
   ```

4. Reiniciar el nodo caído para restaurar el servicio:

   ```powershell
   docker compose start NODO_CAIDO
   ```

5. Si no se recupera, reiniciar el stack conservando datos:

   ```powershell
   docker compose down
   docker compose up -d
   ```

6. Si aún falla, usar la **evidencia ya medida** en `README.md` y
   `docs/pruebas.md`: failover `postgres-master → postgres-replica`, RTO 4.7 s,
   RPO 0, reincorporación 4.4 s. Explicar el diseño y continuar con las
   diapositivas. Nunca inventar una demostración en vivo.

### El dashboard no abre

1. Comprobar el contenedor:

   ```powershell
   docker compose ps dashboard
   docker compose logs --tail=100 dashboard
   ```

2. Reiniciar solo el dashboard:

   ```powershell
   docker compose up -d --no-deps dashboard
   ```

3. Mientras tanto, usar `.\scripts\status.ps1` y `http://127.0.0.1:7000`. La API
   del monitor sigue disponible dentro de la red.

4. Si el problema es el puerto, verificar que `8501` esté libre:

   ```powershell
   Get-NetTCPConnection -LocalPort 8501 -ErrorAction SilentlyContinue
   ```

### Un contenedor aparece `unhealthy`

```powershell
docker compose ps
docker compose logs --tail=100 SERVICIO
docker compose restart SERVICIO
```

Si es PostgreSQL y no vuelve, revisar `patronictl list` y los logs de Patroni
antes de tomar decisiones. No borrar volúmenes salvo que se acepte perder datos.

### No hay Internet en el lugar

El diseño ya es offline. **No ejecutar** `docker compose pull` ni `up.ps1` con
`--build` si las imágenes no están construidas. Usar:

```powershell
docker compose up -d --pull never
```

Si faltan imágenes, no hay forma de descargarlas sin red: usar la evidencia
documentada y las capturas/diapositivas.

### La laptop va lenta

- Cerrar navegadores, Teams, Slack y cualquier VM que no se use.
- No ejecutar el failover y el respaldo al mismo tiempo.
- Bajar `DASHBOARD_REFRESH_SECONDS` no ayuda; dejarlo en 5 s.
- Si es necesario, usar la ventana “15 minutos” para que los gráficos tengan
  menos puntos.

### Pregunta difícil: “¿esto es real o simulado?”

Respuesta:

> “El dashboard no puede cambiar roles. El único mecanismo es detener el
> primario y dejar que Patroni decida con el quórum de etcd. HAProxy consulta la
> API real de Patroni. Las métricas salen de muestras guardadas en SQLite. Puedo
> mostrar los logs y `patronictl list` ahora mismo.”

### Pregunta: “¿por qué el SLA no se cumple?”

> “Porque la ventana es de minutos y el failover consume el presupuesto de
> 99.9 %. El SLA de 99.9 % permite unos 43 minutos de caída al mes; en una
> ventana de 5 minutos el presupuesto es de menos de un segundo. Es un objetivo
> de largo plazo, no una métrica que se pueda cumplir en una demo corta. El
> dashboard muestra la ventana y el margen real.”

### Pregunta: “¿qué pasa si se caen dos etcd?”

> “Se pierde el quórum. Patroni no puede renovar el liderazgo y PostgreSQL deja
> de aceptar escrituras coordinadas. Es el comportamiento correcto: se prefiere
> indisponibilidad temporal antes que dos primarios escribiendo. Con 3 miembros
> se tolera exactamente 1 fallo.”

### Pregunta: “¿por qué no hay PITR?”

> “Es una limitación declarada. El respaldo es lógico con `pg_dump`, que permite
> recuperar a un punto en el tiempo solo hasta el momento del respaldo. Para PITR
> haría falta archivado continuo de WAL a almacenamiento externo, fuera del
> alcance de este laboratorio.”

---

## 6. Cierre en una frase

> “ClusterDB Monitor no solo levanta PostgreSQL en Docker: demuestra que el
> sistema **se recupera solo**, que el nodo caído **vuelve como réplica** y que
> las métricas que se muestran **se midieron de verdad**.”
