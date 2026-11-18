FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r txgen \
    && useradd -r -g txgen -u 10002 -m -d /home/txgen txgen \
    && mkdir -p /data \
    && chmod 0777 /data \
    && chown -R txgen:txgen /home/txgen

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY generator.py ./

USER txgen
CMD ["python", "generator.py"]
