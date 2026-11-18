#!/usr/bin/env bash
# Crea .env a partir de .env.example con contrasenas aleatorias.
# No sobrescribe un .env existente salvo que se use --force.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=1 ;;
        *) echo "Opcion desconocida: $arg" >&2; exit 1 ;;
    esac
done

EXAMPLE_PATH="$REPO_ROOT/.env.example"
ENV_PATH="$REPO_ROOT/.env"

if [ ! -f "$EXAMPLE_PATH" ]; then
    echo "No existe $EXAMPLE_PATH" >&2
    exit 1
fi

if [ -f "$ENV_PATH" ] && [ "$FORCE" -eq 0 ]; then
    echo "Ya existe .env. No se modifico. Use --force para regenerarlo."
    exit 0
fi

new_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 24
    else
        od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
    fi
}

declare -A SECRETS=(
    [POSTGRES_PASSWORD]="$(new_secret)"
    [REPLICATOR_PASSWORD]="$(new_secret)"
    [APP_PASSWORD]="$(new_secret)"
    [BACKUP_PASSWORD]="$(new_secret)"
    [MONITOR_PASSWORD]="$(new_secret)"
)

content="$(cat "$EXAMPLE_PATH")"
for key in "${!SECRETS[@]}"; do
    value="${SECRETS[$key]}"
    content="$(printf '%s\n' "$content" | sed -E "s|^${key}=.*$|${key}=${value}|")"
done

umask 077
printf '%s\n' "$content" > "$ENV_PATH"
chmod 600 "$ENV_PATH"

echo "Creado .env con contrasenas aleatorias."
echo "ALLOW_RESTORE queda en false. El archivo .env esta en .gitignore."
