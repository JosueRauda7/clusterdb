-- Esquema de demostración de ClusterDB Monitor.
-- Todas las tablas usan timestamptz y la hora del servidor (UTC) para que las
-- mediciones sean comparables entre nodos y contenedores.
\set ON_ERROR_STOP on

REVOKE ALL ON DATABASE cluster_demo FROM PUBLIC;
GRANT CONNECT ON DATABASE cluster_demo TO app, backup, monitor;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO app, backup, monitor;

CREATE TABLE IF NOT EXISTS clientes (
    id          serial PRIMARY KEY,
    nombre      text NOT NULL,
    email       text NOT NULL UNIQUE,
    ciudad      text NOT NULL DEFAULT 'San Salvador',
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS productos (
    id          serial PRIMARY KEY,
    sku         text NOT NULL UNIQUE,
    nombre      text NOT NULL,
    precio      numeric(12,2) NOT NULL CHECK (precio > 0),
    stock       integer NOT NULL DEFAULT 0 CHECK (stock >= 0),
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS ventas (
    id                bigserial PRIMARY KEY,
    idempotency_key   uuid NOT NULL UNIQUE,
    cliente_id        integer NOT NULL REFERENCES clientes(id),
    total             numeric(12,2) NOT NULL CHECK (total >= 0),
    estado            text NOT NULL DEFAULT 'completada',
    origen            text NOT NULL DEFAULT 'txgen',
    created_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS detalle_ventas (
    id               bigserial PRIMARY KEY,
    venta_id         bigint NOT NULL REFERENCES ventas(id) ON DELETE CASCADE,
    producto_id      integer NOT NULL REFERENCES productos(id),
    cantidad         integer NOT NULL CHECK (cantidad > 0),
    precio_unitario  numeric(12,2) NOT NULL CHECK (precio_unitario >= 0),
    subtotal         numeric(12,2) GENERATED ALWAYS AS
                     (cantidad * precio_unitario) STORED
);

-- Latido insertado por el generador en cada venta confirmada. El monitor lo usa
-- para verificar, después de un failover, que la última transacción confirmada
-- sobrevivió en el nodo promovido (medición real del RPO observado).
CREATE TABLE IF NOT EXISTS ha_heartbeat (
    id          bigserial PRIMARY KEY,
    source      text NOT NULL DEFAULT 'txgen',
    note        text,
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_ventas_created_at ON ventas (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ventas_cliente ON ventas (cliente_id);
CREATE INDEX IF NOT EXISTS idx_detalle_venta ON detalle_ventas (venta_id);
CREATE INDEX IF NOT EXISTS idx_heartbeat_created_at ON ha_heartbeat (created_at DESC);

GRANT SELECT, INSERT, UPDATE ON clientes, productos, ventas, detalle_ventas TO app;
GRANT SELECT, INSERT ON ha_heartbeat TO app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app;

-- El monitor puede leer todo (pg_read_all_data) y escribir solo el latido de
-- verificación de replicación.
GRANT INSERT ON ha_heartbeat TO monitor;
GRANT USAGE, SELECT ON SEQUENCE ha_heartbeat_id_seq TO monitor;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT ON TABLES TO backup, monitor;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app;

COMMENT ON TABLE ventas IS 'Ventas generadas por la demo; idempotency_key evita duplicados al reintentar tras un failover.';
COMMENT ON TABLE ha_heartbeat IS 'Registro confirmado usado para medir RPO observado tras un failover.';
