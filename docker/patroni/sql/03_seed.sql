-- Datos iniciales deterministas e idempotentes.
-- Se pueden volver a ejecutar sin duplicar filas.
\set ON_ERROR_STOP on

INSERT INTO clientes (nombre, email, ciudad) VALUES
    ('Ana Martínez',     'ana.martinez@example.com',     'San Salvador'),
    ('Carlos Hernández', 'carlos.hernandez@example.com', 'Santa Ana'),
    ('María López',      'maria.lopez@example.com',      'San Miguel'),
    ('José Ramírez',     'jose.ramirez@example.com',     'Soyapango'),
    ('Sofía Castro',     'sofia.castro@example.com',     'Antiguo Cuscatlán'),
    ('Diego Flores',     'diego.flores@example.com',     'Mejicanos'),
    ('Valeria Reyes',    'valeria.reyes@example.com',    'Santa Tecla'),
    ('Luis Morales',     'luis.morales@example.com',     'Apopa'),
    ('Camila Ortiz',     'camila.ortiz@example.com',     'Ilopango'),
    ('Andrés Vargas',    'andres.vargas@example.com',    'San Marcos'),
    ('Daniela Rivas',    'daniela.rivas@example.com',    'Sonsonate'),
    ('Miguel Peña',      'miguel.pena@example.com',      'La Libertad'),
    ('Fernanda Cruz',    'fernanda.cruz@example.com',    'Usulután'),
    ('Ricardo Salazar',  'ricardo.salazar@example.com',  'Zacatecoluca'),
    ('Paola Méndez',     'paola.mendez@example.com',     'Cojutepeque'),
    ('Jorge Aguilar',    'jorge.aguilar@example.com',    'Chalatenango'),
    ('Natalia Romero',   'natalia.romero@example.com',   'Ahuachapán'),
    ('Óscar Molina',     'oscar.molina@example.com',     'Sensuntepeque'),
    ('Elena Navarro',    'elena.navarro@example.com',    'San Vicente'),
    ('Pablo Guzmán',     'pablo.guzman@example.com',     'La Unión')
ON CONFLICT (email) DO NOTHING;

INSERT INTO productos (sku, nombre, precio, stock) VALUES
    ('LAP-001', 'Laptop Pro 14',            1250.00, 800),
    ('MON-002', 'Monitor UltraWide 29"',      420.00, 900),
    ('TEC-003', 'Teclado mecánico',            95.50, 1200),
    ('MOU-004', 'Mouse inalámbrico',           38.75, 1500),
    ('SSD-005', 'SSD NVMe 1 TB',              135.00, 1000),
    ('RAM-006', 'Memoria RAM 16 GB',           82.00, 1100),
    ('AUD-007', 'Audífonos Bluetooth',         65.00, 1300),
    ('CAM-008', 'Cámara web Full HD',          59.99, 950),
    ('DOC-009', 'Docking station USB-C',      145.00, 700),
    ('SIL-010', 'Silla ergonómica',           310.00, 300),
    ('MES-011', 'Escritorio ajustable',       480.00, 250),
    ('UPS-012', 'UPS 1200 VA',                175.00, 500)
ON CONFLICT (sku) DO NOTHING;

-- Diez ventas históricas con detalle. Se usa un identificador determinista
-- para que un segundo bootstrap no duplique información.
DO $$
DECLARE
    i            integer;
    v_cliente_id integer;
    v_producto_id integer;
    v_precio     numeric(12,2);
    v_cantidad   integer;
    v_venta_id   bigint;
    v_key        uuid;
BEGIN
    IF EXISTS (SELECT 1 FROM ventas WHERE origen = 'seed') THEN
        RETURN;
    END IF;

    FOR i IN 1..10 LOOP
        SELECT id INTO v_cliente_id
          FROM clientes ORDER BY id OFFSET ((i - 1) % 20) LIMIT 1;

        SELECT id, precio INTO v_producto_id, v_precio
          FROM productos ORDER BY id OFFSET ((i - 1) % 12) LIMIT 1;

        v_cantidad := (i % 3) + 1;
        v_key := md5('seed-venta-' || i)::uuid;

        INSERT INTO ventas (idempotency_key, cliente_id, total, origen, created_at)
        VALUES (v_key, v_cliente_id, v_precio * v_cantidad, 'seed',
                now() - make_interval(hours => i))
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id INTO v_venta_id;

        IF v_venta_id IS NULL THEN
            SELECT id INTO v_venta_id FROM ventas WHERE idempotency_key = v_key;
        END IF;

        INSERT INTO detalle_ventas (venta_id, producto_id, cantidad, precio_unitario)
        SELECT v_venta_id, v_producto_id, v_cantidad, v_precio
        WHERE NOT EXISTS (
            SELECT 1 FROM detalle_ventas WHERE venta_id = v_venta_id
        );
    END LOOP;
END $$;

INSERT INTO ha_heartbeat (source, note)
SELECT 'seed', 'latido inicial de la base de demostración'
WHERE NOT EXISTS (SELECT 1 FROM ha_heartbeat);

ANALYZE clientes;
ANALYZE productos;
ANALYZE ventas;
ANALYZE detalle_ventas;
