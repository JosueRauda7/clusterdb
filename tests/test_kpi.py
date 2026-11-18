"""Pruebas unitarias de los cálculos de KPI (sin Docker ni PostgreSQL).

Ejecutar:
    python -m unittest discover -s tests -v
con PYTHONPATH apuntando a monitor/.
"""
from __future__ import annotations

import unittest

from kpi import (
    compute_availability,
    compute_incident_metrics,
    compute_sla,
    evaluate_rpo,
    evaluate_rto,
)


def sample(ts: float, available: bool) -> dict:
    return {"ts": ts, "available": available}


class AvailabilityTests(unittest.TestCase):
    def test_empty_returns_none_with_reason(self) -> None:
        result = compute_availability([])
        self.assertIsNone(result["pct"])
        self.assertEqual(result["samples"], 0)
        self.assertIn("Sin muestras", result["reason"])

    def test_insufficient_samples_returns_none(self) -> None:
        rows = [sample(float(i), True) for i in range(5)]
        result = compute_availability(rows, min_samples=10)
        self.assertIsNone(result["pct"])
        self.assertIn("Sin datos suficientes", result["reason"])

    def test_weighted_availability_counts_time_not_samples(self) -> None:
        # 10 intervalos de 2 s: 9 disponibles y 1 caido => 90 %.
        rows = [sample(float(i * 2), i != 5) for i in range(11)]
        result = compute_availability(rows, poll_seconds=2.0, min_samples=10)
        self.assertEqual(result["samples"], 11)
        self.assertAlmostEqual(result["observed_seconds"], 20.0, places=3)
        self.assertAlmostEqual(result["downtime_seconds"], 2.0, places=3)
        self.assertAlmostEqual(result["pct"], 90.0, places=3)

    def test_full_availability_is_100(self) -> None:
        rows = [sample(float(i * 2), True) for i in range(11)]
        result = compute_availability(rows, poll_seconds=2.0, min_samples=10)
        self.assertAlmostEqual(result["pct"], 100.0, places=3)
        self.assertAlmostEqual(result["downtime_seconds"], 0.0, places=3)

    def test_large_gap_is_capped(self) -> None:
        # Un hueco de 1 hora (reinicio del monitor) se recorta al maximo de 10 s
        # (max(10, poll_seconds * 4)); no se cuenta tiempo no observado.
        rows = [
            sample(0.0, True),
            sample(3600.0, True),
        ]
        result = compute_availability(rows, poll_seconds=2.0, min_samples=2)
        self.assertAlmostEqual(result["observed_seconds"], 10.0, places=3)
        self.assertAlmostEqual(result["pct"], 100.0, places=3)

    def test_unsorted_samples_are_sorted(self) -> None:
        rows = [sample(4.0, True), sample(0.0, False), sample(2.0, True)]
        result = compute_availability(rows, poll_seconds=2.0, min_samples=2)
        # 0->2 caido, 2->4 disponible => 50 %.
        self.assertAlmostEqual(result["pct"], 50.0, places=3)


class IncidentMetricsTests(unittest.TestCase):
    def test_no_incidents(self) -> None:
        result = compute_incident_metrics([], observed_seconds=600)
        self.assertEqual(result["failures"], 0)
        self.assertIsNone(result["mttr_seconds"])
        self.assertIsNone(result["mtbf_seconds"])
        self.assertIn("Sin fallos", result["mtbf_reason"])

    def test_mttr_is_mean_of_durations(self) -> None:
        incidents = [
            {"duration_s": 4.0},
            {"duration_s": 8.0},
        ]
        result = compute_incident_metrics(incidents, observed_seconds=600)
        self.assertEqual(result["failures"], 2)
        self.assertAlmostEqual(result["mttr_seconds"], 6.0, places=3)

    def test_open_incident_is_counted_but_not_averaged(self) -> None:
        incidents = [{"duration_s": 4.0}, {"duration_s": None}]
        result = compute_incident_metrics(incidents, observed_seconds=600)
        self.assertEqual(result["failures"], 2)
        self.assertEqual(result["resolved_incidents"], 1)
        self.assertEqual(result["open_incidents"], 1)
        self.assertAlmostEqual(result["mttr_seconds"], 4.0, places=3)

    def test_mtbf_requires_enough_observation(self) -> None:
        result = compute_incident_metrics(
            [{"duration_s": 4.0}], observed_seconds=10, min_observation_seconds=60
        )
        self.assertIsNone(result["mtbf_seconds"])
        self.assertIn("Sin datos suficientes", result["mtbf_reason"])

    def test_mtbf_uses_operating_time(self) -> None:
        result = compute_incident_metrics(
            [{"duration_s": 5.0}], observed_seconds=105, min_observation_seconds=60
        )
        # (105 - 5) / 1 fallo = 100 s.
        self.assertAlmostEqual(result["mtbf_seconds"], 100.0, places=3)


class SlaTests(unittest.TestCase):
    def test_no_availability_has_reason(self) -> None:
        result = compute_sla(None, 99.9, observed_seconds=1000, downtime_seconds=0)
        self.assertIsNone(result["met"])
        self.assertIn("Sin datos suficientes", result["reason"])

    def test_sla_met_and_budget(self) -> None:
        result = compute_sla(100.0, 99.9, observed_seconds=10000, downtime_seconds=1.0)
        self.assertTrue(result["met"])
        self.assertAlmostEqual(result["allowed_downtime_seconds"], 10.0, places=3)
        self.assertAlmostEqual(result["remaining_budget_seconds"], 9.0, places=3)

    def test_sla_not_met_negative_budget(self) -> None:
        result = compute_sla(99.0, 99.9, observed_seconds=10000, downtime_seconds=100.0)
        self.assertFalse(result["met"])
        self.assertLess(result["remaining_budget_seconds"], 0)
        self.assertAlmostEqual(result["gap_pp"], -0.9, places=4)


class RtoRpoTests(unittest.TestCase):
    def test_rto_without_failover(self) -> None:
        result = evaluate_rto(None, 30.0)
        self.assertIsNone(result["actual_seconds"])
        self.assertIn("Sin un failover", result["reason"])

    def test_rto_met_and_not_met(self) -> None:
        self.assertTrue(evaluate_rto({"recovery_s": 4.352}, 30.0)["met"])
        self.assertFalse(evaluate_rto({"recovery_s": 45.0}, 30.0)["met"])

    def test_rpo_without_verification(self) -> None:
        result = evaluate_rpo({"rpo_verified": 0}, 60.0)
        self.assertFalse(result["verified"])
        self.assertIsNone(result["lost_records"])
        self.assertIn("Sin verificación", result["reason"])

    def test_rpo_zero_loss(self) -> None:
        result = evaluate_rpo(
            {"rpo_verified": 1, "rpo_lost_records": 0}, 60.0
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["lost_records"], 0)
        self.assertEqual(result["observed_seconds"], 0.0)
        self.assertEqual(result["status"], "sin_perdida")

    def test_rpo_confirmed_loss_does_not_invent_seconds(self) -> None:
        result = evaluate_rpo(
            {"rpo_verified": 1, "rpo_lost_records": 3}, 60.0
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["lost_records"], 3)
        self.assertIsNone(result["observed_seconds"])
        self.assertEqual(result["status"], "perdida_confirmada")

    def test_configured_target_is_not_reported_as_loss(self) -> None:
        result = evaluate_rpo({"rpo_verified": 1, "rpo_lost_records": 0}, 60.0)
        self.assertEqual(result["target_seconds"], 60.0)
        self.assertNotEqual(result["observed_seconds"], 60.0)


if __name__ == "__main__":
    unittest.main()
