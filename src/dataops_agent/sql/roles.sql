-- Roles y metadatos de DataOps Agent.
-- Plantilla: los marcadores entre dobles guiones bajos se sustituyen antes de ejecutar (docker/postgres/init
-- con sed y dataops_agent.admin con Python). Se ejecuta como superusuario y es idempotente.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '__LECTOR__') THEN
        CREATE ROLE __LECTOR__ LOGIN PASSWORD '__LECTOR_PASSWORD__';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '__ESCRITOR__') THEN
        CREATE ROLE __ESCRITOR__ LOGIN PASSWORD '__ESCRITOR_PASSWORD__';
    END IF;
END
$$;

-- Defensa en profundidad: aunque el servidor MCP ya fija estos valores por sesión,
-- se configuran también en el propio rol para que cualquier cliente los herede
-- (por ejemplo Metabase, que se conecta con el rol lector).
ALTER ROLE __LECTOR__ SET default_transaction_read_only = on;
ALTER ROLE __LECTOR__ SET statement_timeout = '15s';
ALTER ROLE __ESCRITOR__ SET statement_timeout = '60s';

-- Nadie salvo el administrador puede crear objetos en public.
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- Esquema de metadatos: registro de auditoría.
CREATE SCHEMA IF NOT EXISTS dataops_meta;

CREATE TABLE IF NOT EXISTS dataops_meta.auditoria (
    id           uuid PRIMARY KEY,
    instante     timestamptz NOT NULL,
    actor        text        NOT NULL,
    rol_bd       text,
    esquema      text,
    herramienta  text        NOT NULL,
    operacion    text        NOT NULL,
    plan_id      text,
    sql          text,
    destructiva  boolean     NOT NULL DEFAULT false,
    resultado    text        NOT NULL
                 CHECK (resultado IN ('ok', 'error', 'rechazado', 'pendiente_confirmacion')),
    detalle      jsonb,
    duracion_ms  integer
);
CREATE INDEX IF NOT EXISTS auditoria_instante_idx ON dataops_meta.auditoria (instante);

-- El escritor solo puede AÑADIR registros de auditoría (ni UPDATE ni DELETE):
-- la auditoría es de solo anexado por privilegios, no por convenio.
GRANT USAGE ON SCHEMA dataops_meta TO __ESCRITOR__;
GRANT INSERT, SELECT ON dataops_meta.auditoria TO __ESCRITOR__;
