"""API interna del monitor ClusterDB.

El dashboard consume esta API. No se publica al host: vive solo en la red
interna de Docker. Las acciones destructivas no viven aquí; el único control
delicado es restaurar a una base temporal y está bloqueado por ALLOW_RESTORE.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg2
import requests
from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel

import store
from collector import Collector, iso, utc_now
from kpi import (
    compute_availability,
    compute_incident_metrics,
    compute_sla,
    evaluate_rpo,
    evaluate_rto,
)

collector = Collector()

TARGETS = {
    "sla_percent": float(os.getenv("SLA_TARGET_PERCENT", "99.9")),
    "rto_seconds": float(os.getenv("RTO_TARGET_SECONDS", "30")),
    "rpo_seconds": float(os.getenv("RPO_TARGET_SECONDS", "60")),
}
BACKUP_URL = os.getenv("BACKUP_URL", "http://postgres-backup:8080").rstrip("/")
ALLOW_RESTORE = os.getenv("ALLOW_RESTORE", "false").lower() == "true"
NODE_HOSTS = [h.strip() for h in os.getenv(
    "NODE_HOSTS", "postgres-master,postgres-replica").split(",") if h.strip()]
DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "cluster_demo"),
    "user": os.getenv("DB_USER", "monitor"),
    "password": os.getenv("DB_PASSWORD", ""),
    "port": int(os.getenv("DB_PORT", "5432")),
    "connect_timeout": 2,
}
DB_HOST = os.getenv("DB_HOST", "haproxy")


@asynccontextmanager
async def lifespan(_: FastAPI):
    collector.start()
    try:
        yield
    finally:
        collector.stop()


app = FastAPI(
    title="ClusterDB Monitor API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def _window(minutes: int | None) -> tuple[float, float]:
    now = utc_now()
    if minutes is None or minutes <= 0:
        first = store.first_sample_ts()
        return (first if first is not None else now), now
    return now - minutes * 60.0, now


def _db_connect(host: str = DB_HOST, timeout: int = 2):
    conn = psycopg2.connect(host=host, **{**DB_CONFIG, "connect_timeout": timeout},
                            application_name="clusterdb-monitor-api")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 2500")
    return conn


def _read_generator_stats() -> dict[str, Any] | None:
    path = os.getenv("GENERATOR_STATS_PATH", "/generator-data/stats.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data["file_age_seconds"] = round(
                max(0.0, utc_now() - float(data.get("updated_epoch", 0))), 1
            )
            return data
    except (OSError, ValueError, TypeError):
        return None
    return None


def _backup_status(timeout: float = 2.5) -> dict[str, Any]:
    try:
        response = requests.get(f"{BACKUP_URL}/status", timeout=timeout)
        response.raise_for_status()
        data = response.json()
        data["reachable"] = True
        return data
    except Exception as exc:
        latest = (store.latest_sample() or {}).get("backup") or {}
        latest = dict(latest)
        latest.update({
            "reachable": False,
            "service": "unreachable",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        })
        return latest


def _decorate_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for node in nodes:
        item = dict(node)
        role = item.get("role")
        item["role_label"] = {
            "primary": "PRIMARY",
            "replica": "REPLICA",
        }.get(role, "SIN DATOS")
        item["state_label"] = {
            "running": "Activo",
            "stopped": "Detenido",
            "starting": "Iniciando",
            "unknown": "Sin conexión",
            "crashed": "Caído",
        }.get(item.get("state"), item.get("state") or "Sin datos")
        out.append(item)
    return out


def _decorate_failover(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["rto_met"] = (
        None if item.get("recovery_s") is None or item.get("rto_target_s") is None
        else bool(item["recovery_s"] <= item["rto_target_s"])
    )
    item["old_primary_label"] = _label_for_node(item.get("old_primary"))
    item["new_primary_label"] = _label_for_node(item.get("new_primary"))
    return item


def _label_for_node(name: str | None) -> str | None:
    return {"postgres-master": "Master", "postgres-replica": "HA"}.get(name, name)


def _event_dict(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    try:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
    # La UI usa nombres explícitos; se mantienen también las columnas crudas.
    item["event_type"] = item.get("type")
    item["message"] = item.get("description")
    return item


# ----------------------------------------------------------------------
# Health y estado
# ----------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "clusterdb-monitor",
        "uptime_seconds": round(utc_now() - collector.started_at, 1),
        "last_poll_iso": iso(collector.last_poll_at) if collector.last_poll_at else None,
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    latest = store.latest_sample()
    now = utc_now()
    nodes = (latest or {}).get("nodes") or collector._unknown_nodes()
    replication = (latest or {}).get("replication") or {}
    backup = (latest or {}).get("backup") or _backup_status()
    open_incident = store.get_open_incident()
    if open_incident and open_incident.get("ended_at") is None:
        open_incident = dict(open_incident)
        open_incident["elapsed_seconds"] = round(now - open_incident["started_at"], 3)

    last_fo = _decorate_failover(store.last_failover())
    service = {
        "available": None if latest is None else bool(latest.get("available")),
        "latency_ms": (latest or {}).get("latency_ms"),
        "last_sample_iso": (latest or {}).get("ts_iso"),
        "sample_age_seconds": (
            round(now - float(latest["ts"]), 1) if latest else None
        ),
        "primary_name": (latest or {}).get("primary_name"),
        "primary_label": _label_for_node((latest or {}).get("primary_name")),
        "in_recovery": (latest or {}).get("in_recovery"),
        "error": (latest or {}).get("error"),
        "last_hb_id": (latest or {}).get("last_hb_id"),
        "last_venta_id": (latest or {}).get("last_venta_id"),
    }
    return {
        "generated_at": now,
        "generated_iso": iso(now),
        "service": service,
        "nodes": _decorate_nodes(nodes),
        "replication": replication,
        "backup": backup,
        "generator": _read_generator_stats(),
        "targets": TARGETS,
        "open_incident": open_incident,
        "last_failover": last_fo,
        "allow_restore": ALLOW_RESTORE,
        "monitor": collector.runtime_info(),
    }


@app.get("/api/kpis")
def kpis(window_minutes: int = Query(60, ge=0, le=60 * 24 * 90)) -> dict[str, Any]:
    start, end = _window(window_minutes)
    samples = store.samples_between(start, end)
    availability = compute_availability(samples, poll_seconds=collector.poll_seconds)
    incidents = store.incidents_between(start, end)
    incident_metrics = compute_incident_metrics(
        incidents, availability.get("observed_seconds") or 0.0
    )
    sla = compute_sla(
        availability.get("pct"), TARGETS["sla_percent"],
        availability.get("observed_seconds") or 0.0,
        availability.get("downtime_seconds") or 0.0,
    )
    last_fo = store.last_failover()
    rto = evaluate_rto(last_fo, TARGETS["rto_seconds"])
    rpo = evaluate_rpo(last_fo, TARGETS["rpo_seconds"])
    tx = store.tx_counts(start, end)
    stats = _read_generator_stats() or {}

    return {
        "window": {
            "start": start,
            "start_iso": iso(start),
            "end": end,
            "end_iso": iso(end),
            "minutes": window_minutes,
        },
        "availability": availability,
        "mttr": {
            "seconds": incident_metrics["mttr_seconds"],
            "reason": incident_metrics["mttr_reason"],
        },
        "mtbf": {
            "seconds": incident_metrics["mtbf_seconds"],
            "reason": incident_metrics["mtbf_reason"],
        },
        "failures": {
            "detected": incident_metrics["failures"],
            "resolved": incident_metrics["resolved_incidents"],
            "open": incident_metrics["open_incidents"],
        },
        "sla": sla,
        "rto": rto,
        "rpo": rpo,
        "last_failover": _decorate_failover(last_fo),
        "transactions": {
            "window_ok": tx["ok"],
            "window_failed": tx["failed"],
            "window_total": tx["total"],
            "total_ok": stats.get("successful"),
            "total_failed": stats.get("failed"),
            "last_record": stats.get("last_record"),
            "last_error": stats.get("last_error"),
            "generator_state": stats.get("state"),
        },
        "targets": TARGETS,
        "monitor": collector.runtime_info(),
    }


@app.get("/api/timeseries")
def timeseries(window_minutes: int = Query(60, ge=0, le=60 * 24 * 90)) -> dict[str, Any]:
    start, end = _window(window_minutes)
    if window_minutes <= 0:
        bucket = 900
    elif window_minutes <= 30:
        bucket = 30
    elif window_minutes <= 360:
        bucket = 60
    elif window_minutes <= 1440:
        bucket = 300
    else:
        bucket = 900

    samples = store.samples_between(start, end)
    series = store.series_from_samples(samples, bucket)
    incidents = store.incidents_between(start, end)
    failovers = store.failovers_between(start, end)
    return {
        "window": {"start": start, "end": end, "minutes": window_minutes},
        "bucket_seconds": bucket,
        "availability": series,
        "transactions": store.tx_series(start, end, bucket),
        "incidents": incidents,
        "failovers": [_decorate_failover(row) for row in failovers],
    }


@app.get("/api/events")
def events(
    window_minutes: int = Query(1440, ge=0, le=60 * 24 * 90),
    limit: int = Query(300, ge=1, le=2000),
    event_type: str | None = None,
) -> dict[str, Any]:
    start, end = _window(window_minutes)
    rows = store.events(start, end, limit=limit, event_type=event_type)
    return {"events": [_event_dict(row) for row in rows], "count": len(rows)}


@app.get("/api/backups")
def backups() -> dict[str, Any]:
    data = _backup_status()
    history = data.get("history") or []
    return {
        "reachable": data.get("reachable", False),
        "status": data,
        "history": history[-20:][::-1],
    }


# ----------------------------------------------------------------------
# Acciones seguras para el laboratorio
# ----------------------------------------------------------------------
@app.post("/api/actions/verify-replication")
def verify_replication() -> dict[str, Any]:
    """Verificación activa: escribe un latido y comprueba que llegue a la réplica.

    También detecta dos primarios (split-brain) consultando ambos nodos.
    """
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    primary_conn = None
    replica_conn = None
    heartbeat_id = None
    note = f"verify-{uuid.uuid4()}"
    try:
        primary_conn = _db_connect(DB_HOST)
        with primary_conn.cursor() as cur:
            cur.execute("SELECT pg_is_in_recovery(), pg_current_wal_lsn()::text")
            in_recovery, primary_lsn = cur.fetchone()
        checks.append({
            "name": "servicio_primario",
            "ok": not in_recovery,
            "detail": "HAProxy entrega un primario" if not in_recovery
                      else "HAProxy está entregando una réplica",
        })
        if in_recovery:
            return _verification_result(checks, started, heartbeat_id)

        with primary_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ha_heartbeat (source, note) VALUES ('verify', %s) RETURNING id",
                (note,),
            )
            heartbeat_id = cur.fetchone()[0]
        checks.append({
            "name": "escritura_ha_proxy",
            "ok": True,
            "detail": f"Latido de verificación insertado con id={heartbeat_id}",
        })

        node_states = []
        for host in NODE_HOSTS:
            try:
                conn = _db_connect(host, timeout=2)
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT pg_is_in_recovery(), "
                            "pg_last_wal_replay_lsn()::text, "
                            "pg_last_xact_replay_timestamp()"
                        )
                        row = cur.fetchone()
                    node_states.append({
                        "host": host, "in_recovery": bool(row[0]),
                        "replay_lsn": row[1],
                        "replay_ts": row[2].isoformat() if row[2] else None,
                        "ok": True,
                    })
                finally:
                    conn.close()
            except Exception as exc:
                node_states.append({"host": host, "ok": False, "error": str(exc)[:300]})

        primaries = [n for n in node_states if n.get("ok") and not n["in_recovery"]]
        replicas = [n for n in node_states if n.get("ok") and n["in_recovery"]]
        checks.append({
            "name": "topologia",
            "ok": len(primaries) == 1 and len(replicas) == 1,
            "detail": (
                f"primarios={[n['host'] for n in primaries]}, "
                f"réplicas={[n['host'] for n in replicas]}"
            ),
        })
        if len(primaries) > 1:
            checks.append({
                "name": "split_brain",
                "ok": False,
                "detail": "¡Dos nodos se reportan como primarios!",
            })
            return _verification_result(checks, started, heartbeat_id)

        if not replicas:
            checks.append({
                "name": "replica_disponible", "ok": False,
                "detail": "No hay una réplica activa para verificar. "
                          "Puede ser esperado inmediatamente después de un failover.",
            })
            return _verification_result(checks, started, heartbeat_id)

        replica = replicas[0]
        replica_conn = _db_connect(replica["host"])
        with replica_conn.cursor() as cur:
            cur.execute("SET statement_timeout = 2000")
        replicated = False
        replay_lsn = None
        replay_ts = None
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                with replica_conn.cursor() as cur:
                    cur.execute(
                        "SELECT EXISTS (SELECT 1 FROM ha_heartbeat WHERE id = %s), "
                        "pg_last_wal_replay_lsn()::text, "
                        "pg_last_xact_replay_timestamp()",
                        (heartbeat_id,),
                    )
                    replicated, replay_lsn, replay_ts = cur.fetchone()
                if replicated:
                    break
            except psycopg2.Error:
                replica_conn.rollback()
            time.sleep(0.25)

        checks.append({
            "name": "latido_replicado",
            "ok": bool(replicated),
            "detail": (
                f"Latido id={heartbeat_id} visible en {replica['host']}"
                if replicated else
                f"El latido id={heartbeat_id} no llegó a {replica['host']} en 8 s"
            ),
        })

        with primary_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s)::bigint, "
                       "pg_current_wal_lsn()::text",
                (replay_lsn,),
            )
            lag_bytes, current_lsn = cur.fetchone()
        checks.append({
            "name": "retraso_wal",
            "ok": lag_bytes is not None and int(lag_bytes) < 1024 * 1024,
            "detail": f"Retraso actual: {int(lag_bytes or 0)} bytes. "
                      f"LSN primario={current_lsn}, réplica={replay_lsn}",
        })

        with primary_conn.cursor() as cur:
            cur.execute(
                "SELECT application_name, state, sync_state "
                "FROM pg_stat_replication ORDER BY application_name"
            )
            stands = cur.fetchall()
        checks.append({
            "name": "pg_stat_replication",
            "ok": any(s[1] == "streaming" for s in stands),
            "detail": "; ".join(f"{s[0]}={s[1]}/{s[2]}" for s in stands) or "sin filas",
        })
        return _verification_result(checks, started, heartbeat_id)
    except Exception as exc:
        checks.append({
            "name": "error", "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:500]}",
        })
        return _verification_result(checks, started, heartbeat_id)
    finally:
        for conn in (primary_conn, replica_conn):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def _verification_result(checks: list[dict[str, Any]], started: float,
                         heartbeat_id: int | None) -> dict[str, Any]:
    return {
        "ok": bool(checks) and all(c.get("ok") for c in checks),
        "checks": checks,
        "heartbeat_id": heartbeat_id,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


@app.post("/api/actions/backup")
def request_backup() -> dict[str, Any]:
    try:
        response = requests.post(f"{BACKUP_URL}/backup", timeout=5)
        if response.status_code == 409:
            return {"ok": False, "message": "Ya hay un respaldo en ejecución.",
                    "status": response.json()}
        response.raise_for_status()
        return {"ok": True, "message": "Respaldo programado/iniciado.",
                "status": response.json()}
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"No se pudo contactar el servicio de respaldo: {exc}")


class RestoreRequest(BaseModel):
    filename: str | None = None
    latest: bool = True


@app.post("/api/actions/restore")
def request_restore(payload: RestoreRequest = Body(default=RestoreRequest())) -> dict[str, Any]:
    if not ALLOW_RESTORE:
        raise HTTPException(
            status_code=403,
            detail="Restauración deshabilitada. Configure ALLOW_RESTORE=true y use el script CLI.",
        )
    try:
        response = requests.post(
            f"{BACKUP_URL}/restore",
            json={"filename": payload.filename, "latest": payload.latest},
            timeout=600,
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text[:1000])
        return {"ok": True, "restore": response.json()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"No se pudo contactar el servicio de respaldo: {exc}")
