-- Roles de aplicación. Se ejecuta como superusuario postgres en el bootstrap
-- de Patroni. Es idempotente: también sirve para reparar permisos.
\set ON_ERROR_STOP on

-- Rol de escritura usado por el generador de transacciones (a través de HAProxy).
SELECT format('CREATE ROLE app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app') \gexec
ALTER ROLE app WITH LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- Rol de respaldos: solo lectura del clúster, pero con CREATEDB para poder
-- restaurar en una base temporal aislada. Nunca restaura sobre cluster_demo.
SELECT format('CREATE ROLE backup LOGIN CREATEDB PASSWORD %L', :'backup_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'backup') \gexec
ALTER ROLE backup WITH LOGIN PASSWORD :'backup_password' NOSUPERUSER CREATEDB NOCREATEROLE;

-- Rol de monitoreo: lectura + funciones de estadísticas. Se le permite insertar
-- únicamente en ha_heartbeat para la verificación activa de replicación.
SELECT format('CREATE ROLE monitor LOGIN PASSWORD %L', :'monitor_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monitor') \gexec
ALTER ROLE monitor WITH LOGIN PASSWORD :'monitor_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT pg_read_all_data TO backup;
GRANT pg_read_all_data TO monitor;
GRANT pg_monitor TO monitor;

-- Sin privilegios de superusuario para los roles de aplicación.
ALTER ROLE app SET statement_timeout = '15s';
ALTER ROLE app SET idle_in_transaction_session_timeout = '30s';
