"""Smoke test del dashboard con el AppTest oficial de Streamlit.

Se ejecuta DENTRO del contenedor `dashboard`, que ya tiene Streamlit, Plotly,
pandas y acceso a la API interna del monitor. No modifica datos.

Uso:
    docker exec -i clusterdb-dashboard python - < tests/dashboard_smoke.py

Comprueba que la app renderiza sin excepciones, con 6 gráficos Plotly y la tabla
de eventos. Falla si el dashboard no puede consultar el monitor.
"""
from __future__ import annotations

import sys

from streamlit.testing.v1 import AppTest

EXPECTED_CHARTS = 6
EXPECTED_DATAFRAMES = 1

at = AppTest.from_file("/app/app.py", default_timeout=90)
at.run()

exceptions = list(at.exception)
charts = len(at.get("plotly_chart"))
dataframes = len(at.dataframe)
errors = len(at.error)

for exception in exceptions:
    message = getattr(exception, "message", None) or getattr(exception, "value", None)
    print(f"EXCEPCION: {message or exception}")

print(f"excepciones={len(exceptions)}")
print(f"graficos_plotly={charts}")
print(f"dataframes={dataframes}")
print(f"errores_ui={errors}")

failed = False
if exceptions:
    print("FALLA: el dashboard lanzo una excepcion.")
    failed = True
if charts < EXPECTED_CHARTS:
    print(f"FALLA: se esperaban {EXPECTED_CHARTS} graficos Plotly, hay {charts}.")
    failed = True
if dataframes < EXPECTED_DATAFRAMES:
    print(f"FALLA: se esperaba la tabla de eventos ({EXPECTED_DATAFRAMES} dataframe).")
    failed = True

if failed:
    sys.exit(1)

print("DASHBOARD_SMOKE_OK")
