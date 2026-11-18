#!/usr/bin/env bash
# Failover REAL: detiene el primario, espera la promocion de Patroni,
# reincorpora el nodo caido como replica y verifica que no haya split-brain.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

ASSUME_YES=0
NO_REJOIN=0
TIMEOUT_SECONDS=120

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) ASSUME_YES=1 ;;
        --no-rejoin) NO_REJOIN=1 ;;
        --timeout) TIMEOUT_SECONDS="$2"; shift ;;
        *) echo "Opcion desconocida: $1" >&2; exit 1 ;;
    esac
    shift
done

require_docker

available="$(monitor_value '/api/status' 'd["service"]["available"]')"
if [ "$available" != "True" ]; then
    echo 'El servicio no esta disponible; no se puede iniciar la prueba de failover.' >&2
    exit 1
fi

old_primary="$(monitor_value '/api/status' 'd["service"]["primary_name"]')"
new_expected="$(monitor_value '/api/status' '[n["name"] for n in d["nodes"] if n["name"] != d["service"]["primary_name"] and n["role"] == "replica"][0]')"

if [ -z "$old_primary" ] || [ -z "$new_expected" ]; then
    echo 'No se encontro un primario y una replica activos mediante el monitor.' >&2
    exit 1
fi

header 'Prueba de failover real'
echo "Primario actual:   $old_primary"
echo "Replica candidata: $new_expected"
echo "Timeout:           $TIMEOUT_SECONDS s"
echo ''
echo "Se ejecutara: docker compose stop $old_primary"
echo 'Patroni debe promover la replica. Luego se reinicia el nodo caido.'

if [ "$ASSUME_YES" -eq 0 ]; then
    read -r -p 'Escriba SI para provocar el failover real: ' answer
    case "$answer" in
        si|SI|s|S|yes|y) ;;
        *) echo 'Cancelado. No se detuvo ningun nodo.'; exit 0 ;;
    esac
fi

echo ''
echo 'Deteniendo el primario...'
start=$SECONDS
compose stop "$old_primary" || { echo "No se pudo detener $old_primary." >&2; exit 1; }

promoted=0
while (( SECONDS - start < TIMEOUT_SECONDS )); do
    current_available="$(monitor_value '/api/status' 'd["service"]["available"]' 2>/dev/null || echo False)"
    current_primary="$(monitor_value '/api/status' 'd["service"]["primary_name"]' 2>/dev/null || echo '')"
    if [ "$current_available" = "True" ] && [ "$current_primary" = "$new_expected" ]; then
        promoted=1
        break
    fi
    sleep 1
done

elapsed=$(( SECONDS - start ))

if [ "$promoted" -eq 0 ]; then
    echo "No se detecto la promocion dentro del timeout."
    echo "Reiniciando $old_primary para restaurar el servicio..."
    compose start "$old_primary" >/dev/null || true
    echo 'La prueba de failover no pudo confirmar la promocion. Revise los logs de Patroni y etcd.' >&2
    exit 1
fi

echo "Promocion detectada por el script en ${elapsed} s."

# El monitor mide con sus propias muestras; se espera a que cierre el incidente.
start_wait=$SECONDS
while (( SECONDS - start_wait < 40 )); do
    match="$(monitor_value '/api/kpis?window_minutes=0' 'd["last_failover"] is not None and d["last_failover"]["new_primary"] == "'"$new_expected"'"' 2>/dev/null || echo False)"
    if [ "$match" = "True" ]; then
        break
    fi
    sleep 2
done

echo ''
echo 'Resultado medido por el monitor:'
docker exec -i "$MONITOR_CONTAINER" python - <<PY
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/kpis?window_minutes=0", timeout=10) as response:
    kpis = json.load(response)

last = kpis.get("last_failover")
if last:
    print(f"  Failover:        {last.get('old_primary')} -> {last.get('new_primary')}")
    print(f"  Recuperacion:    {last.get('recovery_s')} s")
    rto = kpis["rto"]
    print(f"  RTO objetivo:    {rto['target_seconds']:.0f} s (cumple={rto['met']})")
rpo = kpis["rpo"]
if rpo.get("verified"):
    print(f"  RPO observado:   {rpo['lost_records']} registro(s) perdido(s)")
else:
    print(f"  RPO observado:   {rpo.get('reason')}")
print(f"  Fallos totales:  {kpis['failures']['detected']}")
PY

if [ "$NO_REJOIN" -eq 1 ]; then
    echo ''
    echo "--no-rejoin: $old_primary queda detenido. Para recuperarlo: docker compose start $old_primary"
    exit 0
fi

echo ''
echo "Reincorporando $old_primary como replica..."
start_rejoin=$SECONDS
compose start "$old_primary" >/dev/null || { echo "No se pudo iniciar $old_primary." >&2; exit 1; }

rejoined=0
while (( SECONDS - start_rejoin < 120 )); do
    role="$(patroni_value "$old_primary" 'd.get("role")' 2>/dev/null || echo '')"
    state="$(patroni_value "$old_primary" 'd.get("state")' 2>/dev/null || echo '')"
    if [ "$role" = "replica" ] && [ "$state" = "running" ]; then
        rejoined=1
        break
    fi
    sleep 2
done

if [ "$rejoined" -eq 0 ]; then
    echo "El nodo $old_primary no se reincorporo como replica." >&2
    exit 1
fi

streaming=0
start_streaming=$SECONDS
while (( SECONDS - start_streaming < 60 )); do
    has_stream="$(patroni_value "$new_expected" 'any(r.get("application_name") == "'"$old_primary"'" and r.get("state") == "streaming" for r in (d.get("replication") or []))' 2>/dev/null || echo False)"
    if [ "$has_stream" = "True" ]; then
        streaming=1
        break
    fi
    sleep 2
done

if [ "$streaming" -eq 0 ]; then
    echo "El nodo $old_primary no aparece en streaming." >&2
    exit 1
fi

echo "Reincorporacion confirmada en $(( SECONDS - start_rejoin )) s."
echo 'Verificando topologia y replicacion activa...'

verification=""
for _ in 1 2 3 4; do
    verification="$(monitor_post '/api/actions/verify-replication')"
    ok="$(printf '%s' "$verification" | docker exec -i "$MONITOR_CONTAINER" python -c 'import json,sys; print(json.load(sys.stdin).get("ok"))')"
    if [ "$ok" = "True" ]; then
        break
    fi
    sleep 3
done

if [ "$ok" != "True" ]; then
    echo 'La verificacion de replicacion encontro hallazgos:'
    printf '%s' "$verification" | docker exec -i "$MONITOR_CONTAINER" python -c '
import json, sys
for check in json.load(sys.stdin).get("checks", []):
    print("  [{}] {}: {}".format("OK" if check.get("ok") else "FALLA", check.get("name"), check.get("detail")))
'
    exit 1
fi

echo 'Verificacion de replicacion: OK'

old_role="$(patroni_value "$old_primary" 'd.get("role")')"
new_role="$(patroni_value "$new_expected" 'd.get("role")')"
primaries=0
[ "$old_role" = "primary" ] && primaries=$((primaries + 1))
[ "$new_role" = "primary" ] && primaries=$((primaries + 1))

if [ "$primaries" -gt 1 ]; then
    echo 'SPLIT-BRAIN: dos nodos se reportan como primarios.' >&2
    exit 1
fi
if [ "$primaries" -ne 1 ] || [ "$old_role" != "replica" ]; then
    echo 'La topologia final no es la esperada: un primario y una replica.' >&2
    exit 1
fi
echo 'Topologia final: un solo primario; no hay split-brain.'

echo ''
echo 'Listado final de Patroni:'
docker exec "$(container_name "$new_expected")" patronictl -c /etc/patroni/patroni.yml list

echo ''
echo 'Failover completado. El dashboard ya debe mostrar RTO, RPO y el incidente.'
