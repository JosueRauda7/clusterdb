#!/usr/bin/env bash
# Detiene el stack. Por defecto conserva los volumenes con datos.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

REMOVE_VOLUMES=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --volumes|-v) REMOVE_VOLUMES=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        *) echo "Opcion desconocida: $1" >&2; exit 1 ;;
    esac
    shift
done

require_docker

if [ "$REMOVE_VOLUMES" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
    echo 'ADVERTENCIA: --volumes elimina PostgreSQL, etcd, respaldos y metricas.'
    read -r -p 'Escriba SI para confirmar: ' answer
    case "$answer" in
        si|SI|s|S|yes|y) ;;
        *) echo 'Cancelado. No se elimino nada.'; exit 0 ;;
    esac
fi

args=(down --remove-orphans)
if [ "$REMOVE_VOLUMES" -eq 1 ]; then
    args+=(-v)
fi

header 'Deteniendo ClusterDB Monitor'
compose "${args[@]}"

if [ "$REMOVE_VOLUMES" -eq 1 ]; then
    echo 'Volumenes eliminados. El proximo arranque hara bootstrap desde cero.'
else
    echo 'Datos conservados en los volumenes de Docker.'
fi
