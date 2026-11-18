#!/usr/bin/env bash
# Utilidades compartidas por los scripts de Linux/WSL.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MONITOR_CONTAINER="${MONITOR_CONTAINER:-clusterdb-monitor}"

# Git Bash/MSYS convierte argumentos con forma de ruta absoluta (por ejemplo
# /etc/patroni/patroni.yml) a rutas de Windows antes de llamar a docker.exe.
# Esto desactiva esa conversion para que los comandos dentro del contenedor
# reciban la ruta Linux correcta.
case "${OSTYPE:-}" in
    msys*|cygwin*) export MSYS_NO_PATHCONV=1 ;;
esac

compose() {
    docker compose "$@"
}

require_docker() {
    if ! docker info >/dev/null 2>&1; then
        echo "Docker no responde. Inicie Docker Desktop y vuelva a intentarlo." >&2
        exit 1
    fi
}

monitor_json() {
    local path="$1"
    docker exec "$MONITOR_CONTAINER" curl -fsS "http://127.0.0.1:8000${path}"
}

monitor_post() {
    local path="$1"
    local json="${2:-}"
    [ -z "$json" ] && json='{}'
    docker exec "$MONITOR_CONTAINER" curl -fsS -X POST \
        -H 'Content-Type: application/json' -d "$json" \
        "http://127.0.0.1:8000${path}"
}

json_value() {
    local expr="$1"
    docker exec -i "$MONITOR_CONTAINER" python -c \
        "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$expr"
}

monitor_value() {
    local path="$1"
    local expr="$2"
    monitor_json "$path" | json_value "$expr"
}

internal_json() {
    local url="$1"
    docker exec "$MONITOR_CONTAINER" curl -fsS "$url"
}

internal_value() {
    local url="$1"
    local expr="$2"
    internal_json "$url" | json_value "$expr"
}

patroni_value() {
    local service="$1"
    local expr="$2"
    internal_value "http://${service}:8008/patroni" "$expr"
}

container_name() {
    printf 'clusterdb-%s\n' "$1"
}

format_seconds() {
    local value="${1:-}"
    if [ -z "$value" ] || [ "$value" = "None" ] || [ "$value" = "null" ]; then
        echo "Sin datos suficientes"
        return
    fi
    awk -v s="$value" 'BEGIN {
        if (s < 1) printf "%.0f ms\n", s * 1000;
        else if (s < 60) printf "%.1f s\n", s;
        else if (s < 3600) printf "%.1f min\n", s / 60;
        else printf "%.1f h\n", s / 3600;
    }'
}

header() {
    printf '\n%s\n%s\n%s\n' \
        "==========================================================================" \
        "$1" \
        "=========================================================================="
}

wait_service_healthy() {
    local service="$1"
    local timeout="${2:-240}"
    local container
    container="$(container_name "$service")"
    local start=$SECONDS
    while (( SECONDS - start < timeout )); do
        local state
        state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
        if [ "$state" = "healthy" ]; then
            return 0
        fi
        sleep 2
    done
    echo "Timeout esperando $service healthy" >&2
    return 1
}

check_port() {
    local port="$1"
    (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1
}
