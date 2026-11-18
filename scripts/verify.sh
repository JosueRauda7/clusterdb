#!/usr/bin/env bash
# Verificacion integral NO destructiva del stack.
# Comprueba contenedores, quorum, Patroni, replicacion, respaldo, dashboard y puertos.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

RUN_BACKUP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --backup) RUN_BACKUP=1 ;;
        *) echo "Opcion desconocida: $1" >&2; exit 1 ;;
    esac
    shift
done

require_docker

CHECK_NAMES=()
CHECK_OKS=()
CHECK_DETAILS=()

add_check() {
    CHECK_NAMES+=("$1")
    CHECK_OKS+=("$2")
    CHECK_DETAILS+=("${3:-}")
}

header 'Verificacion integral de ClusterDB Monitor'

# 1. Contenedores y healthchecks.
expected=(
    etcd1 etcd2 etcd3
    postgres-master postgres-replica haproxy
    postgres-backup monitor txgen dashboard
)
bad=()
for service in "${expected[@]}"; do
    container="$(container_name "$service")"
    state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || echo 'ausente')"
    if [ "$state" != "healthy" ]; then
        bad+=("$service=$state")
    fi
done
if [ "${#bad[@]}" -eq 0 ]; then
    add_check 'contenedores y healthchecks' 1 '10/10 activos y saludables'
else
    add_check 'contenedores y healthchecks' 0 "$(IFS=', '; echo "${bad[*]}")"
fi

# 2. Quorum de etcd.
etcd_out="$(docker exec clusterdb-etcd1 etcdctl \
    --endpoints=http://etcd1:2379,http://etcd2:2379,http://etcd3:2379 \
    endpoint health --cluster 2>&1 || true)"
etcd_healthy="$(printf '%s\n' "$etcd_out" | grep -c 'is healthy' || true)"
if [ "$etcd_healthy" -ge 3 ]; then
    add_check 'quorum etcd (3/3)' 1 "endpoints saludables=$etcd_healthy"
else
    add_check 'quorum etcd (3/3)' 0 "endpoints saludables=$etcd_healthy"
fi

# 3. Patroni: exactamente un primario y una replica.
master_role="$(patroni_value postgres-master 'd.get("role")' 2>/dev/null || echo '')"
replica_role="$(patroni_value postgres-replica 'd.get("role")' 2>/dev/null || echo '')"
master_state="$(patroni_value postgres-master 'd.get("state")' 2>/dev/null || echo '')"
replica_state="$(patroni_value postgres-replica 'd.get("state")' 2>/dev/null || echo '')"
primaries=0
[ "$master_role" = "primary" ] && primaries=$((primaries + 1))
[ "$replica_role" = "primary" ] && primaries=$((primaries + 1))
replicas=0
[ "$master_role" = "replica" ] && replicas=$((replicas + 1))
[ "$replica_role" = "replica" ] && replicas=$((replicas + 1))
topology_detail="postgres-master=${master_role}/${master_state}; postgres-replica=${replica_role}/${replica_state}"
if [ "$primaries" -eq 1 ] && [ "$replicas" -eq 1 ] && [ "$master_state" = "running" ] && [ "$replica_state" = "running" ]; then
    add_check 'topologia Patroni (1 primario + 1 replica)' 1 "$topology_detail"
else
    add_check 'topologia Patroni (1 primario + 1 replica)' 0 "$topology_detail"
fi

# 4. Verificacion activa de replicacion (escribe un latido y lo busca en la replica).
if verification="$(monitor_post '/api/actions/verify-replication' 2>/dev/null)"; then
    read -r verification_ok verification_detail < <(printf '%s' "$verification" | docker exec -i "$MONITOR_CONTAINER" python -c '
import json, sys
data = json.load(sys.stdin)
failed = [c for c in data.get("checks", []) if not c.get("ok")]
if failed:
    detail = " | ".join("{}: {}".format(c.get("name"), c.get("detail")) for c in failed)
else:
    detail = "{} comprobaciones OK en {} s".format(len(data.get("checks", [])), data.get("elapsed_seconds"))
print("1" if data.get("ok") else "0", detail.replace("\n", " "))
')
    add_check 'replicacion activa' "$verification_ok" "$verification_detail"
else
    add_check 'replicacion activa' 0 'la verificacion no respondio'
fi

# 5. API del monitor y muestras reales.
samples="$(monitor_value '/api/kpis?window_minutes=0' 'd["monitor"]["samples"]' 2>/dev/null || echo 0)"
health_status="$(monitor_value '/health' 'd.get("status")' 2>/dev/null || echo '')"
availability="$(monitor_value '/api/kpis?window_minutes=0' '("sin datos" if d["availability"]["pct"] is None else "{:.2f}%".format(d["availability"]["pct"]))' 2>/dev/null || echo 'sin datos')"
if [ "$health_status" = "ok" ] && [ "${samples:-0}" -gt 0 ]; then
    add_check 'API del monitor y muestras' 1 "muestras=$samples, disponibilidad=$availability"
else
    add_check 'API del monitor y muestras' 0 "health=$health_status, muestras=$samples"
fi

# 6. Respaldo verificado.
if [ "$RUN_BACKUP" -eq 1 ]; then
    echo 'Ejecutando un respaldo nuevo para la verificacion...'
    "$SCRIPT_DIR/backup.sh" --wait || true
fi
backup_info="$(monitor_value '/api/backups' 'd["status"]["last_backup"]' 2>/dev/null || echo 'None')"
if [ "$backup_info" != "None" ]; then
    read -r backup_ok backup_detail < <(printf '%s' "$backup_info" | docker exec -i "$MONITOR_CONTAINER" python -c '
import ast, sys
data = ast.literal_eval(sys.stdin.read())
ok = data.get("status") == "success" and str(data.get("verification", "")).startswith("ok")
print("1" if ok else "0", "{}, {} bytes, {}".format(data.get("filename"), data.get("size_bytes"), data.get("verification")))
')
    add_check 'respaldo verificado (SHA/TOC)' "$backup_ok" "$backup_detail"
else
    add_check 'respaldo verificado (SHA/TOC)' 0 'sin respaldo completado'
fi

# 7. Dashboard, stats de HAProxy y puertos publicados.
http_ok() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 8 "$url" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 8 -O /dev/null "$url" >/dev/null 2>&1
    else
        local port="${url#*:}"
        port="${port%%/*}"
        check_port "$port"
    fi
}

if http_ok 'http://127.0.0.1:8501/_stcore/health'; then
    add_check 'dashboard en 127.0.0.1:8501' 1 'HTTP 200'
else
    add_check 'dashboard en 127.0.0.1:8501' 0 'sin respuesta'
fi

if http_ok 'http://127.0.0.1:7000/'; then
    add_check 'HAProxy stats en 127.0.0.1:7000' 1 'HTTP 200'
else
    add_check 'HAProxy stats en 127.0.0.1:7000' 0 'sin respuesta'
fi

if check_port 15432; then
    add_check 'PostgreSQL en 127.0.0.1:15432' 1 'puerto acepta conexiones'
else
    add_check 'PostgreSQL en 127.0.0.1:15432' 0 'puerto cerrado'
fi

# 8. Sin dos primarios.
if [ "$primaries" -gt 1 ]; then
    add_check 'sin split-brain' 0 'dos nodos primarios'
else
    add_check 'sin split-brain' 1 'un solo primario'
fi

header 'Resultados'
failed=0
for index in "${!CHECK_NAMES[@]}"; do
    if [ "${CHECK_OKS[$index]}" = "1" ]; then
        printf '  [OK]    %s\n' "${CHECK_NAMES[$index]}"
    else
        printf '  [FALLA] %s\n' "${CHECK_NAMES[$index]}"
        failed=$((failed + 1))
    fi
    if [ -n "${CHECK_DETAILS[$index]}" ]; then
        printf '          %s\n' "${CHECK_DETAILS[$index]}"
    fi
done

echo ''
if [ "$failed" -eq 0 ]; then
    echo "Verificacion correcta: ${#CHECK_NAMES[@]}/${#CHECK_NAMES[@]} comprobaciones OK."
    exit 0
fi
echo "Verificacion con $failed fallo(s) de ${#CHECK_NAMES[@]}."
exit 1
