# PostgreSQL 16 + Patroni + cliente de etcd v3
# Versiones fijadas: no se usan etiquetas "latest".
FROM postgres:16.4-bookworm

ARG PATRONI_VERSION=4.1.5
ARG PSYCOPG2_VERSION=2.9.10

ENV PGDATA=/var/lib/postgresql/data/pgdata \
    PATH=/opt/patroni/bin:$PATH \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-pip \
        curl \
        jq \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/patroni \
    && /opt/patroni/bin/pip install --no-cache-dir --upgrade pip==24.3.1 \
    && /opt/patroni/bin/pip install --no-cache-dir \
        "patroni[etcd3]==${PATRONI_VERSION}" \
        "psycopg2-binary==${PSYCOPG2_VERSION}" \
    && ln -sf /opt/patroni/bin/patroni /usr/local/bin/patroni \
    && ln -sf /opt/patroni/bin/patronictl /usr/local/bin/patronictl

COPY patroni.yml.tmpl /etc/patroni/patroni.yml.tmpl
COPY render_config.py /usr/local/bin/render-patroni-config
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY bootstrap-db.sh /usr/local/bin/bootstrap-db.sh
COPY sql/ /opt/clusterdb/sql/

RUN chmod 0755 /usr/local/bin/entrypoint.sh \
        /usr/local/bin/bootstrap-db.sh \
        /usr/local/bin/render-patroni-config \
    && chmod 0644 /etc/patroni/patroni.yml.tmpl \
    && mkdir -p /run/postgresql /var/log/patroni

EXPOSE 5432 8008

# La imagen oficial trae gosu; el entrypoint ajusta permisos y baja a postgres.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
