#!/usr/bin/env bash
# Ejecuta las pruebas unitarias en un contenedor Python aislado.
# No usa la red y monta el repositorio en solo lectura.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if ! docker info >/dev/null 2>&1; then
    echo 'Docker no responde. Inicie Docker Desktop y vuelva a intentarlo.' >&2
    exit 1
fi

docker run --rm --network none \
    -v "$REPO_ROOT:/app:ro" \
    -e PYTHONPATH=/app/monitor \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -w /app \
    python:3.12-slim-bookworm \
    python -m unittest discover -s tests -v
