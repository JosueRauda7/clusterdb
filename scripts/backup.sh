#!/usr/bin/env bash
# Solicita un respaldo real y, con --wait, espera el resultado verificado.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

WAIT=0
TIMEOUT_SECONDS=180

while [ $# -gt 0 ]; do
    case "$1" in
        --wait|-w) WAIT=1 ;;
        --timeout) TIMEOUT_SECONDS="$2"; shift ;;
        *) echo "Opcion desconocida: $1" >&2; exit 1 ;;
    esac
    shift
done

require_docker

before_reachable="$(monitor_value '/api/backups' 'd["reachable"]')"
if [ "$before_reachable" != "True" ]; then
    echo 'El servicio de respaldo no esta accesible.' >&2
    exit 1
fi

running="$(monitor_value '/api/backups' 'd["status"]["running"]')"
before_sequence=""
if [ "$running" = "True" ]; then
    echo 'Ya hay un respaldo en ejecucion; se esperara su resultado.'
else
    before_sequence="$(monitor_value '/api/backups' 'None if d["status"]["last_attempt"] is None else d["status"]["last_attempt"]["sequence"]')"
    monitor_post '/api/actions/backup'
    echo ''
    echo 'Solicitud de respaldo enviada.'
fi

if [ "$WAIT" -eq 0 ]; then
    echo 'Use --wait para esperar y mostrar el resultado.'
    exit 0
fi

echo 'Esperando a que termine el respaldo...'
start=$SECONDS
while (( SECONDS - start < TIMEOUT_SECONDS )); do
    snapshot="$(monitor_json '/api/backups')"
    line="$(printf '%s' "$snapshot" | docker exec -i "$MONITOR_CONTAINER" python -c '
import json, sys
d = json.load(sys.stdin)
a = d["status"].get("last_attempt")
if not a:
    print("wait")
else:
    print("{}|{}".format(a.get("status"), a.get("sequence")))
')"
    status="${line%%|*}"
    sequence="${line##*|}"
    if [ "$status" = "success" ] || [ "$status" = "failed" ]; then
        if [ -z "$before_sequence" ] || [ "$sequence" != "$before_sequence" ]; then
            break
        fi
    fi
    sleep 1
done

if [ "${status:-wait}" = "wait" ]; then
    echo 'Timeout esperando el respaldo.' >&2
    exit 1
fi

header 'Resultado del respaldo'
docker exec -i "$MONITOR_CONTAINER" python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/backups", timeout=10) as response:
    data = json.load(response)

attempt = data["status"]["last_attempt"]
print(f"Estado:        {attempt.get('status')}")
print(f"Archivo:       {attempt.get('filename')}")
print(f"Tamano:        {attempt.get('size_bytes')} bytes")
print(f"Duracion:      {attempt.get('duration_s')} s")
print(f"SHA-256:       {attempt.get('sha256')}")
print(f"Verificacion:  {attempt.get('verification')}")
print(f"Respaldos:     {data['status'].get('count')}")
if attempt.get("status") != "success":
    print(f"Error: {attempt.get('error')}")
PY

if [ "${status}" != "success" ]; then
    exit 1
fi

echo 'Respaldo completado y verificado.'
