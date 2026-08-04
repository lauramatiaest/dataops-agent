-- Crea (si no existe) el esquema aislado de un proyecto y sus permisos.
-- Plantilla: __ESQUEMA__, __LECTOR__ y __ESCRITOR__ se sustituyen antes de ejecutar.

CREATE SCHEMA IF NOT EXISTS __ESQUEMA__;

-- El escritor puede crear objetos SOLO en este esquema (y es dueño de lo que crea).
GRANT USAGE, CREATE ON SCHEMA __ESQUEMA__ TO __ESCRITOR__;

-- El lector puede leer todo lo que haya y lo que el escritor cree en el futuro.
GRANT USAGE ON SCHEMA __ESQUEMA__ TO __LECTOR__;
GRANT SELECT ON ALL TABLES IN SCHEMA __ESQUEMA__ TO __LECTOR__;
ALTER DEFAULT PRIVILEGES FOR ROLE __ESCRITOR__ IN SCHEMA __ESQUEMA__
    GRANT SELECT ON TABLES TO __LECTOR__;
