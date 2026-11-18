# La base postgres:16.4 ya incluye pg_dump y pg_restore de la MISMA versión
# mayor que el servidor. Python se instala en un venv para el servicio HTTP.
FROM postgres:16.4-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-pip \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip==24.3.1 \
    && mkdir -p /backups /app \
    && chown -R postgres:postgres /backups /app

WORKDIR /app

COPY requirements.txt ./
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY backup_service.py ./

USER postgres
EXPOSE 8080

CMD ["uvicorn", "backup_service:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
