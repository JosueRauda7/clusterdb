#!/usr/bin/env bash
# Smoke test del dashboard dentro del contenedor.
# Requiere el stack levantado (monitor + dashboard saludables).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if ! docker info >/dev/null 2>&1; then
    echo 'Docker no responde. Inicie Docker Desktop y vuelva a intentarlo.' >&2
    exit 1
fi

state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' clusterdb-dashboard 2>/dev/null || echo 'ausente')"
if [ "$state" != "healthy" ]; then
    echo "El contenedor clusterdb-dashboard no esta healthy (estado: $state)." >&2
    echo 'Ejecute ./scripts/up.sh.' >&2
    exit 1
fi

docker exec -i clusterdb-dashboard python - < "$SCRIPT_DIR/dashboard_smoke.py"
