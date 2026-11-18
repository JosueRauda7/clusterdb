FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r monitor \
    && useradd -r -g monitor -u 10001 -m -d /home/monitor monitor \
    && mkdir -p /data /generator-data \
    && chown -R monitor:monitor /data /home/monitor \
    && chmod 0777 /generator-data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py collector.py kpi.py store.py ./

USER monitor
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
