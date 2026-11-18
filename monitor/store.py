"""Persistencia local del monitor en SQLite (volumen Docker dedicado).

SQLite se usa a propósito para que las métricas sobrevivan a reinicios del
clúster PostgreSQL y del propio monitor, sin depender de la base monitoreada.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable

from kpi import compute_availability

DB_PATH = os.getenv("MONITOR_DB_PATH", "/data/monitor.db")
_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    available INTEGER NOT NULL,
    latency_ms REAL,
    in_recovery INTEGER,
    db_name TEXT,
    server_version TEXT,
    primary_name TEXT,
    replication_state TEXT,
    replication_lag_bytes INTEGER,
    last_hb_id INTEGER,
    last_venta_id INTEGER,
    nodes_json TEXT,
    replication_json TEXT,
    backup_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    started_iso TEXT NOT NULL,
    detected_at REAL NOT NULL,
    detected_iso TEXT NOT NULL,
    ended_at REAL,
    ended_iso TEXT,
    duration_s REAL,
    kind TEXT NOT NULL DEFAULT 'unplanned',
    failed_node TEXT,
    new_primary TEXT,
    last_ack_id INTEGER,
    last_ack_at REAL,
    last_ack_iso TEXT,
    rpo_verified INTEGER NOT NULL DEFAULT 0,
    rpo_lost_records INTEGER,
    rpo_observed_seconds REAL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents (started_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    type TEXT NOT NULL,
    node TEXT,
    description TEXT NOT NULL,
    duration_s REAL,
    result TEXT NOT NULL DEFAULT 'info',
    severity TEXT NOT NULL DEFAULT 'info',
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS failovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at REAL NOT NULL,
    detected_iso TEXT NOT NULL,
    old_primary TEXT,
    new_primary TEXT NOT NULL,
    recovery_s REAL,
    rto_target_s REAL,
    rto_met INTEGER,
    rpo_verified INTEGER NOT NULL DEFAULT 0,
    rpo_lost_records INTEGER,
    rpo_seconds REAL,
    incident_id INTEGER,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_failovers_detected ON failovers (detected_at);

CREATE TABLE IF NOT EXISTS tx_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT UNIQUE,
    ts REAL NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL,
    venta_id INTEGER,
    idempotency_key TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tx_events_ts ON tx_events (ts);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def cursor(write: bool = False):
    with _LOCK:
        conn = connect()
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            yield cur
            if write:
                conn.execute("COMMIT")
        except Exception:
            if write:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _LOCK:
        conn = connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()


def insert_sample(sample: dict[str, Any]) -> int:
    with cursor(write=True) as cur:
        cur.execute(
            """
            INSERT INTO samples (
                ts, ts_iso, available, latency_ms, in_recovery, db_name,
                server_version, primary_name, replication_state,
                replication_lag_bytes, last_hb_id, last_venta_id, nodes_json,
                replication_json, backup_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample["ts"], sample["ts_iso"], int(bool(sample["available"])),
                sample.get("latency_ms"), _opt_int(sample.get("in_recovery")),
                sample.get("db_name"), sample.get("server_version"),
                sample.get("primary_name"), sample.get("replication_state"),
                _opt_int(sample.get("replication_lag_bytes")),
                _opt_int(sample.get("last_hb_id")),
                _opt_int(sample.get("last_venta_id")),
                _json(sample.get("nodes", [])),
                _json(sample.get("replication", {})),
                _json(sample.get("backup", {})),
                _truncate(sample.get("error")),
            ),
        )
        return int(cur.lastrowid)


def latest_sample() -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
    return _decorate_sample(_row_to_dict(row))


def previous_sample(before_ts: float) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM samples WHERE ts < ? ORDER BY ts DESC LIMIT 1",
            (before_ts,),
        )
        row = cur.fetchone()
    return _decorate_sample(_row_to_dict(row))


def samples_between(start: float, end: float, limit: int = 120000) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT ts, available, latency_ms, replication_lag_bytes,
                   replication_state, primary_name
            FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT ?
            """,
            (start, end, limit),
        )
        rows = [dict(row) for row in cur.fetchall()]
    return rows


def count_samples() -> int:
    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM samples")
        return int(cur.fetchone()["n"])


def first_sample_ts() -> float | None:
    with cursor() as cur:
        cur.execute("SELECT min(ts) AS ts FROM samples")
        return cur.fetchone()["ts"]


def open_incident(start: float, start_iso: str, detected: float, detected_iso: str,
                  failed_node: str | None, last_ack_id: int | None,
                  last_ack_at: float | None, last_ack_iso: str | None,
                  notes: str = "") -> int:
    with cursor(write=True) as cur:
        cur.execute(
            """
            INSERT INTO incidents (
                started_at, started_iso, detected_at, detected_iso, kind,
                failed_node, last_ack_id, last_ack_at, last_ack_iso, notes
            ) VALUES (?, ?, ?, ?, 'unplanned', ?, ?, ?, ?, ?)
            """,
            (start, start_iso, detected, detected_iso, failed_node,
             last_ack_id, last_ack_at, last_ack_iso, notes),
        )
        return int(cur.lastrowid)


def close_incident(incident_id: int, ended: float, ended_iso: str,
                   duration_s: float, new_primary: str | None,
                   rpo_verified: bool, rpo_lost: int | None,
                   rpo_seconds: float | None, notes: str = "") -> None:
    with cursor(write=True) as cur:
        cur.execute(
            """
            UPDATE incidents
               SET ended_at = ?, ended_iso = ?, duration_s = ?,
                   new_primary = COALESCE(?, new_primary),
                   rpo_verified = ?, rpo_lost_records = ?,
                   rpo_observed_seconds = ?, notes = ?
             WHERE id = ?
            """,
            (ended, ended_iso, duration_s, new_primary, int(rpo_verified),
             rpo_lost, rpo_seconds, notes, incident_id),
        )


def set_incident_rpo(incident_id: int, verified: bool, lost: int | None,
                     seconds: float | None, notes: str = "") -> None:
    with cursor(write=True) as cur:
        cur.execute(
            """
            UPDATE incidents
               SET rpo_verified = ?, rpo_lost_records = ?,
                   rpo_observed_seconds = ?, notes = ?
             WHERE id = ?
            """,
            (int(verified), lost, seconds, notes, incident_id),
        )


def get_open_incident() -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM incidents WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
        return _row_to_dict(cur.fetchone())


def incidents_between(start: float, end: float) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT * FROM incidents
             WHERE started_at >= ? AND started_at <= ?
             ORDER BY started_at ASC
            """,
            (start, end),
        )
        return [dict(row) for row in cur.fetchall()]


def recent_incidents(limit: int = 100) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute("SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]


def insert_failover(detected: float, detected_iso: str, old_primary: str | None,
                    new_primary: str, recovery_s: float | None,
                    rto_target: float, incident_id: int | None,
                    notes: str = "") -> int:
    with cursor(write=True) as cur:
        cur.execute(
            """
            INSERT INTO failovers (
                detected_at, detected_iso, old_primary, new_primary,
                recovery_s, rto_target_s, rto_met, incident_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (detected, detected_iso, old_primary, new_primary, recovery_s,
             rto_target, None if recovery_s is None else int(recovery_s <= rto_target),
             incident_id, notes),
        )
        return int(cur.lastrowid)


def update_failover_recovery(failover_id: int, recovery_s: float,
                             rto_target: float, incident_id: int | None,
                             rpo_verified: bool, rpo_lost: int | None,
                             rpo_seconds: float | None) -> None:
    with cursor(write=True) as cur:
        cur.execute(
            """
            UPDATE failovers
               SET recovery_s = ?, rto_met = ?, incident_id = COALESCE(?, incident_id),
                   rpo_verified = ?, rpo_lost_records = ?, rpo_seconds = ?
             WHERE id = ?
            """,
            (recovery_s, int(recovery_s <= rto_target), incident_id,
             int(rpo_verified), rpo_lost, rpo_seconds, failover_id),
        )


def update_failover_incident(failover_id: int, incident_id: int) -> None:
    with cursor(write=True) as cur:
        cur.execute("UPDATE failovers SET incident_id = ? WHERE id = ?",
                    (incident_id, failover_id))


def last_failover() -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM failovers ORDER BY detected_at DESC LIMIT 1")
        return _row_to_dict(cur.fetchone())


def failovers_between(start: float, end: float) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM failovers WHERE detected_at >= ? AND detected_at <= ? ORDER BY detected_at",
            (start, end),
        )
        return [dict(row) for row in cur.fetchall()]


def insert_event(ts: float, ts_iso: str, event_type: str, node: str | None,
                 description: str, duration_s: float | None = None,
                 result: str = "info", severity: str = "info",
                 metadata: dict[str, Any] | None = None) -> int:
    with cursor(write=True) as cur:
        cur.execute(
            """
            INSERT INTO events (ts, ts_iso, type, node, description, duration_s,
                                result, severity, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, ts_iso, event_type, node, description, duration_s, result,
             severity, _json(metadata or {})),
        )
        return int(cur.lastrowid)


def events(start: float, end: float, limit: int = 300,
           event_type: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM events WHERE ts >= ? AND ts <= ?"
    params: list[Any] = [start, end]
    if event_type:
        query += " AND type = ?"
        params.append(event_type)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with cursor() as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def insert_tx_event(attempt_id: str, ts: float, success: bool,
                    latency_ms: float | None, venta_id: int | None,
                    idempotency_key: str | None, error: str | None) -> None:
    with cursor(write=True) as cur:
        cur.execute(
            """
            INSERT OR IGNORE INTO tx_events (
                attempt_id, ts, success, latency_ms, venta_id, idempotency_key, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (attempt_id, ts, int(success), latency_ms, venta_id,
             idempotency_key, _truncate(error)),
        )


def tx_counts(start: float, end: float) -> dict[str, int]:
    with cursor() as cur:
        cur.execute(
            """
            SELECT
              sum(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok,
              sum(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed,
              count(*) AS total
            FROM tx_events WHERE ts >= ? AND ts <= ?
            """,
            (start, end),
        )
        row = dict(cur.fetchone())
    return {
        "ok": int(row["ok"] or 0),
        "failed": int(row["failed"] or 0),
        "total": int(row["total"] or 0),
    }


def tx_series(start: float, end: float, bucket_seconds: int) -> list[dict[str, Any]]:
    bucket = max(1, int(bucket_seconds))
    with cursor() as cur:
        cur.execute(
            """
            SELECT CAST(ts / ? AS INTEGER) * ? AS bucket,
                   sum(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok,
                   sum(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed
            FROM tx_events WHERE ts >= ? AND ts <= ?
            GROUP BY bucket ORDER BY bucket
            """,
            (bucket, bucket, start, end),
        )
        return [
            {"ts": float(row["bucket"]), "ok": int(row["ok"] or 0),
             "failed": int(row["failed"] or 0)}
            for row in cur.fetchall()
        ]


def get_meta(key: str, default: str | None = None) -> str | None:
    with cursor() as cur:
        cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: Any) -> None:
    with cursor(write=True) as cur:
        cur.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def ingest_generator_attempts(path: str) -> int:
    """Lee intentos nuevos del JSONL del generador y los persiste.

    Se guarda un offset en la tabla meta. Si el archivo se truncó o rotó, el
    offset se reinicia. attempt_id es UNIQUE, por lo que no se duplican filas.
    """
    if not path or not os.path.exists(path):
        return 0

    offset = int(get_meta("generator_offset", "0") or 0)
    size = os.path.getsize(path)
    if size < offset:
        offset = 0

    inserted = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
        if not data:
            return 0
        end = data.rfind(b"\n")
        if end < 0:
            return 0
        complete = data[: end + 1]
        new_offset = offset + len(complete)

        for raw_line in complete.splitlines()[:2000]:
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            attempt_id = str(
                item.get("attempt_id")
                or hashlib.sha1(line).hexdigest()
            )
            ts = item.get("epoch")
            if ts is None:
                try:
                    ts = float(item.get("ts"))
                except (TypeError, ValueError):
                    ts = 0.0
            insert_tx_event(
                attempt_id=attempt_id,
                ts=float(ts),
                success=bool(item.get("success")),
                latency_ms=_opt_float(item.get("latency_ms")),
                venta_id=_opt_int(item.get("venta_id")),
                idempotency_key=item.get("idempotency_key"),
                error=item.get("error"),
            )
            inserted += 1

    set_meta("generator_offset", new_offset)
    return inserted


def prune(retention_days: int) -> int:
    cutoff = _now() - max(1, retention_days) * 86400
    with cursor(write=True) as cur:
        cur.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount
    return int(deleted or 0)


def _now() -> float:
    import time
    return time.time()


def _decorate_sample(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    for key in ("nodes_json", "replication_json", "backup_json"):
        try:
            row[key.replace("_json", "")] = json.loads(row.get(key) or "null")
        except json.JSONDecodeError:
            row[key.replace("_json", "")] = None
        row.pop(key, None)
    return row


def series_from_samples(samples: list[dict[str, Any]], bucket_seconds: int) -> list[dict[str, Any]]:
    """Agrupa muestras para gráficos sin inventar puntos."""
    buckets: dict[int, list[dict[str, Any]]] = {}
    bucket = max(1, int(bucket_seconds))
    for sample in samples:
        key = int(float(sample["ts"]) // bucket) * bucket
        buckets.setdefault(key, []).append(sample)

    out = []
    for key in sorted(buckets):
        rows = buckets[key]
        avail = compute_availability(rows, poll_seconds=2.0, min_samples=1)
        lags = [r["replication_lag_bytes"] for r in rows
                if r.get("replication_lag_bytes") is not None]
        latencies = [r["latency_ms"] for r in rows
                     if r.get("latency_ms") is not None]
        out.append({
            "ts": float(key),
            "availability_pct": avail.get("pct"),
            "samples": len(rows),
            "available": sum(1 for r in rows if r.get("available")),
            "lag_bytes": max(lags) if lags else None,
            "lag_avg_bytes": (sum(lags) / len(lags)) if lags else None,
            "latency_ms_avg": (sum(latencies) / len(latencies)) if latencies else None,
            "latency_ms_max": max(latencies) if latencies else None,
        })
    return out


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _opt_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _truncate(value: Any, limit: int = 1200) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit]
