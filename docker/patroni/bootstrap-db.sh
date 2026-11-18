#!/usr/bin/env bash
# Se ejecuta UNA sola vez, en el nodo que crea el clúster (postgres-master).
# Crea roles de aplicación y la base cluster_demo con esquema y datos semilla.
set -Eeuo pipefail

export PGHOST=/var/run/postgresql
export PGUSER=postgres
export PGDATABASE=postgres
export PGPORT=5432

PSQL=(psql --no-psqlrc -v ON_ERROR_STOP=1)

echo "[bootstrap] Esperando a que PostgreSQL acepte conexiones locales..."
for _ in $(seq 1 30); do
    if "${PSQL[@]}" -tAc "SELECT 1" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "[bootstrap] Creando roles de aplicación..."
"${PSQL[@]}" \
    -v app_password="${APP_PASSWORD}" \
    -v backup_password="${BACKUP_PASSWORD}" \
    -v monitor_password="${MONITOR_PASSWORD}" \
    -f /opt/clusterdb/sql/01_roles.sql

if [ "$("${PSQL[@]}" -tAc "SELECT 1 FROM pg_database WHERE datname = 'cluster_demo'")" != "1" ]; then
    echo "[bootstrap] Creando base cluster_demo..."
    createdb --owner=postgres cluster_demo
fi

echo "[bootstrap] Aplicando esquema..."
"${PSQL[@]}" -d cluster_demo -f /opt/clusterdb/sql/02_schema.sql

echo "[bootstrap] Insertando datos semilla (idempotente)..."
"${PSQL[@]}" -d cluster_demo -f /opt/clusterdb/sql/03_seed.sql

echo "[bootstrap] Base de demostración lista."
"${PSQL[@]}" -d cluster_demo -tAc \
    "SELECT 'clientes=' || count(*) FROM clientes UNION ALL
     SELECT 'productos=' || count(*) FROM productos UNION ALL
     SELECT 'ventas=' || count(*) FROM ventas;"
