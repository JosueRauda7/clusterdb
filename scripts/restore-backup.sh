#!/usr/bin/env bash
# Restaura un respaldo SOLO a una base temporal cluster_demo_restore_*.
# Requiere ALLOW_RESTORE=true en postgres-backup y monitor.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

FILE=""
LATEST=0
ASSUME_YES=0
TIMEOUT_SECONDS=900

while [ $# -gt 0 ]; do
    case "$1" in
        --file|-f) FILE="$2"; shift ;;
        --latest|-l) LATEST=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --timeout) TIMEOUT_SECONDS="$2"; shift ;;
        *) echo "Opcion desconocida: $1" >&2; exit 1 ;;
    esac
    shift
done

require_docker

if [ -z "$FILE" ]; then
    LATEST=1
fi
if [ -n "$FILE" ] && ! printf '%s' "$FILE" | grep -Eq '^[A-Za-z0-9_.-]+\.dump$'; then
    echo 'El nombre del archivo debe terminar en .dump y no puede contener rutas.' >&2
    exit 1
fi

backup_allow="$(docker exec clusterdb-postgres-backup printenv ALLOW_RESTORE 2>/dev/null | tr -d '\r' || true)"
monitor_allow="$(docker exec "$MONITOR_CONTAINER" printenv ALLOW_RESTORE 2>/dev/null | tr -d '\r' || true)"

if [ "$backup_allow" != "true" ] || [ "$monitor_allow" != "true" ]; then
    echo 'Restauracion deshabilitada por seguridad.'
    echo "ALLOW_RESTORE en postgres-backup: $backup_allow"
    echo "ALLOW_RESTORE en monitor:         $monitor_allow"
    echo ''
    echo 'Para habilitarla en esta sesion de demo:'
    echo "  export ALLOW_RESTORE=true"
    echo '  docker compose up -d --force-recreate postgres-backup monitor dashboard'
    echo '  ./scripts/restore-backup.sh --latest'
    echo ''
    echo 'La restauracion NUNCA sobrescribe cluster_demo: solo crea una base temporal.'
    exit 2
fi

target="${FILE:-el ultimo respaldo}"
if [ "$ASSUME_YES" -eq 0 ]; then
    echo "Se restaurara $target en una base temporal nueva."
    echo 'La base activa cluster_demo no se modifica.'
    read -r -p 'Escriba SI para continuar: ' answer
    case "$answer" in
        si|SI|s|S|yes|y) ;;
        *) echo 'Cancelado.'; exit 0 ;;
    esac
fi

if [ -n "$FILE" ]; then
    payload="$(printf '{"filename": "%s", "latest": false}' "$FILE")"
else
    payload='{"filename": null, "latest": true}'
fi

header 'Restaurando respaldo a base temporal'
echo 'Esto puede tardar varios segundos...'
response="$(monitor_post '/api/actions/restore' "$payload")"

printf '%s' "$response" | docker exec -i "$MONITOR_CONTAINER" python -c '
import json
import sys

data = json.load(sys.stdin)
restore = data["restore"]
print("Base temporal:  {}".format(restore.get("database")))
print("Archivo:        {}".format(restore.get("filename")))
print("Duracion:       {} s".format(restore.get("duration_s")))
print("Conteos verificados:")
print(json.dumps(restore.get("counts"), indent=2))
print("")
print("La base activa cluster_demo no fue modificada.")
print("La base temporal {} se conserva para inspeccion; las mas antiguas se limpian.".format(restore.get("database")))
'
