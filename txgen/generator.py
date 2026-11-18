"""Generador de transacciones idempotente a través de HAProxy.

Cada venta lógica usa un UUID de idempotencia generado UNA vez y reutilizado en
todos los reintentos. Si el commit se completó pero la conexión se perdió, el
reintento encuentra la fila existente y no duplica la venta ni su detalle.

Escribe estadísticas y un JSONL de intentos en un volumen compartido que el
monitor ingiere para calcular transacciones exitosas/fallidas.
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("clusterdb.txgen")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
STATS_FILE = DATA_DIR / "stats.json"
ATTEMPTS_FILE = DATA_DIR / "attempts.jsonl"
DB = {
    "host": os.getenv("DB_HOST", "haproxy"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "cluster_demo"),
    "user": os.getenv("DB_USER", "app"),
    "password": os.getenv("DB_PASSWORD", ""),
}
INTERVAL = float(os.getenv("TX_INTERVAL_SECONDS", "2"))
MAX_BACKOFF = float(os.getenv("TX_MAX_BACKOFF_SECONDS", "2"))
MAX_LOG_BYTES = 50 * 1024 * 1024

stop_event = threading.Event()


def _signal_handler(signum, _frame) -> None:
    log.info("Señal %s recibida; terminando de forma ordenada.", signum)
    stop_event.set()


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="milliseconds")


class Generator:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.successful = 0
        self.failed = 0
        self.retries = 0
        self.started_at = iso()
        self.session_started_at = iso()
        self.last_record: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_success_at: str | None = None
        self.state = "starting"
        self.consecutive_failures = 0
        self._load()

    def _load(self) -> None:
        try:
            with STATS_FILE.open("r", encoding="utf-8") as fh:
                old = json.load(fh)
            if isinstance(old, dict):
                self.successful = int(old.get("successful", 0))
                self.failed = int(old.get("failed", 0))
                self.retries = int(old.get("retries", 0))
                self.started_at = old.get("started_at") or self.started_at
                self.last_record = old.get("last_record")
                self.last_error = old.get("last_error")
                self.last_success_at = old.get("last_success_at")
                log.info("Contadores recuperados: ok=%s fallidas=%s",
                         self.successful, self.failed)
        except (OSError, ValueError, TypeError):
            pass

    def _write_stats(self, attempt: dict[str, Any] | None = None) -> None:
        now = utcnow()
        data = {
            "started_at": self.started_at,
            "session_started_at": self.session_started_at,
            "updated_at": iso(now),
            "updated_epoch": now.timestamp(),
            "successful": self.successful,
            "failed": self.failed,
            "retries": self.retries,
            "consecutive_failures": self.consecutive_failures,
            "state": self.state,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "last_record": self.last_record,
            "last_attempt": attempt,
            "database": DB["dbname"],
            "host": DB["host"],
            "interval_seconds": INTERVAL,
        }
        tmp = STATS_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STATS_FILE)

    def _append_attempt(self, attempt: dict[str, Any]) -> None:
        try:
            if ATTEMPTS_FILE.exists() and ATTEMPTS_FILE.stat().st_size > MAX_LOG_BYTES:
                old = ATTEMPTS_FILE.with_suffix(".jsonl.1")
                os.replace(ATTEMPTS_FILE, old)
            with ATTEMPTS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(attempt, ensure_ascii=False, default=str) + "\n")
                fh.flush()
        except OSError:
            log.exception("No se pudo escribir el registro de intentos")

    def _perform_sale(self, idempotency_key: str) -> dict[str, Any]:
        started = time.perf_counter()
        conn = psycopg2.connect(
            host=DB["host"], port=DB["port"], dbname=DB["dbname"],
            user=DB["user"], password=DB["password"], connect_timeout=3,
            application_name="clusterdb-txgen",
            options="-c statement_timeout=5000",
        )
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SELECT id, nombre FROM clientes ORDER BY random() LIMIT 1")
                cliente = cur.fetchone()
                if not cliente:
                    raise RuntimeError("No hay clientes en cluster_demo")
                cantidad = random.randint(1, 3)
                cur.execute(
                    """
                    SELECT id, sku, nombre, precio FROM productos
                     WHERE stock >= %s ORDER BY random() LIMIT 1
                    """,
                    (cantidad,),
                )
                producto = cur.fetchone()
                if not producto:
                    raise RuntimeError("No hay productos con stock suficiente")
                total = round(float(producto[3]) * cantidad, 2)

                cur.execute(
                    """
                    INSERT INTO ventas (idempotency_key, cliente_id, total, origen)
                    VALUES (%s, %s, %s, 'txgen')
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id, created_at
                    """,
                    (idempotency_key, cliente[0], total),
                )
                inserted_row = cur.fetchone()
                inserted = inserted_row is not None

                if inserted:
                    venta_id, created_at = inserted_row
                    cur.execute(
                        """
                        INSERT INTO detalle_ventas
                            (venta_id, producto_id, cantidad, precio_unitario)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (venta_id, producto[0], cantidad, producto[3]),
                    )
                    cur.execute(
                        "UPDATE productos SET stock = stock - %s "
                        "WHERE id = %s AND stock >= %s",
                        (cantidad, producto[0], cantidad),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("Stock insuficiente al confirmar la venta")
                    cur.execute(
                        "INSERT INTO ha_heartbeat (source, note) VALUES ('txgen', %s)",
                        (f"venta_id={venta_id}",),
                    )
                    conn.commit()
                else:
                    # Commit previo incierto: la fila ya existe. No se duplica
                    # el detalle ni se vuelve a descontar stock.
                    conn.rollback()
                    cur.execute(
                        "SELECT id, created_at FROM ventas WHERE idempotency_key = %s",
                        (idempotency_key,),
                    )
                    existing = cur.fetchone()
                    if not existing:
                        raise RuntimeError("Conflicto de idempotencia sin fila existente")
                    venta_id, created_at = existing

            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return {
                "venta_id": int(venta_id),
                "idempotency_key": idempotency_key,
                "cliente_id": int(cliente[0]),
                "cliente": cliente[1],
                "producto_id": int(producto[0]),
                "sku": producto[1],
                "producto": producto[2],
                "cantidad": cantidad,
                "total": total,
                "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
                "inserted": inserted,
                "latency_ms": latency_ms,
            }
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def run(self) -> None:
        self.state = "running"
        self._write_stats()
        log.info("Generador iniciado -> %s:%s/%s cada %.1fs",
                 DB["host"], DB["port"], DB["dbname"], INTERVAL)
        while not stop_event.is_set():
            logical_key = str(uuid.uuid4())
            failure_count = 0
            while not stop_event.is_set():
                attempt_id = str(uuid.uuid4())
                attempt_started = utcnow()
                try:
                    record = self._perform_sale(logical_key)
                    self.successful += 1
                    self.consecutive_failures = 0
                    self.last_error = None
                    self.last_record = record
                    self.last_success_at = iso()
                    self.state = "running"
                    attempt = {
                        "attempt_id": attempt_id,
                        "epoch": utcnow().timestamp(),
                        "ts": iso(),
                        "success": True,
                        "latency_ms": record["latency_ms"],
                        "venta_id": record["venta_id"],
                        "idempotency_key": logical_key,
                        "error": None,
                        "retries_for_sale": failure_count,
                        "inserted": record["inserted"],
                    }
                    self._append_attempt(attempt)
                    self._write_stats(attempt)
                    log.info(
                        "Venta confirmada id=%s cliente=%s producto=%s total=%.2f "
                        "latencia=%.1fms%s",
                        record["venta_id"], record["cliente"], record["sku"],
                        record["total"], record["latency_ms"],
                        "" if record["inserted"] else " (idempotente, ya existía)",
                    )
                    break
                except Exception as exc:
                    failure_count += 1
                    self.failed += 1
                    self.retries += 1
                    self.consecutive_failures += 1
                    self.last_error = f"{type(exc).__name__}: {str(exc).strip()[:800]}"
                    self.state = "retrying"
                    attempt = {
                        "attempt_id": attempt_id,
                        "epoch": utcnow().timestamp(),
                        "ts": iso(),
                        "success": False,
                        "latency_ms": round((utcnow() - attempt_started).total_seconds() * 1000.0, 2),
                        "venta_id": None,
                        "idempotency_key": logical_key,
                        "error": self.last_error,
                        "retries_for_sale": failure_count,
                    }
                    self._append_attempt(attempt)
                    self._write_stats(attempt)
                    log.warning("Intento fallido #%s: %s", failure_count, self.last_error)
                    backoff = min(MAX_BACKOFF, 0.5 * (2 ** min(failure_count - 1, 4)))
                    if stop_event.wait(backoff):
                        break

            if stop_event.is_set():
                break
            if stop_event.wait(INTERVAL):
                break

        self.state = "stopped"
        self._write_stats()
        log.info("Generador detenido. ok=%s fallidas=%s", self.successful, self.failed)


def main() -> int:
    generator = Generator()
    try:
        generator.run()
    except Exception:
        log.exception("Fallo fatal del generador")
        generator.state = "crashed"
        generator._write_stats()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
