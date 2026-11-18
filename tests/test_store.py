"""Pruebas unitarias de la persistencia SQLite del monitor.

Cada prueba usa una base temporal propia: no toca el volumen de Docker ni
requiere PostgreSQL.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import uuid

# Debe definirse antes de importar store.
os.environ.setdefault("MONITOR_DB_PATH", os.path.join(tempfile.gettempdir(), "clusterdb-test-bootstrap.db"))

import store  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="clusterdb-store-")
        store.DB_PATH = os.path.join(self._dir, f"monitor-{uuid.uuid4().hex}.db")
        store.init_db()

    def _insert_sample(self, ts: float, available: bool = True, **extra) -> int:
        payload = {
            "ts": ts,
            "ts_iso": f"2026-11-06T00:00:{int(ts) % 60:02d}+00:00",
            "available": available,
            "latency_ms": extra.pop("latency_ms", 10.0),
            "replication_lag_bytes": extra.pop("replication_lag_bytes", 0),
            "nodes": extra.pop("nodes", []),
            "replication": extra.pop("replication", {}),
            "backup": extra.pop("backup", {}),
        }
        payload.update(extra)
        return store.insert_sample(payload)

    def test_schema_and_empty_queries(self) -> None:
        self.assertEqual(store.count_samples(), 0)
        self.assertIsNone(store.latest_sample())
        self.assertIsNone(store.first_sample_ts())
        self.assertIsNone(store.get_meta("missing"))

    def test_insert_and_fetch_latest(self) -> None:
        self._insert_sample(100.0, True, latency_ms=12.5)
        self._insert_sample(102.0, False, latency_ms=99.0)
        self.assertEqual(store.count_samples(), 2)

        latest = store.latest_sample()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["ts"], 102.0)
        self.assertEqual(latest["available"], 0)
        self.assertEqual(latest["latency_ms"], 99.0)
        # Los JSON se decoran a listas/dicts.
        self.assertEqual(latest["nodes"], [])
        self.assertEqual(latest["replication"], {})

        previous = store.previous_sample(102.0)
        self.assertIsNotNone(previous)
        self.assertEqual(previous["ts"], 100.0)

    def test_samples_between_and_series(self) -> None:
        base = 1000.0
        for i in range(6):
            self._insert_sample(
                base + i * 2,
                available=i != 3,
                latency_ms=10.0 + i,
                replication_lag_bytes=i * 100,
            )
        rows = store.samples_between(base, base + 10)
        self.assertEqual(len(rows), 6)

        series = store.series_from_samples(rows, bucket_seconds=6)
        self.assertTrue(series)
        self.assertEqual(sum(item["samples"] for item in series), 6)
        for bucket in series:
            if bucket["latency_ms_avg"] is not None:
                self.assertLessEqual(bucket["latency_ms_avg"], bucket["latency_ms_max"])

    def test_incident_lifecycle(self) -> None:
        incident_id = store.open_incident(
            start=100.0, start_iso="2026-11-06T00:00:00+00:00",
            detected=100.0, detected_iso="2026-11-06T00:00:00+00:00",
            failed_node="Master", last_ack_id=7, last_ack_at=99.0,
            last_ack_iso="2026-11-06T00:00:00+00:00",
        )
        open_incident = store.get_open_incident()
        self.assertIsNotNone(open_incident)
        self.assertEqual(open_incident["id"], incident_id)

        store.close_incident(
            incident_id, ended=104.0, ended_iso="2026-11-06T00:00:04+00:00",
            duration_s=4.0, new_primary="postgres-replica",
            rpo_verified=True, rpo_lost=0, rpo_seconds=0.0,
        )
        self.assertIsNone(store.get_open_incident())

        incidents = store.recent_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["duration_s"], 4.0)
        self.assertEqual(incidents[0]["new_primary"], "postgres-replica")
        self.assertEqual(incidents[0]["rpo_lost_records"], 0)

    def test_failover_records(self) -> None:
        incident_id = store.open_incident(
            100.0, "iso", 100.0, "iso", "Master", 7, 99.0, "iso"
        )
        failover_id = store.insert_failover(
            detected=100.0, detected_iso="iso", old_primary="postgres-master",
            new_primary="postgres-replica", recovery_s=4.352,
            rto_target=30.0, incident_id=incident_id,
        )
        store.update_failover_recovery(
            failover_id, recovery_s=4.352, rto_target=30.0,
            incident_id=incident_id, rpo_verified=True, rpo_lost=0,
            rpo_seconds=0.0,
        )
        row = store.last_failover()
        self.assertIsNotNone(row)
        self.assertEqual(row["new_primary"], "postgres-replica")
        self.assertEqual(row["rto_met"], 1)
        self.assertEqual(row["rpo_verified"], 1)
        self.assertEqual(row["rpo_lost_records"], 0)

    def test_events_window_and_filter(self) -> None:
        store.insert_event(
            100.0, "iso", "FAILOVER_START", "Master", "inicio",
            result="failed", severity="critical",
        )
        store.insert_event(
            101.0, "iso", "SERVICE_RECOVERED", "Master", "recuperado",
            result="success", severity="info",
        )
        everything = store.events(0.0, 200.0)
        self.assertEqual(len(everything), 2)
        only_recovery = store.events(0.0, 200.0, event_type="SERVICE_RECOVERED")
        self.assertEqual(len(only_recovery), 1)
        self.assertEqual(only_recovery[0]["type"], "SERVICE_RECOVERED")

    def test_tx_events_are_idempotent(self) -> None:
        store.insert_tx_event("a1", 100.0, True, 5.0, 1, "key-1", None)
        store.insert_tx_event("a1", 100.0, True, 5.0, 1, "key-1", None)
        store.insert_tx_event("a2", 101.0, False, 3000.0, None, "key-2", "timeout")
        counts = store.tx_counts(0.0, 200.0)
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["total"], 2)

        series = store.tx_series(0.0, 200.0, bucket_seconds=10)
        self.assertEqual(sum(item["ok"] for item in series), 1)
        self.assertEqual(sum(item["failed"] for item in series), 1)

    def test_ingest_generator_attempts_is_incremental(self) -> None:
        path = os.path.join(self._dir, "attempts.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"attempt_id": "x1", "epoch": 100.0, "success": True, "latency_ms": 3.0}) + "\n")
            handle.write(json.dumps({"attempt_id": "x2", "epoch": 101.0, "success": False, "error": "boom"}) + "\n")

        self.assertEqual(store.ingest_generator_attempts(path), 2)
        self.assertEqual(store.ingest_generator_attempts(path), 0)
        self.assertEqual(store.tx_counts(0.0, 200.0)["total"], 2)

        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"attempt_id": "x3", "epoch": 102.0, "success": True}) + "\n")
        self.assertEqual(store.ingest_generator_attempts(path), 1)
        self.assertEqual(store.tx_counts(0.0, 200.0)["total"], 3)

    def test_metadata_roundtrip(self) -> None:
        store.set_meta("k", "v1")
        self.assertEqual(store.get_meta("k"), "v1")
        store.set_meta("k", "v2")
        self.assertEqual(store.get_meta("k"), "v2")

    def test_prune_removes_only_old_samples(self) -> None:
        now = time.time()
        self._insert_sample(now - 40 * 86400)
        self._insert_sample(now)
        deleted = store.prune(30)
        self.assertEqual(deleted, 1)
        self.assertEqual(store.count_samples(), 1)


if __name__ == "__main__":
    unittest.main()
