"""Colector periódico de estado, incidentes y eventos reales.

El colector hace dos cosas distintas y las registra por separado:
1. Disponibilidad del SERVICIO: consulta PostgreSQL a través de HAProxy y exige
   que el nodo atendido sea primario (acepte escrituras).
2. Estado de los NODOS: consulta la API REST de Patroni y el clúster de etcd
   (a través de Patroni) para conocer roles, estados y retraso de replicación.

Así, la caída de un nodo no se confunde automáticamente con la indisponibilidad
del servicio, y viceversa.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import psycopg2
import requests

import store
from kpi import evaluate_rpo

log = logging.getLogger("clusterdb.monitor")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def utc_now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def normalize_role(value: str | None) -> str:
    if not value:
        return "unknown"
    role = value.lower()
    if role.startswith("primary") or role in {"leader", "master"}:
        return "primary"
    if role in {"replica", "sync_standby", "standby_leader", "quorum_leader"}:
        return "replica"
    return role


class Collector:
    def __init__(self) -> None:
        self.poll_seconds = _env_float("MONITOR_POLL_SECONDS", 2.0)
        self.retention_days = _env_int("MONITOR_RETENTION_DAYS", 30)
        self.lag_alert_bytes = _env_int("REPLICATION_LAG_ALERT_BYTES", 5 * 1024 * 1024)
        self.rto_target = _env_float("RTO_TARGET_SECONDS", 30.0)

        self.db_host = os.getenv("DB_HOST", "haproxy")
        self.db_port = _env_int("DB_PORT", 5432)
        self.db_name = os.getenv("DB_NAME", "cluster_demo")
        self.db_user = os.getenv("DB_USER", "monitor")
        self.db_password = os.getenv("DB_PASSWORD", "")
        self.replica_host = os.getenv("REPLICA_HOST", "postgres-replica")
        self.replica_port = _env_int("REPLICA_PORT", 5432)

        self.patroni_urls = {
            "postgres-master": os.getenv("PATRONI_MASTER_URL", "http://postgres-master:8008"),
            "postgres-replica": os.getenv("PATRONI_REPLICA_URL", "http://postgres-replica:8008"),
        }
        self.backup_url = os.getenv("BACKUP_URL", "http://postgres-backup:8080").rstrip("/")
        self.generator_stats_path = os.getenv("GENERATOR_STATS_PATH", "/generator-data/stats.json")
        self.generator_attempts_path = os.getenv("GENERATOR_ATTEMPTS_PATH", "/generator-data/attempts.jsonl")

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "clusterdb-monitor/1.0"})

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at = utc_now()
        self.last_poll_at: float | None = None
        self.last_error: str | None = None

        self.initialized = False
        self.prev_roles: dict[str, str] = {}
        self.prev_states: dict[str, str] = {}
        self.open_incident: dict[str, Any] | None = None
        self.failure_started_at: float | None = None
        self.failure_started_iso: str | None = None
        self.consecutive_failures = 0
        # Un failover real puede durar menos que dos ciclos de sondeo. Con el
        # umbral en 1 no se pierde el incidente; el tiempo se mide con la
        # diferencia entre la primera muestra no disponible y la recuperación.
        self.failure_threshold = max(1, _env_int("FAILURE_THRESHOLD_SAMPLES", 1))
        self.last_known_hb_id: int | None = None
        self.last_known_hb_at: str | None = None
        self.last_known_venta_id: int | None = None
        self.pending_failover_id: int | None = None
        self.last_closed_incident: dict[str, Any] | None = None
        self.lag_alert = False
        self.last_seen_backup_sequence: int | None = None
        self.last_primary: str | None = None
        self.last_nodes: list[dict[str, Any]] = self._unknown_nodes()

        store.init_db()
        self._restore_state()

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="clusterdb-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        try:
            store.insert_event(utc_now(), iso(utc_now()), "MONITOR_STOPPED", "monitor",
                               "Servicio de monitoreo detenido.", result="info",
                               severity="info")
        except Exception:
            pass

    def runtime_info(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "started_iso": iso(self.started_at),
            "uptime_seconds": round(utc_now() - self.started_at, 3),
            "last_poll_at": self.last_poll_at,
            "last_poll_iso": iso(self.last_poll_at) if self.last_poll_at else None,
            "last_error": self.last_error,
            "poll_seconds": self.poll_seconds,
            "db_path": store.DB_PATH,
            "samples": store.count_samples(),
        }

    def _run(self) -> None:
        log.info("Colector iniciado (poll=%.1fs)", self.poll_seconds)
        while not self._stop.is_set():
            started = utc_now()
            try:
                self._poll()
                self.last_error = None
            except Exception as exc:  # nunca debe morir el hilo
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Error en el ciclo de monitoreo")
                try:
                    self._insert_failure_sample(started, self.last_error)
                except Exception:
                    log.exception("No se pudo guardar la muestra de error")
            elapsed = utc_now() - started
            self._stop.wait(max(0.2, self.poll_seconds - elapsed))

    def _restore_state(self) -> None:
        last = store.latest_sample()
        if last:
            self.initialized = True
            self._set_previous_nodes(last.get("nodes") or [])
            self.last_known_hb_id = last.get("last_hb_id")
            self.last_known_venta_id = last.get("last_venta_id")
            self.last_primary = last.get("primary_name")
        self.open_incident = store.get_open_incident()
        if self.open_incident:
            self.failure_started_at = self.open_incident["started_at"]
            self.failure_started_iso = self.open_incident["started_iso"]
            self.last_known_hb_id = self.open_incident.get("last_ack_id")

        last_fo = store.last_failover()
        if last_fo and last_fo.get("recovery_s") is None:
            self.pending_failover_id = int(last_fo["id"])

        seq = store.get_meta("last_seen_backup_sequence")
        if seq is not None:
            try:
                self.last_seen_backup_sequence = int(seq)
            except ValueError:
                pass

    def _set_previous_nodes(self, nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            name = node.get("name")
            if not name:
                continue
            if node.get("role") in {"primary", "replica"} and node.get("state") == "running":
                self.prev_roles[name] = node["role"]
            if node.get("state"):
                self.prev_states[name] = node["state"]

    # ------------------------------------------------------------------
    # Recolección
    # ------------------------------------------------------------------
    def _poll(self) -> None:
        now = utc_now()
        service = self._probe_service(now)
        nodes, cluster_info = self._probe_patroni(now)
        backup = self._probe_backup()
        stats = self._read_generator_stats()
        attempts = 0
        try:
            attempts = store.ingest_generator_attempts(self.generator_attempts_path)
        except Exception as exc:
            log.warning("No se pudieron ingerir intentos del generador: %s", exc)

        primary = next((n["name"] for n in nodes if n.get("role") == "primary"
                        and n.get("state") == "running"), None)
        if primary:
            self.last_primary = primary

        replication = service.get("replication") or {}
        if not replication.get("state"):
            # Fallback: datos del clúster de Patroni.
            replica_node = next(
                (n for n in nodes if n.get("name") == "postgres-replica"), None
            )
            if replica_node and replica_node.get("role") == "replica":
                replication = {
                    "state": "streaming" if replica_node.get("state") == "running" else "unknown",
                    "lag_bytes": replica_node.get("lag_bytes"),
                    "lag_seconds": None,
                    "source": "patroni",
                }

        sample = {
            "ts": now,
            "ts_iso": iso(now),
            "available": bool(service.get("available")),
            "latency_ms": service.get("latency_ms"),
            "in_recovery": service.get("in_recovery"),
            "db_name": service.get("db_name"),
            "server_version": service.get("server_version"),
            "primary_name": primary or self.last_primary,
            "replication_state": replication.get("state"),
            "replication_lag_bytes": replication.get("lag_bytes"),
            "last_hb_id": service.get("last_hb_id") if service.get("available") else self.last_known_hb_id,
            "last_venta_id": service.get("last_venta_id") if service.get("available") else self.last_known_venta_id,
            "nodes": nodes,
            "replication": replication,
            "backup": backup,
            "error": service.get("error"),
        }
        store.insert_sample(sample)
        self.last_poll_at = now
        self.last_nodes = nodes

        if service.get("available"):
            self.last_known_hb_id = service.get("last_hb_id") or self.last_known_hb_id
            self.last_known_hb_at = service.get("last_hb_at") or self.last_known_hb_at
            self.last_known_venta_id = service.get("last_venta_id") or self.last_known_venta_id

        if self.initialized:
            self._process_events(now, service, nodes, backup, replication)
        else:
            # Primera muestra: fija el estado base sin generar falsos eventos.
            if service.get("available") and self.open_incident:
                self._close_incident(now, "Incidente abierto cerrado tras reinicio del monitor.")
            self.initialized = True
            if self.last_seen_backup_sequence is None:
                self._remember_backup_sequence(backup)
            if not store.get_meta("monitor_ever_started"):
                store.insert_event(
                    now, iso(now), "MONITOR_STARTED", "monitor",
                    "Monitor iniciado; comenzando a recolectar métricas reales.",
                    result="success", severity="info",
                )
                store.set_meta("monitor_ever_started", "1")

        self._set_previous_nodes(nodes)
        self.prev_states = {n["name"]: n.get("state", "unknown") for n in nodes if n.get("name")}
        self.last_primary = primary or self.last_primary
        self._update_lag_alert(now, replication)
        self._process_backup(now, backup)

        if attempts:
            log.debug("Intentos del generador ingestados: %s", attempts)

        if int(now) % 600 < int(self.poll_seconds) + 1:
            try:
                store.prune(self.retention_days)
            except Exception:
                log.exception("No se pudo aplicar la retención de muestras")

    # ------------------------------------------------------------------
    # Sondas
    # ------------------------------------------------------------------
    def _probe_service(self, now: float) -> dict[str, Any]:
        started = time.perf_counter()
        result: dict[str, Any] = {
            "available": False,
            "latency_ms": None,
            "in_recovery": None,
            "db_name": None,
            "server_version": None,
            "last_hb_id": None,
            "last_hb_at": None,
            "last_venta_id": None,
            "replication": {},
            "error": None,
        }
        conn = None
        try:
            conn = psycopg2.connect(
                host=self.db_host, port=self.db_port, dbname=self.db_name,
                user=self.db_user, password=self.db_password,
                connect_timeout=2, application_name="clusterdb-monitor",
            )
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 1500")
                cur.execute(
                    """
                    SELECT pg_is_in_recovery(),
                           current_database(),
                           current_setting('server_version'),
                           clock_timestamp(),
                           (SELECT count(*) FROM ventas),
                           (SELECT max(id) FROM ha_heartbeat),
                           (SELECT created_at FROM ha_heartbeat ORDER BY id DESC LIMIT 1),
                           (SELECT max(id) FROM ventas)
                    """
                )
                row = cur.fetchone()
                in_recovery = bool(row[0])
                result.update({
                    "in_recovery": in_recovery,
                    "db_name": row[1],
                    "server_version": row[2],
                    "last_hb_id": row[5],
                    "last_hb_at": row[6].isoformat() if row[6] else None,
                    "last_venta_id": row[7],
                })
                result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 2)

                if in_recovery:
                    result["available"] = False
                    result["error"] = (
                        "HAProxy respondió desde una réplica; no hay primario "
                        "disponible para escrituras."
                    )
                    return result

                result["available"] = True
                cur.execute(
                    """
                    SELECT application_name, client_addr::text, state, sync_state,
                           pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint,
                           EXTRACT(EPOCH FROM replay_lag)::double precision
                      FROM pg_stat_replication
                     ORDER BY application_name
                    """
                )
                stands = []
                for srow in cur.fetchall():
                    stands.append({
                        "application_name": srow[0],
                        "client_addr": srow[1],
                        "state": srow[2],
                        "sync_state": srow[3],
                        "lag_bytes": max(0, int(srow[4] or 0)) if srow[4] is not None else None,
                        "lag_seconds": round(float(srow[5]), 3) if srow[5] is not None else None,
                    })
                if stands:
                    primary_stand = next(
                        (s for s in stands if s["state"] == "streaming"), stands[0]
                    )
                    lag = max((s["lag_bytes"] or 0) for s in stands)
                    result["replication"] = {
                        "state": "streaming" if any(s["state"] == "streaming" for s in stands) else "degraded",
                        "sync_state": primary_stand.get("sync_state"),
                        "lag_bytes": lag,
                        "lag_seconds": primary_stand.get("lag_seconds"),
                        "standbys": stands,
                        "source": "pg_stat_replication",
                    }
                else:
                    result["replication"] = {
                        "state": "none",
                        "sync_state": None,
                        "lag_bytes": None,
                        "lag_seconds": None,
                        "standbys": [],
                        "source": "pg_stat_replication",
                    }
        except Exception as exc:
            result["available"] = False
            result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
            result["error"] = f"{type(exc).__name__}: {str(exc).strip()[:500]}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return result

    def _probe_patroni(self, now: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cluster_info: dict[str, Any] = {}
        cluster_error = None
        for url in self.patroni_urls.values():
            try:
                data = self._get_json(url + "/cluster", timeout=1.5)
                if isinstance(data, dict) and "members" in data:
                    cluster_info = data
                    break
            except Exception as exc:
                cluster_error = str(exc)

        members = {m.get("name"): m for m in cluster_info.get("members", []) if m.get("name")}

        def fetch(name: str, base: str) -> tuple[str, dict[str, Any] | None, str | None]:
            try:
                data = self._get_json(base + "/patroni", timeout=1.5, allow_error_body=True)
                return name, data if isinstance(data, dict) else None, None
            except Exception as exc:
                return name, None, str(exc)

        responses: dict[str, dict[str, Any] | None] = {}
        errors: dict[str, str | None] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(fetch, name, base)
                for name, base in self.patroni_urls.items()
            ]
            for future in futures:
                try:
                    name, data, error = future.result(timeout=3)
                    responses[name] = data
                    errors[name] = error
                except Exception as exc:
                    log.debug("Patroni timeout: %s", exc)

        nodes = []
        labels = {"postgres-master": "Master", "postgres-replica": "HA"}
        for name in ("postgres-master", "postgres-replica"):
            info = responses.get(name) or {}
            member = members.get(name) or {}
            role = normalize_role(info.get("role") or member.get("role"))
            state = info.get("state") or member.get("state") or "unknown"
            # Patroni reporta "running" cuando el postmaster está arriba.
            if state in {"running", "streaming"}:
                state = "running"
            postmaster_ts = parse_iso(info.get("postmaster_start_time"))
            uptime = max(0.0, now - postmaster_ts) if postmaster_ts else None
            api_ok = bool(info)
            if not member and not info:
                state = "unknown"
            nodes.append({
                "name": name,
                "label": labels[name],
                "role": role,
                "state": state,
                "api_ok": api_ok,
                "uptime_seconds": round(uptime, 1) if uptime is not None else None,
                "postmaster_start_time": info.get("postmaster_start_time"),
                "lag_bytes": member.get("lag"),
                "timeline": member.get("timeline"),
                "api_error": errors.get(name),
                "connection": "ok" if api_ok else "sin conexión",
            })

        return nodes, {"cluster": cluster_info, "error": cluster_error}

    def _probe_backup(self) -> dict[str, Any]:
        try:
            data = self._get_json(self.backup_url + "/status", timeout=2.0)
            if not isinstance(data, dict):
                raise ValueError("respuesta inválida")
            data["reachable"] = True
            return data
        except Exception as exc:
            return {
                "reachable": False,
                "service": "unreachable",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "last_backup": None,
                "last_attempt": None,
            }

    def _read_generator_stats(self) -> dict[str, Any] | None:
        try:
            with open(self.generator_stats_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data["file_age_seconds"] = round(
                    max(0.0, utc_now() - float(data.get("updated_epoch", 0))), 1
                )
                return data
        except (OSError, ValueError, TypeError):
            return None
        return None

    def _get_json(self, url: str, timeout: float, allow_error_body: bool = False) -> Any:
        response = self.session.get(url, timeout=timeout)
        if not allow_error_body:
            response.raise_for_status()
            return response.json()
        try:
            return response.json()
        except ValueError:
            response.raise_for_status()
            raise

    # ------------------------------------------------------------------
    # Eventos e incidentes
    # ------------------------------------------------------------------
    def _process_events(self, now: float, service: dict[str, Any],
                        nodes: list[dict[str, Any]], backup: dict[str, Any],
                        replication: dict[str, Any]) -> None:
        available = bool(service.get("available"))

        if not available:
            self.consecutive_failures += 1
            if self.failure_started_at is None:
                self.failure_started_at = now
                self.failure_started_iso = iso(now)
            if self.open_incident is None and self.consecutive_failures >= self.failure_threshold:
                failed_node = self._node_for_primary(self.last_primary)
                self.open_incident = {
                    "id": store.open_incident(
                        start=self.failure_started_at,
                        start_iso=self.failure_started_iso or iso(self.failure_started_at),
                        detected=now,
                        detected_iso=iso(now),
                        failed_node=failed_node,
                        last_ack_id=self.last_known_hb_id,
                        last_ack_at=parse_iso(self.last_known_hb_at),
                        last_ack_iso=self.last_known_hb_at,
                        notes=service.get("error") or "Servicio no disponible a través de HAProxy.",
                    ),
                    "started_at": self.failure_started_at,
                    "started_iso": self.failure_started_iso,
                    "detected_at": now,
                    "detected_iso": iso(now),
                    "last_ack_id": self.last_known_hb_id,
                    "last_ack_at": parse_iso(self.last_known_hb_at),
                    "last_ack_iso": self.last_known_hb_at,
                    "notes": service.get("error"),
                }
                self.open_incident = store.get_open_incident() or self.open_incident
                store.insert_event(
                    now, iso(now), "SERVICE_DOWN", failed_node or "haproxy",
                    "Servicio PostgreSQL no disponible para escrituras a través de HAProxy. "
                    f"Detalle: {service.get('error') or 'sin detalle'}",
                    result="failed", severity="critical",
                    metadata={"last_ack_id": self.last_known_hb_id},
                )
        else:
            self.consecutive_failures = 0
            self.failure_started_at = None
            self.failure_started_iso = None
            if self.open_incident is not None:
                self._close_incident(now, "")

        # Transiciones de estado y rol por nodo.
        current_states = {n["name"]: n.get("state", "unknown") for n in nodes}
        for node in nodes:
            name = node["name"]
            previous_state = self.prev_states.get(name)
            current_state = current_states.get(name, "unknown")
            if previous_state == "running" and current_state != "running":
                store.insert_event(
                    now, iso(now), "NODE_DOWN", node["label"],
                    f"{node['label']} ({name}) dejó de reportarse como activo. "
                    f"Estado Patroni: {current_state}.",
                    result="warning", severity="warning",
                    metadata={"state": current_state},
                )
            elif previous_state and previous_state != "running" and current_state == "running":
                store.insert_event(
                    now, iso(now), "NODE_UP", node["label"],
                    f"{node['label']} ({name}) volvió a estar activo. Rol: {node.get('role')}.",
                    result="success", severity="info",
                    metadata={"role": node.get("role")},
                )

            previous_role = self.prev_roles.get(name)
            current_role = node.get("role")
            if current_state == "running" and current_role in {"primary", "replica"}:
                if previous_role == "primary" and current_role == "replica":
                    store.insert_event(
                        now, iso(now), "NODE_REJOINED", node["label"],
                        f"{node['label']} ({name}) dejó de ser primario y se reincorporó "
                        "como réplica bajo coordinación de Patroni.",
                        result="success", severity="warning",
                    )
                if previous_role != "primary" and current_role == "primary":
                    self._record_promotion(now, name, nodes)

        # Actualiza roles conocidos solo cuando el rol es confiable.
        for node in nodes:
            if node.get("state") == "running" and node.get("role") in {"primary", "replica"}:
                self.prev_roles[node["name"]] = node["role"]

    def _record_promotion(self, now: float, new_primary: str,
                          nodes: list[dict[str, Any]]) -> None:
        old_primary = next(
            (name for name, role in self.prev_roles.items()
             if role == "primary" and name != new_primary),
            self.last_primary,
        )
        recovery = None
        incident_id = None
        if self.open_incident:
            recovery = round(now - float(self.open_incident["started_at"]), 3)
            incident_id = int(self.open_incident["id"])
        elif self.last_closed_incident and now - float(self.last_closed_incident["ended_at"]) <= 120:
            recovery = float(self.last_closed_incident["duration_s"])
            incident_id = int(self.last_closed_incident["id"])
        elif self.failure_started_at is not None:
            # La promoción llegó antes de abrir/cerrar el incidente formal.
            recovery = round(now - float(self.failure_started_at), 3)

        failover_id = store.insert_failover(
            detected=now,
            detected_iso=iso(now),
            old_primary=old_primary,
            new_primary=new_primary,
            recovery_s=recovery,
            rto_target=self.rto_target,
            incident_id=incident_id,
            notes="Promoción detectada mediante la API REST de Patroni.",
        )
        self.pending_failover_id = failover_id
        if incident_id:
            store.update_failover_incident(failover_id, incident_id)

        label = "HA" if new_primary == "postgres-replica" else "Master"
        store.insert_event(
            now, iso(now), "REPLICA_PROMOTED", label,
            f"Patroni promovió {new_primary} a primario. "
            f"Primario anterior: {old_primary or 'desconocido'}.",
            result="success", severity="critical",
            metadata={"old_primary": old_primary, "new_primary": new_primary},
        )

        if recovery is not None and self.open_incident is None:
            # La promoción se detectó después de recuperarse el servicio.
            self._finalize_failover_rpo(failover_id, incident_id, recovery,
                                        self.last_closed_incident)

    def _close_incident(self, now: float, extra_note: str) -> None:
        incident = self.open_incident
        if not incident:
            return
        duration = max(0.0, now - float(incident["started_at"]))
        new_primary = self._current_primary_name()
        rpo = self._verify_rpo(incident)

        notes = (incident.get("notes") or "").strip()
        if extra_note:
            notes = f"{notes} {extra_note}".strip()
        if rpo.get("notes"):
            notes = f"{notes} {rpo['notes']}".strip()

        store.close_incident(
            int(incident["id"]), now, iso(now), round(duration, 3), new_primary,
            bool(rpo.get("verified")), rpo.get("lost_records"),
            rpo.get("seconds"), notes,
        )
        store.insert_event(
            now, iso(now), "SERVICE_RECOVERED",
            self._node_for_primary(new_primary) or "servicio",
            "Servicio PostgreSQL disponible nuevamente a través de HAProxy. "
            f"Primario activo: {new_primary or 'no identificado'}.",
            duration_s=round(duration, 3), result="success", severity="info",
            metadata={"new_primary": new_primary, "recovery_s": round(duration, 3)},
        )
        if rpo.get("verified"):
            store.insert_event(
                now, iso(now), "RPO_VERIFIED", "servicio",
                "Verificación de RPO posterior al failover: "
                + ("0 registros confirmados perdidos." if rpo.get("lost_records") == 0
                   else f"pérdida confirmada de al menos {rpo.get('lost_records')} registro(s)."),
                result="success" if rpo.get("lost_records") == 0 else "warning",
                severity="info" if rpo.get("lost_records") == 0 else "warning",
                metadata={"last_ack_id": incident.get("last_ack_id"),
                          "lost_records": rpo.get("lost_records")},
            )

        closed = dict(incident)
        closed.update({
            "ended_at": now, "ended_iso": iso(now), "duration_s": round(duration, 3),
            "new_primary": new_primary,
            "rpo_verified": bool(rpo.get("verified")),
            "rpo_lost_records": rpo.get("lost_records"),
            "rpo_observed_seconds": rpo.get("seconds"),
        })
        self.last_closed_incident = closed

        if self.pending_failover_id:
            self._finalize_failover_rpo(
                self.pending_failover_id, int(incident["id"]), duration, closed
            )
            self.pending_failover_id = None

        self.open_incident = None
        self.failure_started_at = None
        self.failure_started_iso = None
        self.consecutive_failures = 0

    def _finalize_failover_rpo(self, failover_id: int, incident_id: int | None,
                               recovery_s: float, incident: dict[str, Any] | None) -> None:
        if incident_id is None:
            return
        rpo = self._verify_rpo(incident or {})
        seconds = rpo.get("seconds")
        store.update_failover_recovery(
            failover_id, recovery_s, self.rto_target, incident_id,
            bool(rpo.get("verified")), rpo.get("lost_records"), seconds,
        )

    def _verify_rpo(self, incident: dict[str, Any]) -> dict[str, Any]:
        last_ack_id = incident.get("last_ack_id")
        if last_ack_id is None:
            return {"verified": False, "lost_records": None, "seconds": None,
                    "notes": "No había un latido confirmado antes del incidente."}

        last_error = None
        for attempt in range(3):
            conn = None
            try:
                conn = psycopg2.connect(
                    host=self.db_host, port=self.db_port, dbname=self.db_name,
                    user=self.db_user, password=self.db_password,
                    connect_timeout=2, application_name="clusterdb-rpo-check",
                )
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = 2000")
                    cur.execute("SELECT id FROM ha_heartbeat WHERE id = %s", (last_ack_id,))
                    exists = cur.fetchone() is not None
                if exists:
                    return {
                        "verified": True, "lost_records": 0, "seconds": 0.0,
                        "notes": f"Latido confirmado id={last_ack_id} presente en el nodo promovido.",
                    }
                return {
                    "verified": True, "lost_records": 1, "seconds": None,
                    "notes": (
                        f"El latido confirmado id={last_ack_id} no existe en el nodo "
                        "promovido; hay pérdida confirmada de al menos un registro."
                    ),
                }
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.5)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        return {"verified": False, "lost_records": None, "seconds": None,
                "notes": f"No fue posible verificar RPO: {last_error}"}

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------
    def _update_lag_alert(self, now: float, replication: dict[str, Any]) -> None:
        lag = replication.get("lag_bytes")
        if lag is None:
            return
        if lag >= self.lag_alert_bytes and not self.lag_alert:
            self.lag_alert = True
            store.insert_event(
                now, iso(now), "REPLICATION_DEGRADED", "HA",
                f"Retraso de replicación de {int(lag)} bytes supera el umbral "
                f"de {self.lag_alert_bytes} bytes.",
                result="warning", severity="warning",
                metadata={"lag_bytes": int(lag)},
            )
        elif lag < max(1, self.lag_alert_bytes // 2) and self.lag_alert:
            self.lag_alert = False
            store.insert_event(
                now, iso(now), "REPLICATION_RECOVERED", "HA",
                "El retraso de replicación volvió por debajo del umbral.",
                result="success", severity="info",
                metadata={"lag_bytes": int(lag)},
            )

    def _process_backup(self, now: float, backup: dict[str, Any]) -> None:
        attempt = backup.get("last_attempt") or {}
        if attempt.get("status") == "running":
            return
        sequence = attempt.get("sequence")
        if sequence is None:
            return
        try:
            sequence = int(sequence)
        except (TypeError, ValueError):
            return
        if self.last_seen_backup_sequence is None:
            self._remember_backup_sequence(backup)
            return
        if sequence <= self.last_seen_backup_sequence:
            return

        success = attempt.get("status") == "success"
        filename = attempt.get("filename") or "sin archivo"
        size = attempt.get("size_bytes")
        description = (
            f"Respaldo completado: {filename}"
            + (f" ({size} bytes)." if size is not None else ".")
            if success else
            f"Respaldo fallido: {attempt.get('error') or 'sin detalle'}."
        )
        store.insert_event(
            now, iso(now), "BACKUP_COMPLETED" if success else "BACKUP_FAILED",
            "Backup", description,
            duration_s=attempt.get("duration_s"),
            result="success" if success else "failed",
            severity="info" if success else "warning",
            metadata={"filename": filename, "size_bytes": size,
                      "verification": attempt.get("verification")},
        )
        self.last_seen_backup_sequence = sequence
        store.set_meta("last_seen_backup_sequence", sequence)

    def _remember_backup_sequence(self, backup: dict[str, Any]) -> None:
        attempt = backup.get("last_attempt") or {}
        try:
            self.last_seen_backup_sequence = int(attempt.get("sequence"))
            store.set_meta("last_seen_backup_sequence", self.last_seen_backup_sequence)
        except (TypeError, ValueError):
            pass

    def _insert_failure_sample(self, ts: float, error: str) -> None:
        store.insert_sample({
            "ts": ts, "ts_iso": iso(ts), "available": False,
            "latency_ms": None, "in_recovery": None, "db_name": self.db_name,
            "server_version": None, "primary_name": self.last_primary,
            "replication_state": None, "replication_lag_bytes": None,
            "last_hb_id": self.last_known_hb_id,
            "last_venta_id": self.last_known_venta_id,
            "nodes": self.last_nodes,
            "replication": {}, "backup": {},
            "error": error,
        })

    def _current_primary_name(self) -> str | None:
        try:
            last = store.latest_sample()
            for node in (last or {}).get("nodes") or []:
                if node.get("role") == "primary" and node.get("state") == "running":
                    return node.get("name")
        except Exception:
            pass
        return self.last_primary

    def _node_for_primary(self, name: str | None) -> str | None:
        if name == "postgres-master":
            return "Master"
        if name == "postgres-replica":
            return "HA"
        return name

    @staticmethod
    def _unknown_nodes() -> list[dict[str, Any]]:
        return [
            {"name": "postgres-master", "label": "Master", "role": "unknown",
             "state": "unknown", "api_ok": False, "uptime_seconds": None,
             "lag_bytes": None, "connection": "sin datos"},
            {"name": "postgres-replica", "label": "HA", "role": "unknown",
             "state": "unknown", "api_ok": False, "uptime_seconds": None,
             "lag_bytes": None, "connection": "sin datos"},
        ]
