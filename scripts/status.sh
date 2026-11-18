#!/usr/bin/env bash
# Muestra el estado real del cluster, los KPIs acumulados y Patroni.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_docker

header 'Contenedores'
compose ps

header 'Servicio, nodos y KPIs (datos reales)'
if ! docker exec -i "$MONITOR_CONTAINER" python - <<'PY'
import json
import urllib.request

base = "http://127.0.0.1:8000"


def get(path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.load(response)


def fmt_seconds(value):
    if value is None:
        return "Sin datos suficientes"
    seconds = float(value)
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


status = get("/api/status")
kpis = get("/api/kpis?window_minutes=0")

service = status["service"]
available = service.get("available")
print(f"Servicio HAProxy:     {'DISPONIBLE' if available is True else 'NO DISPONIBLE' if available is False else 'SIN DATOS'}")
print(f"Primario actual:      {service.get('primary_name')}")
print(f"Latencia:             {service.get('latency_ms')} ms")
print(f"Ultima muestra:       {service.get('last_sample_iso')} (hace {service.get('sample_age_seconds'):.1f} s)")
print("")
for node in status.get("nodes", []):
    print(f"  {node.get('name'):<18} rol={node.get('role'):<8} estado={node.get('state'):<8} lag={node.get('lag_bytes')}")

replication = status.get("replication", {})
print("")
print(f"Replicacion:          {replication.get('state')}")
print(f"Lag:                  {replication.get('lag_bytes')} bytes")
print(f"Sync standby:         {replication.get('sync_state')}")

availability = kpis["availability"]
print("")
print("KPIs acumulados (datos reales)")
if availability.get("pct") is not None:
    print(f"Disponibilidad:       {availability['pct']:.2f}%  ({availability['observed_seconds']:.0f} s observados, {availability['downtime_seconds']:.1f} s de caida)")
else:
    print(f"Disponibilidad:       {availability.get('reason')}")

sla = kpis["sla"]
if sla.get("met") is not None:
    verdict = "CUMPLE" if sla["met"] else "NO CUMPLE"
    print(f"SLA {sla['target_pct']:.1f}%:            {verdict}  (margen {sla['gap_pp']:+.3f} pp)")
else:
    print(f"SLA:                  {sla.get('reason')}")

print(f"MTTR:                 {fmt_seconds(kpis['mttr']['seconds'])}")
print(f"MTBF:                 {fmt_seconds(kpis['mtbf']['seconds'])}")
failures = kpis["failures"]
print(f"Fallos detectados:    {failures['detected']} (resueltos {failures['resolved']}, abiertos {failures['open']})")

rto = kpis["rto"]
if rto.get("actual_seconds") is not None:
    print(f"RTO observado:        {fmt_seconds(rto['actual_seconds'])}  (objetivo {rto['target_seconds']:.0f} s, cumple={rto['met']})")
else:
    print(f"RTO observado:        {rto.get('reason')}")

rpo = kpis["rpo"]
if rpo.get("verified"):
    print(f"RPO observado:        {rpo['lost_records']} registro(s) perdido(s)  (objetivo {rpo['target_seconds']:.0f} s)")
else:
    print(f"RPO observado:        {rpo.get('reason')}")

last = kpis.get("last_failover")
if last:
    print(f"Ultimo failover:      {last.get('old_primary')} -> {last.get('new_primary')} a las {last.get('detected_iso')}")
else:
    print("Ultimo failover:      Sin failovers registrados.")

tx = kpis["transactions"]
print(f"Transacciones:        ventana OK={tx['window_ok']} fallidas={tx['window_failed']}; acumulado OK={tx['total_ok']} fallidas={tx['total_failed']}")

print("")
print("Respaldos")
backup = status["backup"]
if backup.get("reachable"):
    last_backup = backup.get("last_backup")
    if last_backup:
        print(f"Ultimo respaldo:      {last_backup.get('filename')} ({last_backup.get('status')})")
        print(f"Tamano:               {last_backup.get('size_bytes')} bytes")
        print(f"Verificacion:         {last_backup.get('verification')}")
        print(f"SHA-256:              {last_backup.get('sha256')}")
    else:
        print("Ultimo respaldo:      Sin respaldos completados.")
    print(f"Proximo respaldo:     {backup.get('next_run_at')}")
    restore = "HABILITADA (solo base temporal)" if backup.get("allow_restore") else "deshabilitada"
    print(f"Restauracion:         {restore}")
else:
    print(f"Servicio de respaldo: NO DISPONIBLE - {backup.get('error')}")
PY
then
    echo 'No se pudo consultar la API del monitor.' >&2
    exit 1
fi

header 'Patroni'
primary="$(monitor_value '/api/status' 'd["service"]["primary_name"]' 2>/dev/null || true)"
if [ -n "$primary" ] && [ "$primary" != "None" ]; then
    docker exec "$(container_name "$primary")" patronictl -c /etc/patroni/patroni.yml list
else
    echo 'Sin primario identificado; no se puede consultar patronictl.'
fi

echo ''
echo 'Dashboard: http://127.0.0.1:8501  |  HAProxy stats: http://127.0.0.1:7000'
