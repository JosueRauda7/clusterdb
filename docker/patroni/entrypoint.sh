#!/usr/bin/env bash
set -Eeuo pipefail

: "${PATRONI_NAME:?PATRONI_NAME no está definido}"
: "${PATRONI_RESTAPI_CONNECT_ADDRESS:?PATRONI_RESTAPI_CONNECT_ADDRESS no está definido}"
: "${PATRONI_POSTGRESQL_CONNECT_ADDRESS:?PATRONI_POSTGRESQL_CONNECT_ADDRESS no está definido}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD no está definido}"
: "${REPLICATOR_PASSWORD:?REPLICATOR_PASSWORD no está definido}"

mkdir -p "${PGDATA}" /run/postgresql /var/log/patroni

# El volumen puede venir vacío o creado por otra ejecución. Patroni corre
# siempre como el usuario postgres, nunca como root.
chown -R postgres:postgres "${PGDATA}" /run/postgresql /var/log/patroni

# PostgreSQL rechaza un data_dir accesible por grupo/mundo. pg_basebackup
# hereda el modo del directorio existente, así que lo aseguramos aquí.
chmod 0700 "${PGDATA}"

/opt/patroni/bin/python /usr/local/bin/render-patroni-config \
    /etc/patroni/patroni.yml.tmpl /etc/patroni/patroni.yml

chown postgres:postgres /etc/patroni/patroni.yml
chmod 0600 /etc/patroni/patroni.yml

echo "[clusterdb] Nodo=${PATRONI_NAME} bootstrap=${PATRONI_BOOTSTRAP_ENABLED:-false}"
echo "[clusterdb] Iniciando Patroni..."

exec gosu postgres /opt/patroni/bin/patroni /etc/patroni/patroni.yml
