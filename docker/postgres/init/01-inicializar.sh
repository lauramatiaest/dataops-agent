#!/bin/bash
# Se ejecuta UNA vez, cuando el volumen de PostgreSQL está vacío
# (mecanismo estándar /docker-entrypoint-initdb.d de la imagen oficial).
# Reutiliza las mismas plantillas SQL que dataops_agent.admin para no duplicar lógica.
set -euo pipefail

sustituir() {
    sed -e "s/__LECTOR__/${DATAOPS_LECTOR_USUARIO}/g" \
        -e "s/__ESCRITOR__/${DATAOPS_ESCRITOR_USUARIO}/g" \
        -e "s/__LECTOR_PASSWORD__/${DATAOPS_LECTOR_PASSWORD}/g" \
        -e "s/__ESCRITOR_PASSWORD__/${DATAOPS_ESCRITOR_PASSWORD}/g" \
        -e "s/__ESQUEMA__/${DATAOPS_ESQUEMA}/g" \
        "$1"
}

ejecutar() {
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$1"
}

echo ">> DataOps: roles, auditoría y esquema del proyecto '${DATAOPS_ESQUEMA}'"
sustituir /plantillas/roles.sql    | ejecutar "$POSTGRES_DB"
sustituir /plantillas/proyecto.sql | ejecutar "$POSTGRES_DB"

echo ">> Metabase: base de datos de aplicación (metadatos de Metabase, separada de los datos)"
ejecutar "$POSTGRES_DB" <<SQL
CREATE ROLE ${MB_APP_USUARIO} LOGIN PASSWORD '${MB_APP_PASSWORD}';
CREATE DATABASE ${MB_APP_BD} OWNER ${MB_APP_USUARIO};
SQL
