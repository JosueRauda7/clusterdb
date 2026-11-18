#!/usr/bin/env bash
# Levanta el stack completo, espera healthchecks y muestra las URLs locales.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

NO_BUILD=0
OFFLINE=0
TIMEOUT_SECONDS=360

while [ $# -gt 0 ]; do
    case "$1" in
        --no-build) NO_BUILD=1 ;;
        --offline) OFFLINE=1 ;;
        --timeout) TIMEOUT_SECONDS="$2"; shift ;;
        *) echo "Opcion desconocida: $1" >&2; exit 1 ;;
    esac
    shift
done

require_docker

if [ ! -f "$REPO_ROOT/.env" ]; then
    echo "No existe .env; ejecutando init-env.sh..."
    "$SCRIPT_DIR/init-env.sh"
fi

args=(up -d)
if [ "$NO_BUILD" -eq 0 ]; then
    args+=(--build)
fi
if [ "$OFFLINE" -eq 1 ]; then
    args+=(--pull never)
fi

header 'Levantando ClusterDB Monitor'
compose "${args[@]}"

services=(
    etcd1 etcd2 etcd3
    postgres-master postgres-replica
    haproxy postgres-backup monitor txgen dashboard
)

for service in "${services[@]}"; do
    echo "Esperando $service..."
    wait_service_healthy "$service" "$TIMEOUT_SECONDS"
done

header 'Estado de los contenedores'
compose ps

echo ''
echo 'Comprobando puertos publicados en 127.0.0.1...'
check_port 15432 && echo '  OK    15432: PostgreSQL via HAProxy' || echo '  FALLA 15432: PostgreSQL via HAProxy'
check_port 7000 && echo '  OK    7000: HAProxy stats' || echo '  FALLA 7000: HAProxy stats'
check_port 8501 && echo '  OK    8501: Dashboard' || echo '  FALLA 8501: Dashboard'

echo ''
echo 'URLs locales:'
echo '  Dashboard:        http://127.0.0.1:8501'
echo '  HAProxy stats:    http://127.0.0.1:7000'
echo '  PostgreSQL:       postgresql://app@127.0.0.1:15432/cluster_demo'
echo ''
echo 'Siguiente paso: ./scripts/status.sh'
