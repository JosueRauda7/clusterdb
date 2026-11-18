"""Dashboard Streamlit de ClusterDB Monitor.

Consume únicamente la API interna del monitor. No se monta el socket de Docker
y no se ofrecen botones de failover: el failover se provoca con los comandos
reales documentados en el laboratorio de fallos.
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

API = os.getenv("MONITOR_URL", "http://monitor:8000").rstrip("/")
REFRESH_SECONDS = max(2, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "5")))
SLA_TARGET = float(os.getenv("SLA_TARGET_PERCENT", "99.9"))
RTO_TARGET = float(os.getenv("RTO_TARGET_SECONDS", "30"))
RPO_TARGET = float(os.getenv("RPO_TARGET_SECONDS", "60"))
TZ = os.getenv("TZ", "UTC")
try:
    LOCAL_TZ = ZoneInfo(TZ)
except Exception:
    LOCAL_TZ = timezone.utc
TIMEOUT = 6

GREEN = "#34d399"
CYAN = "#22d3ee"
AMBER = "#f59e0b"
RED = "#f87171"
SLATE = "#94a3b8"

st.set_page_config(
    page_title="ClusterDB Monitor",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="expanded",
)

CSS = """
<style>
  :root {
    --bg: #070d18;
    --panel: #0e1728;
    --panel-2: #111f35;
    --line: rgba(148, 163, 184, 0.18);
    --text: #e2e8f0;
    --muted: #94a3b8;
    --cyan: #22d3ee;
    --green: #34d399;
    --amber: #f59e0b;
    --red: #f87171;
  }
  .stApp {
    background:
      radial-gradient(1200px 600px at 15% -10%, rgba(34, 211, 238, 0.10), transparent 60%),
      radial-gradient(900px 500px at 95% 0%, rgba(52, 211, 153, 0.08), transparent 55%),
      var(--bg);
    color: var(--text);
  }
  section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0b1424 0%, #070d18 100%);
    border-right: 1px solid var(--line);
  }
  header[data-testid="stHeader"] { background: transparent; }
  .block-container { padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1500px; }
  h1, h2, h3 { color: #f8fafc !important; letter-spacing: -0.02em; }
  .hero {
    display: flex; justify-content: space-between; gap: 1rem;
    align-items: flex-end; flex-wrap: wrap;
    padding: 0.4rem 0 1rem 0;
    border-bottom: 1px solid var(--line);
    margin-bottom: 1.1rem;
  }
  .hero h1 { font-size: 2rem; margin: 0; }
  .hero p { color: var(--muted); margin: 0.35rem 0 0 0; font-size: 0.95rem; }
  .pill {
    display: inline-flex; align-items: center; gap: 0.45rem;
    border: 1px solid var(--line); border-radius: 999px;
    padding: 0.35rem 0.7rem; color: var(--muted);
    font-size: 0.78rem; background: rgba(15, 23, 42, 0.6);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .card {
    background: linear-gradient(160deg, rgba(17, 31, 53, 0.95), rgba(12, 21, 37, 0.95));
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 0.95rem 1rem;
    min-height: 112px;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.22);
    position: relative;
    overflow: hidden;
  }
  .card::before {
    content: ""; position: absolute; inset: 0 auto 0 0; width: 3px;
    background: var(--muted);
  }
  .card.good::before { background: var(--green); }
  .card.warn::before { background: var(--amber); }
  .card.bad::before { background: var(--red); }
  .card.cyan::before { background: var(--cyan); }
  .card-label {
    color: var(--muted); font-size: 0.72rem; text-transform: uppercase;
    letter-spacing: 0.11em; font-weight: 600;
  }
  .card-value {
    color: #f8fafc; font-size: 1.42rem; font-weight: 700;
    margin-top: 0.38rem; line-height: 1.15; word-break: break-word;
  }
  .card-value.small { font-size: 1.08rem; }
  .card-detail {
    color: var(--muted); font-size: 0.78rem; margin-top: 0.36rem;
    line-height: 1.35;
  }
  .kpi-card {
    background: linear-gradient(160deg, rgba(17, 31, 53, 0.92), rgba(11, 19, 33, 0.96));
    border: 1px solid var(--line); border-radius: 14px;
    padding: 1rem 1.05rem; min-height: 128px; height: 100%;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.18);
    position: relative; overflow: hidden;
  }
  .kpi-card::after {
    content: ""; position: absolute; right: -30px; top: -40px;
    width: 120px; height: 120px; border-radius: 50%;
    background: radial-gradient(circle, rgba(34, 211, 238, 0.13), transparent 70%);
  }
  .kpi-card.good { border-color: rgba(52, 211, 153, 0.38); }
  .kpi-card.warn { border-color: rgba(245, 158, 11, 0.38); }
  .kpi-card.bad { border-color: rgba(248, 113, 113, 0.40); }
  .kpi-card .card-value { font-size: 1.55rem; }
  .section-title {
    display: flex; align-items: center; gap: 0.6rem;
    margin: 1.6rem 0 0.7rem 0;
  }
  .section-title h2 { font-size: 1.16rem; margin: 0; }
  .section-title span {
    color: var(--muted); font-size: 0.8rem; font-weight: 400;
  }
  .note {
    border: 1px solid rgba(245, 158, 11, 0.35);
    background: rgba(245, 158, 11, 0.07);
    border-radius: 12px; padding: 0.8rem 1rem;
    color: #fcd34d; font-size: 0.86rem;
  }
  .ok-note {
    border: 1px solid rgba(52, 211, 153, 0.35);
    background: rgba(52, 211, 153, 0.06);
    border-radius: 12px; padding: 0.8rem 1rem;
    color: #a7f3d0; font-size: 0.86rem;
  }
  .check-row { border-bottom: 1px solid var(--line); padding: 0.45rem 0; font-size: 0.86rem; }
  .check-ok { color: var(--green); font-weight: 700; }
  .check-bad { color: var(--red); font-weight: 700; }
  code { color: #a5f3fc; }
  div[data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 12px; }
  .stButton > button {
    border-radius: 9px; border: 1px solid var(--line);
    background: rgba(17, 31, 53, 0.9); color: var(--text);
  }
  .stButton > button:hover { border-color: var(--cyan); color: var(--cyan); }
  .footer { color: #64748b; font-size: 0.76rem; margin-top: 2rem; line-height: 1.55; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def get_json(path: str, params: dict[str, Any] | None = None, timeout: float = TIMEOUT):
    try:
        response = requests.get(f"{API}{path}", params=params, timeout=timeout)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:  # noqa: BLE001 - se muestra al operador
        return None, f"{type(exc).__name__}: {exc}"


def post_json(path: str, payload: dict[str, Any] | None = None, timeout: float = 60):
    try:
        response = requests.post(f"{API}{path}", json=payload, timeout=timeout)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            return None, f"HTTP {response.status_code}: {str(detail)[:600]}"
        return response.json(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def fmt_duration(seconds: float | None, missing: str = "Sin datos suficientes") -> str:
    if seconds is None:
        return missing
    seconds = float(seconds)
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def fmt_bytes(value: float | None) -> str:
    if value is None:
        return "sin dato"
    value = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024
    return f"{value:.1f} GB"


def fmt_pct(value: float | None, digits: int = 2) -> str:
    return "Sin datos suficientes" if value is None else f"{float(value):.{digits}f}%"


def fmt_ts(iso_value: str | None, missing: str = "—") -> str:
    if not iso_value:
        return missing
    try:
        raw = iso_value.replace("Z", "+00:00")
        return datetime.fromisoformat(raw).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(iso_value)


def relative(ts: float | None, now: float | None) -> str:
    if not ts:
        return "Sin failovers"
    if not now:
        return fmt_ts(datetime.fromtimestamp(ts, tz=timezone.utc).isoformat())
    delta = max(0.0, float(now) - float(ts))
    return "hace " + fmt_duration(delta)


def card(label: str, value: Any, detail: Any = "", tone: str = "cyan",
         small: bool = False) -> str:
    value_class = "card-value small" if small else "card-value"
    return (
        f'<div class="card {tone}">'
        f'<div class="card-label">{esc(label)}</div>'
        f'<div class="{value_class}">{esc(value)}</div>'
        f'<div class="card-detail">{esc(detail)}</div>'
        f"</div>"
    )


def kpi_card(label: str, value: Any, detail: Any = "", tone: str = "",
             small: bool = False) -> str:
    value_class = "card-value small" if small else "card-value"
    return (
        f'<div class="kpi-card {tone}">'
        f'<div class="card-label">{esc(label)}</div>'
        f'<div class="{value_class}">{esc(value)}</div>'
        f'<div class="card-detail">{esc(detail)}</div>'
        f"</div>"
    )


def section(title: str, subtitle: str = "") -> None:
    st.markdown(
        f'<div class="section-title"><h2>{esc(title)}</h2>'
        f'<span>{esc(subtitle)}</span></div>',
        unsafe_allow_html=True,
    )


def base_figure(title: str, height: int = 300) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template="plotly_dark",
        title={"text": title, "font": {"size": 14, "color": "#e2e8f0"}, "x": 0.01},
        height=height,
        margin={"l": 12, "r": 12, "t": 46, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#cbd5e1", "size": 11},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.12, "x": 0, "font": {"size": 10}},
    )
    fig.update_xaxes(gridcolor="rgba(148,163,184,0.10)", zeroline=False)
    fig.update_yaxes(gridcolor="rgba(148,163,184,0.10)", zeroline=False)
    return fig


def fig_timestamp(ts: Any) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(LOCAL_TZ)


def render_chart(fig: go.Figure) -> None:
    st.plotly_chart(
        fig,
        use_container_width=True,
        config={"displayModeBar": False, "responsive": True},
    )


# ---------------------------------------------------------------------------
# Bloques
# ---------------------------------------------------------------------------
def render_status_cards(status: dict[str, Any]) -> None:
    service = status.get("service") or {}
    nodes = status.get("nodes") or []
    replication = status.get("replication") or {}
    backup = status.get("backup") or {}
    generator = status.get("generator") or {}

    primary = next((n for n in nodes if n.get("role") == "primary"), None)
    replica = next((n for n in nodes if n.get("role") == "replica"), None)

    available = service.get("available")
    if available is True:
        service_tone, service_value = "good", "DISPONIBLE"
    elif available is False:
        service_tone, service_value = "bad", "NO DISPONIBLE"
    else:
        service_tone, service_value = "warn", "SIN DATOS"
    latency = service.get("latency_ms")
    service_detail = (
        f"Latencia {latency:.0f} ms · primario {service.get('primary_label') or '—'}"
        if latency is not None else (service.get("error") or "Esperando muestras")
    )

    if primary:
        primary_tone = "good"
        primary_value = primary.get("label") or primary.get("name")
        primary_detail = (
            f"{primary.get('name')} · {primary.get('state_label') or primary.get('state')}"
            f" · TL {primary.get('timeline') if primary.get('timeline') is not None else '?'}"
        )
    else:
        primary_tone, primary_value = "bad", "Sin primario"
        primary_detail = "Patroni no reporta un nodo primario activo."

    if replica:
        replica_tone = "good" if replica.get("state") == "running" else "warn"
        replica_value = replica.get("label") or replica.get("name")
        lag = replica.get("lag_bytes")
        replica_detail = (
            f"{replica.get('name')} · {replica.get('state_label') or replica.get('state')}"
            f" · lag {fmt_bytes(lag) if lag is not None else 'sin dato'}"
        )
    else:
        replica_tone, replica_value = "warn", "Sin réplica"
        replica_detail = "No hay un nodo en rol réplica en este momento."

    rep_state = (replication.get("state") or "unknown").lower()
    if rep_state == "streaming":
        rep_tone, rep_value = "good", "STREAMING"
    elif rep_state in {"none", "unknown"}:
        rep_tone, rep_value = "bad", "SIN REPLICACIÓN"
    else:
        rep_tone, rep_value = "warn", rep_state.upper()
    rep_detail = (
        f"Lag {fmt_bytes(replication.get('lag_bytes'))}"
        f" · sync {replication.get('sync_state') or '—'}"
    )

    last_backup = backup.get("last_backup") or {}
    last_status = (last_backup.get("status") or "").lower()
    if not backup.get("reachable"):
        backup_tone, backup_value = "bad", "NO DISPONIBLE"
        backup_detail = backup.get("error") or "Servicio de respaldo inaccesible."
    elif last_status == "success":
        backup_tone, backup_value = "good", "RESPALDO OK"
        backup_detail = (
            f"{last_backup.get('filename') or 'archivo'} · "
            f"{fmt_bytes(last_backup.get('size_bytes'))}"
        )
    elif last_status == "failed":
        backup_tone, backup_value = "bad", "RESPALDO FALLÓ"
        backup_detail = str(last_backup.get("error") or "Revise el servicio de respaldo.")[:120]
    else:
        backup_tone, backup_value = "warn", "SIN RESPALDO"
        backup_detail = "Aún no hay un respaldo completado."

    gen_state = (generator.get("state") or "unknown").lower()
    gen_tone = "good" if gen_state == "running" else ("warn" if gen_state in {"retrying", "starting"} else "bad")
    gen_value = gen_state.upper()
    gen_detail = (
        f"OK {generator.get('successful', 0)} · fallidas {generator.get('failed', 0)}"
        f" · archivo {generator.get('file_age_seconds', '?')} s"
    )

    columns = st.columns(6, gap="small")
    items = [
        ("Servicio HAProxy", service_value, service_detail, service_tone),
        ("Nodo primario", primary_value, primary_detail, primary_tone),
        ("Nodo réplica", replica_value, replica_detail, replica_tone),
        ("Replicación", rep_value, rep_detail, rep_tone),
        ("Respaldos", backup_value, backup_detail, backup_tone),
        ("Generador TX", gen_value, gen_detail, gen_tone),
    ]
    for column, (label, value, detail, tone) in zip(columns, items):
        with column:
            st.markdown(card(label, value, detail, tone, small=True), unsafe_allow_html=True)


def render_kpis(kpis: dict[str, Any], now: float) -> None:
    availability = kpis.get("availability") or {}
    mttr = kpis.get("mttr") or {}
    mtbf = kpis.get("mtbf") or {}
    failures = kpis.get("failures") or {}
    sla = kpis.get("sla") or {}
    rto = kpis.get("rto") or {}
    rpo = kpis.get("rpo") or {}
    last_fo = kpis.get("last_failover") or {}
    tx = kpis.get("transactions") or {}

    availability_pct = availability.get("pct")
    availability_tone = (
        "bad" if availability_pct is not None and availability_pct < SLA_TARGET
        else ("good" if availability_pct is not None else "")
    )
    availability_detail = (
        f"{fmt_duration(availability.get('observed_seconds'))} observados"
        f" · {availability.get('samples', 0)} muestras"
        f" · caída {fmt_duration(availability.get('downtime_seconds'))}"
    )

    sla_met = sla.get("met")
    sla_tone = "good" if sla_met is True else ("bad" if sla_met is False else "")
    sla_value = "CUMPLE" if sla_met is True else ("NO CUMPLE" if sla_met is False else "SIN DATOS")
    sla_detail = (
        f"Objetivo {sla.get('target_pct', SLA_TARGET):g}%"
        f" · margen {sla.get('gap_pp'):+.3f} pp"
        f" · presupuesto restante {fmt_duration(sla.get('remaining_budget_seconds'))}"
        if sla.get("gap_pp") is not None else (sla.get("reason") or "Sin datos suficientes")
    )

    mttr_value = fmt_duration(mttr.get("seconds"), "Sin datos suficientes")
    mtbf_value = fmt_duration(mtbf.get("seconds"), "Sin datos suficientes")
    rto_actual = rto.get("actual_seconds")
    rto_tone = "good" if rto.get("met") is True else ("bad" if rto.get("met") is False else "")
    rto_value = fmt_duration(rto_actual, "Sin datos suficientes")
    rto_detail = (
        f"Objetivo {rto.get('target_seconds', RTO_TARGET):g} s"
        f" · {'cumple' if rto.get('met') else 'no cumple'}"
        if rto_actual is not None else (rto.get("reason") or "Sin datos suficientes")
    )

    if rpo.get("verified"):
        lost = rpo.get("lost_records")
        rpo_value = f"{lost} registro(s)" if lost is not None else "Verificado"
        rpo_tone = "good" if lost == 0 else "warn"
        rpo_detail = (
            f"Objetivo {rpo.get('target_seconds', RPO_TARGET):g} s"
            " · pérdida medida, no objetivo"
        )
    else:
        rpo_tone = ""
        rpo_value = "Sin datos suficientes"
        rpo_detail = rpo.get("reason") or "Aún no hay un failover verificado."

    failures_count = int(failures.get("detected") or 0)
    failures_tone = "good" if failures_count == 0 else "warn"
    failures_detail = (
        f"Resueltos {failures.get('resolved', 0)}"
        f" · abiertos {failures.get('open', 0)}"
    )

    tx_ok = int(tx.get("window_ok") or 0)
    tx_failed = int(tx.get("window_failed") or 0)
    tx_tone = "good" if tx_failed == 0 else "warn"
    tx_detail = (
        f"En ventana · acumulado OK {tx.get('total_ok', 0)}"
        f" · fallidas {tx.get('total_failed', 0)}"
    )

    last_fo_value = relative(last_fo.get("detected_at"), now)
    last_fo_detail = (
        f"{last_fo.get('old_primary_label') or '?'} → {last_fo.get('new_primary_label') or '?'}"
        f" · recuperación {fmt_duration(last_fo.get('recovery_s'))}"
        if last_fo else "Aún no se ha registrado un failover real."
    )
    last_fo_tone = "cyan" if last_fo else ""

    cards = [
        ("Disponibilidad", fmt_pct(availability_pct), availability_detail, availability_tone, False),
        ("SLA", sla_value, sla_detail, sla_tone, False),
        ("MTTR", mttr_value, mttr.get("reason") or "Promedio de incidentes resueltos.", "", False),
        ("MTBF", mtbf_value, mtbf.get("reason") or "Tiempo operativo entre fallos.", "", False),
        ("RTO observado", rto_value, rto_detail, rto_tone, False),
        ("RPO observado", rpo_value, rpo_detail, rpo_tone, True),
        ("Último failover", last_fo_value, last_fo_detail, last_fo_tone, False),
        ("Fallos detectados", failures_count, failures_detail, failures_tone, False),
        ("Transacciones OK / fallidas", f"{tx_ok:,} / {tx_failed:,}", tx_detail, tx_tone, False),
    ]

    for start in range(0, len(cards), 3):
        columns = st.columns(3, gap="small")
        for column, (label, value, detail, tone, small) in zip(columns, cards[start:start + 3]):
            with column:
                st.markdown(kpi_card(label, value, detail, tone, small), unsafe_allow_html=True)


def render_charts(timeseries: dict[str, Any], backups: dict[str, Any]) -> None:
    series = timeseries.get("availability") or []
    tx_series = timeseries.get("transactions") or []
    failovers = timeseries.get("failovers") or []

    columns = st.columns(2, gap="medium")

    with columns[0]:
        fig = base_figure("Disponibilidad del servicio (%)")
        # Un bucket sin muestras devuelve availability_pct=None; no se debe
        # graficar ni usar para calcular el rango del eje Y.
        points = [row for row in series if row.get("availability_pct") is not None]
        if points:
            x = [fig_timestamp(row["ts"]) for row in points]
            y = [float(row["availability_pct"]) for row in points]
            fig.add_trace(go.Scatter(
                x=x, y=y, mode="lines", name="Disponibilidad",
                line={"color": CYAN, "width": 2.2},
                fill="tozeroy", fillcolor="rgba(34,211,238,0.12)",
                hovertemplate="%{y:.2f}%<extra></extra>",
            ))
            fig.add_hline(
                y=SLA_TARGET, line_dash="dot", line_color=AMBER,
                annotation_text=f"SLA {SLA_TARGET:g}%",
                annotation_font_color=AMBER,
            )
            fig.update_yaxes(range=[max(0, min(y) - 5), 101], ticksuffix="%")
        else:
            fig.add_annotation(text="Sin datos suficientes para graficar",
                               showarrow=False, font={"color": SLATE})
        render_chart(fig)

    with columns[1]:
        fig = base_figure("Latencia del servicio (ms)")
        rows = [row for row in series if row.get("latency_ms_avg") is not None]
        if rows:
            fig.add_trace(go.Scatter(
                x=[fig_timestamp(row["ts"]) for row in rows],
                y=[row["latency_ms_avg"] for row in rows],
                mode="lines", name="Latencia promedio",
                line={"color": GREEN, "width": 2},
                hovertemplate="%{y:.2f} ms<extra></extra>",
            ))
            max_rows = [row for row in rows if row.get("latency_ms_max") is not None]
            if max_rows:
                fig.add_trace(go.Scatter(
                    x=[fig_timestamp(row["ts"]) for row in max_rows],
                    y=[row["latency_ms_max"] for row in max_rows],
                    mode="lines", name="Máxima",
                    line={"color": "rgba(52,211,153,0.35)", "width": 1, "dash": "dot"},
                    hovertemplate="%{y:.2f} ms<extra></extra>",
                ))
        else:
            fig.add_annotation(text="Sin latencias registradas", showarrow=False,
                               font={"color": SLATE})
        render_chart(fig)

    with columns[0]:
        fig = base_figure("Retraso de replicación (MB)")
        rows = [row for row in series if row.get("lag_bytes") is not None]
        if rows:
            fig.add_trace(go.Scatter(
                x=[fig_timestamp(row["ts"]) for row in rows],
                y=[float(row["lag_bytes"]) / 1048576.0 for row in rows],
                mode="lines", name="Lag máximo",
                line={"color": AMBER, "width": 2},
                fill="tozeroy", fillcolor="rgba(245,158,11,0.10)",
                hovertemplate="%{y:.3f} MB<extra></extra>",
            ))
        else:
            fig.add_annotation(text="Sin datos de replicación", showarrow=False,
                               font={"color": SLATE})
        render_chart(fig)

    with columns[1]:
        fig = base_figure("Transacciones por intervalo")
        if tx_series:
            fig.add_trace(go.Bar(
                x=[fig_timestamp(row["ts"]) for row in tx_series],
                y=[row.get("ok", 0) for row in tx_series],
                name="OK", marker_color="rgba(52,211,153,0.85)",
            ))
            fig.add_trace(go.Bar(
                x=[fig_timestamp(row["ts"]) for row in tx_series],
                y=[row.get("failed", 0) for row in tx_series],
                name="Fallidas", marker_color="rgba(248,113,113,0.9)",
            ))
            fig.update_layout(barmode="stack")
        else:
            fig.add_annotation(text="Sin intentos del generador", showarrow=False,
                               font={"color": SLATE})
        render_chart(fig)

    with columns[0]:
        fig = base_figure("Recuperación de failovers (RTO)")
        rows = [row for row in failovers if row.get("recovery_s") is not None]
        if rows:
            fig.add_trace(go.Bar(
                x=[fig_timestamp(row["detected_at"]) for row in rows],
                y=[row["recovery_s"] for row in rows],
                name="Recuperación",
                marker_color=[
                    GREEN if row.get("rto_met") else RED for row in rows
                ],
                hovertemplate="%{y:.2f} s<extra></extra>",
            ))
            fig.add_hline(
                y=RTO_TARGET, line_dash="dot", line_color=AMBER,
                annotation_text=f"RTO objetivo {RTO_TARGET:g} s",
                annotation_font_color=AMBER,
            )
            fig.update_yaxes(ticksuffix=" s")
        else:
            fig.add_annotation(
                text="Sin failovers medidos. Ejecute el laboratorio de fallos.",
                showarrow=False, font={"color": SLATE},
            )
        render_chart(fig)

    with columns[1]:
        fig = base_figure("Tamaño de respaldos (KB)")
        history = (backups.get("status") or {}).get("history") or []
        rows = [
            row for row in history
            if row.get("status") == "success"
            and row.get("size_bytes")
            and row.get("finished_at")
        ]
        if rows:
            fig.add_trace(go.Bar(
                x=[fig_timestamp(datetime.fromisoformat(
                    str(row.get("finished_at")).replace("Z", "+00:00")
                ).timestamp()) for row in rows],
                y=[float(row["size_bytes"]) / 1024.0 for row in rows],
                name="Tamaño", marker_color="rgba(34,211,238,0.75)",
                hovertemplate="%{y:.1f} KB<extra></extra>",
            ))
        else:
            fig.add_annotation(text="Sin respaldos completados", showarrow=False,
                               font={"color": SLATE})
        render_chart(fig)


def render_events(events_data: dict[str, Any]) -> None:
    rows = events_data.get("events") or []
    if not rows:
        st.info("Todavía no hay eventos en la ventana seleccionada.")
        return
    frame = pd.DataFrame([
        {
            "Hora local": fmt_ts(row.get("ts_iso")),
            "Tipo": row.get("event_type") or row.get("type"),
            "Nodo": row.get("node") or "—",
            "Descripción": row.get("message") or row.get("description"),
            "Resultado": row.get("result"),
            "Severidad": row.get("severity"),
        }
        for row in rows
    ])
    st.dataframe(
        frame,
        use_container_width=True,
        hide_index=True,
        height=360,
        column_config={
            "Hora local": st.column_config.TextColumn(width="medium"),
            "Tipo": st.column_config.TextColumn(width="small"),
            "Nodo": st.column_config.TextColumn(width="small"),
            "Descripción": st.column_config.TextColumn(width="large"),
            "Resultado": st.column_config.TextColumn(width="small"),
            "Severidad": st.column_config.TextColumn(width="small"),
        },
    )


def render_failure_lab(status: dict[str, Any]) -> None:
    nodes = status.get("nodes") or []
    primary = next((n for n in nodes if n.get("role") == "primary"), None)
    replica = next((n for n in nodes if n.get("role") == "replica"), None)
    primary_name = (primary or {}).get("name") or "postgres-master"
    replica_name = (replica or {}).get("name") or "postgres-replica"

    st.markdown(
        '<div class="note">El failover es real: Patroni promueve la réplica y el '
        'antiguo primario se reincorpora como réplica. El dashboard no puede '
        'provocarlo (no tiene acceso al socket de Docker); use una terminal con '
        'los comandos de abajo. Los botones de esta sección solo ejecutan '
        'acciones seguras.</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Sugerencia: deje el generador de transacciones activo y observe las "
        "tarjetas de disponibilidad, RTO, RPO y el historial de eventos."
    )

    col_left, col_right = st.columns(2, gap="medium")
    with col_left:
        st.markdown("**1. Provocar la caída del primario actual**")
        st.code(
            f"docker compose stop {primary_name}\n"
            f"docker compose ps\n"
            f"docker compose exec {replica_name} patronictl -c /etc/patroni/patroni.yml list",
            language="powershell",
        )
        st.markdown("**2. Reincorporar el nodo caído**")
        st.code(
            f"docker compose start {primary_name}\n"
            f"docker compose exec {replica_name} patronictl -c /etc/patroni/patroni.yml list",
            language="powershell",
        )
        st.caption(
            "El nodo que se detiene es el que la tarjeta 'Nodo primario' muestre "
            "en ese momento. Tras la promoción, los roles se intercambian."
        )

    with col_right:
        st.markdown("**Acciones seguras**")
        action_col_1, action_col_2 = st.columns(2)
        with action_col_1:
            verify_clicked = st.button("Verificar replicación", key="btn_verify",
                                       use_container_width=True)
        with action_col_2:
            backup_clicked = st.button("Solicitar respaldo", key="btn_backup",
                                       use_container_width=True)

        if verify_clicked:
            with st.spinner("Escribiendo latido y verificando la réplica..."):
                result, error = post_json("/api/actions/verify-replication", timeout=40)
            st.session_state["verify_result"] = result or {"error": error}
        if backup_clicked:
            with st.spinner("Solicitando respaldo al servicio..."):
                result, error = post_json("/api/actions/backup", timeout=10)
            st.session_state["backup_result"] = result or {"error": error}

        verify_result = st.session_state.get("verify_result")
        if verify_result:
            if verify_result.get("error"):
                st.error(verify_result["error"])
            else:
                ok = verify_result.get("ok")
                st.markdown(
                    f'<div class="{"ok-note" if ok else "note"}">'
                    f'{"Verificación correcta" if ok else "Verificación con hallazgos"}'
                    f' · {verify_result.get("elapsed_seconds", "?")} s</div>',
                    unsafe_allow_html=True,
                )
                for check in verify_result.get("checks") or []:
                    mark = '<span class="check-ok">OK</span>' if check.get("ok") \
                        else '<span class="check-bad">FALLA</span>'
                    st.markdown(
                        f'<div class="check-row">{mark} · '
                        f'<b>{esc(check.get("name"))}</b> · '
                        f'{esc(check.get("detail"))}</div>',
                        unsafe_allow_html=True,
                    )

        backup_result = st.session_state.get("backup_result")
        if backup_result:
            if backup_result.get("error"):
                st.error(backup_result["error"])
            else:
                st.success(backup_result.get("message") or "Solicitud enviada.")
                st.caption(
                    "El respaldo se ejecuta en el nodo de respaldo, no en los "
                    "nodos replicados. Consulte el resultado en la tarjeta de respaldos."
                )

    st.divider()
    st.markdown("**Restauración controlada**")
    allow_restore = bool(status.get("allow_restore"))
    if not allow_restore:
        st.markdown(
            '<div class="note">Restauración deshabilitada por defecto '
            '(ALLOW_RESTORE=false). El script CLI solo restaura a una base '
            'temporal <code>cluster_demo_restore_*</code>; nunca sobrescribe '
            '<code>cluster_demo</code>.</div>',
            unsafe_allow_html=True,
        )
        st.button("Restaurar último respaldo a base temporal", disabled=True,
                  use_container_width=True)
        st.code(
            "$env:ALLOW_RESTORE='true'\n"
            "docker compose up -d --force-recreate postgres-backup monitor dashboard\n"
            ".\\scripts\\restore-backup.ps1 -Latest",
            language="powershell",
        )
    else:
        st.warning(
            "ALLOW_RESTORE=true. La restauración se hará en una base temporal "
            "nueva; la base activa no se sobrescribe."
        )
        confirm = st.checkbox(
            "Confirmo que deseo restaurar a una base temporal y verificar los conteos.",
            key="restore_confirm",
        )
        filename = st.text_input(
            "Archivo .dump (vacío = último respaldo)", key="restore_filename",
            placeholder="cluster_demo_20260101T000000Z.dump",
        )
        if st.button("Restaurar a base temporal", disabled=not confirm,
                     use_container_width=True):
            payload = {
                "filename": filename.strip() or None,
                "latest": not bool(filename.strip()),
            }
            with st.spinner("Restaurando y verificando la base temporal..."):
                result, error = post_json("/api/actions/restore", payload=payload,
                                          timeout=900)
            if error:
                st.error(error)
            else:
                restore = result.get("restore") or {}
                st.success(
                    f"Restauración verificada en {restore.get('database')} "
                    f"({restore.get('duration_s')} s)."
                )
                st.json(restore.get("counts") or {}, expanded=True)


def render_footer(status: dict[str, Any]) -> None:
    service = status.get("service") or {}
    monitor = status.get("monitor") or {}
    st.markdown(
        '<div class="footer">'
        "ClusterDB Monitor · demostración local de alta disponibilidad con "
        "PostgreSQL + Patroni + etcd + HAProxy.<br>"
        f"Última muestra: {esc(service.get('last_sample_iso') or 'sin datos')} · "
        f"antigüedad {fmt_duration(service.get('sample_age_seconds'))} · "
        f"monitor con {monitor.get('samples', 0)} muestras en SQLite "
        f"({esc(monitor.get('db_path'))}).<br>"
        "La disponibilidad se mide contra HAProxy exigiendo un primario que acepte "
        "escrituras. El RPO mostrado es pérdida verificada, no el objetivo "
        "configurado. Los objetivos SLA/RTO/RPO son metas de la demo."
        "</div>",
        unsafe_allow_html=True,
    )


def render_body(window_minutes: int) -> None:
    status, status_error = get_json("/api/status")
    if status is None:
        st.error(
            "No se pudo consultar el monitor. Verifique que el contenedor "
            f"'monitor' esté saludable. Detalle: {status_error}"
        )
        return

    params = {"window_minutes": window_minutes}
    kpis, kpis_error = get_json("/api/kpis", params=params)
    timeseries, ts_error = get_json("/api/timeseries", params=params)
    events_data, events_error = get_json("/api/events", params={"window_minutes": window_minutes, "limit": 250})
    backups, backups_error = get_json("/api/backups")

    now = float(status.get("generated_at") or 0)
    st.markdown(
        '<div class="hero">'
        "<div><h1>ClusterDB Monitor</h1>"
        "<p>PostgreSQL HA local · Patroni + etcd + HAProxy · métricas reales</p></div>"
        f'<div class="pill"><span class="dot" style="background:{GREEN if (status.get("service") or {}).get("available") else RED}"></span>'
        f'Actualizado {esc(fmt_ts(status.get("generated_iso")))}</div>'
        "</div>",
        unsafe_allow_html=True,
    )

    if kpis_error or ts_error or events_error or backups_error:
        st.warning(
            "Algunos datos no se pudieron cargar: "
            + " | ".join(str(e) for e in [kpis_error, ts_error, events_error, backups_error] if e)
        )

    section("Estado del clúster", "Tarjetas en vivo desde la API interna del monitor")
    render_status_cards(status)

    section("KPIs", f"Ventana seleccionada · {window_minutes if window_minutes else 'todo lo observado'}")
    if kpis:
        render_kpis(kpis, now)
    else:
        st.info("Sin datos de KPIs disponibles.")

    section("Gráficos", "Serie temporal derivada de muestras reales")
    render_charts(timeseries or {}, backups or {})

    section("Historial de eventos", "Incidentes, promociones, respaldos y verificaciones")
    render_events(events_data or {})

    section("Laboratorio de fallos", "Failover real, sin simulación en el dashboard")
    render_failure_lab(status)

    render_footer(status)


@st.fragment(run_every=f"{REFRESH_SECONDS}s")
def live_body(window_minutes: int) -> None:
    render_body(window_minutes)


def main() -> None:
    st.sidebar.markdown("## 🛰️ ClusterDB")
    st.sidebar.caption("Monitor de alta disponibilidad PostgreSQL")
    st.sidebar.divider()

    window_label = st.sidebar.selectbox(
        "Ventana de análisis",
        options=[
            "15 minutos",
            "1 hora",
            "6 horas",
            "24 horas",
            "Todo lo observado",
        ],
        index=1,
    )
    window_minutes = {
        "15 minutos": 15,
        "1 hora": 60,
        "6 horas": 360,
        "24 horas": 1440,
        "Todo lo observado": 0,
    }[window_label]

    auto_refresh = st.sidebar.checkbox("Actualización automática", value=True)
    st.sidebar.caption(f"Intervalo: {REFRESH_SECONDS} s")
    if st.sidebar.button("Actualizar ahora", use_container_width=True):
        st.rerun()

    st.sidebar.divider()
    st.sidebar.markdown("**Objetivos de la demo**")
    st.sidebar.markdown(
        f"- SLA: **{SLA_TARGET:g}%**\n"
        f"- RTO: **{RTO_TARGET:g} s**\n"
        f"- RPO: **{RPO_TARGET:g} s** (referencia)"
    )
    st.sidebar.caption(
        "Los objetivos no son resultados. El RPO se reporta como pérdida "
        "verificada después de cada failover."
    )
    st.sidebar.divider()
    st.sidebar.markdown(
        "**Acceso local**\n\n"
        "Dashboard: `http://127.0.0.1:8501`\n\n"
        "PostgreSQL vía HAProxy: `127.0.0.1:15432`\n\n"
        "HAProxy stats: `http://127.0.0.1:7000`"
    )

    if auto_refresh:
        live_body(window_minutes)
    else:
        render_body(window_minutes)


if __name__ == "__main__":
    main()
