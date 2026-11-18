"""Cálculos de KPIs a partir de muestras e incidentes reales.

Reglas importantes:
- Nunca se inventan valores. Si no hay datos suficientes se devuelve None
  y un motivo para que el dashboard muestre "Sin datos suficientes".
- La disponibilidad es ponderada por tiempo (no solo por número de muestras).
- MTTR y MTBF son métricas distintas de RTO. RTO es un objetivo.
- El RPO observado se basa en la verificación de la última transacción
  confirmada; no se presenta el objetivo configurado como si fuera una pérdida.
"""
from __future__ import annotations

from statistics import mean
from typing import Any, Iterable


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def compute_availability(
    samples: Iterable[dict[str, Any]],
    poll_seconds: float = 2.0,
    min_samples: int = 10,
) -> dict[str, Any]:
    """Disponibilidad ponderada por tiempo a partir de muestras consecutivas.

    Cada muestra representa el estado durante el intervalo hasta la siguiente.
    Los huecos mayores a 4 veces el intervalo de sondeo (reinicio del monitor)
    se recortan para no contar tiempo no observado.
    """
    rows = sorted((dict(s) for s in samples), key=lambda s: s["ts"])
    total = len(rows)
    result: dict[str, Any] = {
        "pct": None,
        "samples": total,
        "available_samples": sum(1 for s in rows if s.get("available")),
        "observed_seconds": 0.0,
        "downtime_seconds": 0.0,
        "min_samples": min_samples,
        "reason": None,
    }

    if total == 0:
        result["reason"] = "Sin muestras de monitoreo."
        return result

    if total < min_samples:
        result["reason"] = (
            f"Sin datos suficientes: {total} muestra(s); se requieren al menos "
            f"{min_samples}."
        )
        return result

    max_gap = max(10.0, poll_seconds * 4)
    observed = 0.0
    available_time = 0.0
    for current, following in zip(rows, rows[1:]):
        interval = max(0.0, float(following["ts"]) - float(current["ts"]))
        interval = min(interval, max_gap)
        observed += interval
        if current.get("available"):
            available_time += interval

    result["observed_seconds"] = round(observed, 3)
    result["downtime_seconds"] = round(max(0.0, observed - available_time), 3)

    if observed <= 0:
        result["reason"] = "Sin intervalo de observación válido."
        return result

    result["pct"] = _rounded(available_time / observed * 100.0, 4)
    return result


def compute_incident_metrics(
    incidents: Iterable[dict[str, Any]],
    observed_seconds: float,
    min_observation_seconds: float = 60.0,
) -> dict[str, Any]:
    """MTTR, MTBF y conteo de fallos a partir de incidentes medidos."""
    rows = list(incidents)
    failures = len(rows)
    durations = [
        float(row["duration_s"])
        for row in rows
        if row.get("duration_s") is not None
    ]
    result: dict[str, Any] = {
        "failures": failures,
        "resolved_incidents": len(durations),
        "open_incidents": failures - len(durations),
        "mttr_seconds": None,
        "mtbf_seconds": None,
        "mttr_reason": None,
        "mtbf_reason": None,
    }

    if durations:
        result["mttr_seconds"] = _rounded(mean(durations), 3)
    else:
        result["mttr_reason"] = "Sin incidentes resueltos en la ventana."

    if failures == 0:
        result["mtbf_reason"] = "Sin fallos detectados en la ventana."
    elif observed_seconds < min_observation_seconds:
        result["mtbf_reason"] = (
            "Sin datos suficientes: se requiere al menos 1 minuto de observación."
        )
    else:
        # Tiempo operativo / número de fallos. Es una aproximación estándar
        # para una ventana finita de observación.
        operating = max(0.0, observed_seconds - sum(durations))
        result["mtbf_seconds"] = _rounded(operating / failures, 3)

    return result


def compute_sla(
    availability_pct: float | None,
    target_pct: float,
    observed_seconds: float,
    downtime_seconds: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "target_pct": target_pct,
        "met": None,
        "gap_pp": None,
        "allowed_downtime_seconds": _rounded(
            observed_seconds * max(0.0, 100.0 - target_pct) / 100.0, 3
        ),
        "remaining_budget_seconds": None,
        "reason": None,
    }
    if availability_pct is None:
        result["reason"] = "Sin datos suficientes de disponibilidad."
        return result

    result["met"] = availability_pct >= target_pct
    result["gap_pp"] = _rounded(availability_pct - target_pct, 4)
    allowed = result["allowed_downtime_seconds"] or 0.0
    result["remaining_budget_seconds"] = _rounded(allowed - downtime_seconds, 3)
    return result


def evaluate_rto(last_failover: dict[str, Any] | None, target_seconds: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "target_seconds": target_seconds,
        "actual_seconds": None,
        "met": None,
        "reason": None,
    }
    if not last_failover or last_failover.get("recovery_s") is None:
        result["reason"] = "Sin un failover con recuperación medida."
        return result

    actual = float(last_failover["recovery_s"])
    result["actual_seconds"] = _rounded(actual, 3)
    result["met"] = actual <= target_seconds
    return result


def evaluate_rpo(last_failover: dict[str, Any] | None, target_seconds: float) -> dict[str, Any]:
    """RPO observado: pérdida de transacciones confirmadas, no el objetivo.

    Si la última transacción confirmada existe en el nodo promovido, la pérdida
    observada es 0 registros. Si no existe, se marca pérdida confirmada y no se
    inventa una ventana en segundos.
    """
    result: dict[str, Any] = {
        "target_seconds": target_seconds,
        "observed_seconds": None,
        "lost_records": None,
        "verified": False,
        "status": "sin_datos",
        "reason": None,
    }
    if not last_failover or not last_failover.get("rpo_verified"):
        result["reason"] = "Sin verificación de RPO posterior a un failover."
        return result

    lost = last_failover.get("rpo_lost_records")
    result["verified"] = True
    result["lost_records"] = lost
    if lost == 0:
        result["observed_seconds"] = 0.0
        result["status"] = "sin_perdida"
        result["reason"] = (
            "La última transacción confirmada antes del failover existe en el "
            "nodo promovido (0 registros perdidos)."
        )
    else:
        result["status"] = "perdida_confirmada"
        result["reason"] = (
            f"Se confirmó la pérdida de al menos {lost} registro(s). No es "
            "posible calcular una ventana exacta en segundos sin los registros "
            "del nodo que falló."
        )
    return result
