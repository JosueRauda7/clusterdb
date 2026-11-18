"""Servicio independiente de respaldos lógicos con pg_dump/pg_restore.

- Programa respaldos automáticos por intervalo.
- Conserva un índice persistente en /backups (volumen Docker).
- Verifica integridad básica con `pg_restore --list` y SHA-256.
- Restaura SOLO hacia bases temporales `cluster_demo_restore_*`.
- La restauración está bloqueada por defecto con ALLOW_RESTORE=false.

No participa en la replicación ni en la elección de primario.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("clusterdb.backup")

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/backups"))
INTERVAL_HOURS = float(os.getenv("BACKUP_INTERVAL_HOURS", "6"))
RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "7"))
RETENTION_COUNT = int(os.getenv("BACKUP_RETENTION_COUNT", "14"))
ON_START = os.getenv("BACKUP_ON_START", "true").lower() == "true"
ON_START_DELAY = int(os.getenv("BACKUP_ON_START_DELAY_SECONDS", "25"))
ALLOW_RESTORE = os.getenv("ALLOW_RESTORE", "false").lower() == "true"
RETENTION_TEMP_DATABASES = int(os.getenv("RETENTION_TEMP_DATABASES", "2"))
ON_START_MAX_AGE_MINUTES = int(os.getenv("BACKUP_ON_START_MAX_AGE_MINUTES", "60"))

DB = {
    "host": os.getenv("PGHOST", "haproxy"),
    "port": int(os.getenv("PGPORT", "5432")),
    "dbname": os.getenv("PGDATABASE", "cluster_demo"),
    "user": os.getenv("PGUSER", "backup"),
    "password": os.getenv("PGPASSWORD", ""),
}
PG_ENV = {
    **os.environ,
    "PGPASSWORD": DB["password"],
    "PGCONNECT_TIMEOUT": "5",
    "PGAPPNAME": "clusterdb-backup",
}
INDEX_FILE = BACKUP_DIR / "index.json"
TEMP_DB_RE = re.compile(r"^cluster_demo_restore_\d{8}_\d{6}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


class RestoreRequest(BaseModel):
    filename: str | None = None
    latest: bool = True


class BackupService:
    def __init__(self) -> None:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self._state_lock = threading.RLock()
        self._lock = asyncio.Lock()
        self._running = False
        self._restoring = False
        self._next_run: datetime | None = None
        self.state = self._load_state()

    # ------------------------------------------------------------------
    # Estado persistente
    # ------------------------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "version": 1,
            "sequence": 0,
            "last_attempt": None,
            "last_backup": None,
            "last_restore": None,
            "history": [],
        }
        if INDEX_FILE.exists():
            try:
                with INDEX_FILE.open("r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    state.update(loaded)
            except (OSError, json.JSONDecodeError):
                log.exception("Índice de respaldos ilegible; se reconstruirá")

        last_attempt = state.get("last_attempt") or {}
        if last_attempt.get("status") == "running":
            last_attempt["status"] = "failed"
            last_attempt["error"] = "Proceso interrumpido por reinicio del servicio."
            last_attempt["finished_at"] = iso()
        state["last_attempt"] = last_attempt or None
        if not state.get("history"):
            state["history"] = []
        return state

    def _persist(self) -> None:
        with self._state_lock:
            tmp = INDEX_FILE.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False, indent=2, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, INDEX_FILE)

    def _snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return json.loads(json.dumps(self.state, default=str))

    def status(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        files = sorted(
            [p for p in BACKUP_DIR.glob("*.dump") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return {
            "service": "ok",
            "time": iso(),
            "running": self._running,
            "restoring": self._restoring,
            "interval_hours": INTERVAL_HOURS,
            "retention_days": RETENTION_DAYS,
            "retention_count": RETENTION_COUNT,
            "allow_restore": ALLOW_RESTORE,
            "next_run_at": iso(self._next_run) if self._next_run else None,
            "count": len(files),
            "files": [
                {"filename": p.name, "size_bytes": p.stat().st_size}
                for p in files[:20]
            ],
            "last_backup": snapshot.get("last_backup"),
            "last_attempt": snapshot.get("last_attempt"),
            "last_restore": snapshot.get("last_restore"),
            "history": snapshot.get("history", [])[-30:],
            "database": DB["dbname"],
        }

    # ------------------------------------------------------------------
    # Respaldo
    # ------------------------------------------------------------------
    def _perform_backup(self) -> dict[str, Any]:
        started = utcnow()
        sequence = int(self.state.get("sequence") or 0) + 1
        timestamp = started.strftime("%Y%m%dT%H%M%SZ")
        filename = f"cluster_demo_{timestamp}.dump"
        target = BACKUP_DIR / filename
        suffix = 1
        while target.exists():
            suffix += 1
            filename = f"cluster_demo_{timestamp}_{suffix}.dump"
            target = BACKUP_DIR / filename
        partial = target.with_suffix(".dump.partial")

        attempt: dict[str, Any] = {
            "sequence": sequence,
            "started_at": iso(started),
            "finished_at": None,
            "status": "running",
            "filename": filename,
            "size_bytes": None,
            "sha256": None,
            "verification": None,
            "duration_s": None,
            "database": DB["dbname"],
            "format": "custom",
            "error": None,
        }
        with self._state_lock:
            self.state["sequence"] = sequence
            self.state["last_attempt"] = dict(attempt)
        self._persist()
        log.info("Iniciando respaldo %s", filename)

        last_error = None
        for attempt_no in range(1, 4):
            try:
                if partial.exists():
                    partial.unlink()
                cmd = [
                    "pg_dump",
                    "--format=custom",
                    "--compress=6",
                    "--no-owner",
                    "--no-privileges",
                    "--lock-wait-timeout=15s",
                    "--dbname", DB["dbname"],
                    "--file", str(partial),
                ]
                log.info("pg_dump intento %s/3", attempt_no)
                completed = subprocess.run(
                    cmd, env=PG_ENV, capture_output=True, text=True, timeout=600,
                )
                if completed.returncode != 0:
                    raise RuntimeError(
                        f"pg_dump falló ({completed.returncode}): "
                        f"{(completed.stderr or completed.stdout).strip()[:1000]}"
                    )
                if not partial.exists() or partial.stat().st_size < 1024:
                    raise RuntimeError("El archivo de respaldo está vacío o es demasiado pequeño.")

                listing = subprocess.run(
                    ["pg_restore", "--list", str(partial)],
                    env=PG_ENV, capture_output=True, text=True, timeout=120,
                )
                if listing.returncode != 0:
                    raise RuntimeError(
                        f"pg_restore --list falló: {listing.stderr.strip()[:1000]}"
                    )
                entries = [
                    line for line in listing.stdout.splitlines()
                    if line and not line.startswith(";")
                ]
                if not entries:
                    raise RuntimeError("La verificación no encontró objetos en el respaldo.")

                os.replace(partial, target)
                digest = hashlib.sha256()
                with target.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(chunk)
                target.with_suffix(".dump.sha256").write_text(
                    f"{digest.hexdigest()}  {target.name}\n", encoding="utf-8"
                )
                finished = utcnow()
                attempt.update({
                    "finished_at": iso(finished),
                    "status": "success",
                    "size_bytes": target.stat().st_size,
                    "sha256": digest.hexdigest(),
                    "verification": f"ok ({len(entries)} entradas TOC)",
                    "duration_s": round((finished - started).total_seconds(), 3),
                    "error": None,
                })
                with self._state_lock:
                    self.state["last_attempt"] = dict(attempt)
                    self.state["last_backup"] = dict(attempt)
                    self._append_history(attempt)
                self._persist()
                self._apply_retention()
                self._next_run = utcnow() + timedelta(hours=INTERVAL_HOURS)
                log.info("Respaldo completado: %s (%s bytes)", filename,
                         attempt["size_bytes"])
                return attempt
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
                log.warning("Fallo de respaldo intento %s/3: %s", attempt_no, last_error)
                if partial.exists():
                    try:
                        partial.unlink()
                    except OSError:
                        pass
                if attempt_no < 3:
                    time.sleep(5)

        finished = utcnow()
        attempt.update({
            "finished_at": iso(finished),
            "status": "failed",
            "duration_s": round((finished - started).total_seconds(), 3),
            "error": last_error or "Error desconocido",
        })
        with self._state_lock:
            self.state["last_attempt"] = dict(attempt)
            self._append_history(attempt)
        self._persist()
        log.error("Respaldo fallido: %s", last_error)
        return attempt

    def _append_history(self, attempt: dict[str, Any]) -> None:
        history = list(self.state.get("history") or [])
        history.append(dict(attempt))
        self.state["history"] = history[-100:]

    def _apply_retention(self) -> None:
        now = time.time()
        files = sorted(
            [p for p in BACKUP_DIR.glob("*.dump") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        keep = set()
        for index, path in enumerate(files):
            age_days = (now - path.stat().st_mtime) / 86400.0
            if index < RETENTION_COUNT and age_days <= RETENTION_DAYS:
                keep.add(path)
        for path in files:
            if path not in keep:
                log.info("Retención: eliminando %s", path.name)
                try:
                    path.unlink()
                    path.with_suffix(".dump.sha256").unlink(missing_ok=True)
                except OSError:
                    log.exception("No se pudo eliminar %s", path)
        for partial in BACKUP_DIR.glob("*.partial"):
            if now - partial.stat().st_mtime > 86400:
                partial.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Restauración controlada
    # ------------------------------------------------------------------
    def _restore(self, filename: str | None, latest: bool) -> dict[str, Any]:
        if not ALLOW_RESTORE:
            raise PermissionError("Restauración deshabilitada (ALLOW_RESTORE=false).")
        target_file = self._select_backup(filename, latest)
        started = time.time()
        temp_db = f"cluster_demo_restore_{utcnow().strftime('%Y%m%d_%H%M%S')}"
        conn = None
        try:
            conn = psycopg2.connect(
                host=DB["host"], port=DB["port"], dbname="postgres",
                user=DB["user"], password=DB["password"], connect_timeout=5,
            )
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (temp_db,))
                if cur.fetchone():
                    raise RuntimeError(f"La base temporal {temp_db} ya existe.")
                cur.execute(f'CREATE DATABASE "{temp_db}"')
            log.info("Base temporal creada: %s", temp_db)

            completed = subprocess.run(
                [
                    "pg_restore",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname", temp_db,
                    str(target_file),
                ],
                env={**PG_ENV, "PGDATABASE": temp_db},
                capture_output=True, text=True, timeout=900,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"pg_restore falló ({completed.returncode}): "
                    f"{(completed.stderr or completed.stdout).strip()[:2000]}"
                )

            # La verificación se hace conectando a la base temporal: PostgreSQL
            # no permite consultar tablas de otra base en la misma conexión.
            verify_conn = psycopg2.connect(
                host=DB["host"], port=DB["port"], dbname=temp_db,
                user=DB["user"], password=DB["password"], connect_timeout=5,
            )
            try:
                verify_conn.autocommit = True
                with verify_conn.cursor() as cur:
                    cur.execute("""
                        SELECT
                          (SELECT count(*) FROM clientes),
                          (SELECT count(*) FROM productos),
                          (SELECT count(*) FROM ventas),
                          (SELECT count(*) FROM detalle_ventas)
                    """)
                    counts = cur.fetchone()
            finally:
                verify_conn.close()
            if sum(int(c or 0) for c in counts) <= 0:
                raise RuntimeError("La verificación posterior encontró la base temporal vacía.")

            result = {
                "filename": target_file.name,
                "database": temp_db,
                "verified": True,
                "counts": {
                    "clientes": int(counts[0] or 0),
                    "productos": int(counts[1] or 0),
                    "ventas": int(counts[2] or 0),
                    "detalle_ventas": int(counts[3] or 0),
                },
                "duration_s": round(time.time() - started, 3),
                "restored_at": iso(),
                "note": "La base activa cluster_demo no fue sobrescrita.",
            }
            self._cleanup_temp_databases(conn, keep=temp_db)
            with self._state_lock:
                self.state["last_restore"] = result
            self._persist()
            log.info("Restauración verificada en %s: %s", temp_db, result["counts"])
            return result
        except Exception:
            if conn is not None and temp_db:
                try:
                    conn.rollback()
                    with conn.cursor() as cur:
                        cur.execute(f'DROP DATABASE IF EXISTS "{temp_db}" WITH (FORCE)')
                except Exception:
                    pass
            raise
        finally:
            if conn is not None:
                conn.close()

    def _select_backup(self, filename: str | None, latest: bool) -> Path:
        if filename:
            safe = os.path.basename(filename)
            if safe != filename or not safe.endswith(".dump"):
                raise ValueError("Nombre de respaldo inválido.")
            path = BACKUP_DIR / safe
        elif latest:
            last = (self.state.get("last_backup") or {}).get("filename")
            path = BACKUP_DIR / last if last else None
            if path is None or not path.exists():
                files = sorted(BACKUP_DIR.glob("*.dump"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                if not files:
                    raise FileNotFoundError("No hay respaldos disponibles.")
                path = files[0]
        else:
            raise ValueError("Debe indicar filename o latest=true.")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"No existe el respaldo {path.name}.")
        return path

    def _cleanup_temp_databases(self, conn, keep: str) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'cluster_demo_restore_%' "
                "ORDER BY datname DESC"
            )
            names = [row[0] for row in cur.fetchall() if TEMP_DB_RE.match(row[0] or "")]
        keep_names = set(names[:max(1, RETENTION_TEMP_DATABASES)]) | {keep}
        for name in names:
            if name in keep_names:
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                log.info("Base temporal antigua eliminada: %s", name)
            except Exception:
                log.exception("No se pudo eliminar la base temporal %s", name)

    # ------------------------------------------------------------------
    # Orquestación
    # ------------------------------------------------------------------
    async def run_backup(self, trigger: str) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            self._running = True
            try:
                log.info("Respaldo disparado por %s", trigger)
                await asyncio.to_thread(self._perform_backup)
            finally:
                self._running = False
                if self._next_run is None:
                    self._next_run = utcnow() + timedelta(hours=INTERVAL_HOURS)

    async def run_restore(self, filename: str | None, latest: bool) -> dict[str, Any]:
        if self._lock.locked():
            raise RuntimeError("Hay un respaldo o restauración en ejecución.")
        async with self._lock:
            self._restoring = True
            try:
                return await asyncio.to_thread(self._restore, filename, latest)
            finally:
                self._restoring = False

    def next_run_at(self) -> datetime:
        if self._next_run:
            return self._next_run
        last = (self.state.get("last_backup") or {}).get("finished_at")
        if last:
            try:
                return datetime.fromisoformat(last) + timedelta(hours=INTERVAL_HOURS)
            except ValueError:
                pass
        return utcnow() + timedelta(minutes=1)

    async def scheduler(self) -> None:
        if ON_START:
            await asyncio.sleep(max(0, ON_START_DELAY))
            last = (self.state.get("last_backup") or {}).get("finished_at")
            should_run = True
            if last:
                try:
                    age = (utcnow() - datetime.fromisoformat(last)).total_seconds() / 60.0
                    should_run = age > ON_START_MAX_AGE_MINUTES
                except ValueError:
                    should_run = True
            if should_run:
                await self.run_backup("arranque")
        while True:
            next_run = self.next_run_at()
            self._next_run = next_run
            delay = max(5.0, (next_run - utcnow()).total_seconds())
            log.info("Próximo respaldo programado: %s", iso(next_run))
            await asyncio.sleep(delay)
            await self.run_backup("programado")


service = BackupService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(service.scheduler())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="ClusterDB Backup Service", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "clusterdb-backup",
        "running": service._running,
        "last_backup_status": (service.state.get("last_backup") or {}).get("status"),
    }


@app.get("/status")
def status() -> dict[str, Any]:
    return service.status()


@app.post("/backup")
async def trigger_backup() -> dict[str, Any]:
    if service._running or service._restoring:
        raise HTTPException(status_code=409, detail="Ya hay una operación en ejecución.")
    if service._lock.locked():
        raise HTTPException(status_code=409, detail="Ya hay una operación en ejecución.")
    asyncio.create_task(service.run_backup("manual"))
    return {"status": "started", "message": "Respaldo iniciado."}


@app.post("/restore")
async def trigger_restore(payload: RestoreRequest) -> dict[str, Any]:
    if not ALLOW_RESTORE:
        raise HTTPException(
            status_code=403,
            detail="Restauración deshabilitada. Defina ALLOW_RESTORE=true para habilitarla.",
        )
    try:
        return await service.run_restore(payload.filename, payload.latest)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {str(exc)[:1500]}")
