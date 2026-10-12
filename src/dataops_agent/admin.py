"""Tareas de administración: crear roles, crear y reiniciar esquemas de proyecto.

Estas operaciones necesitan un usuario administrador de PostgreSQL y NUNCA se exponen
como herramientas MCP: el agente no puede crear esquemas ni roles. Las usan el
arranque de Docker (vía las plantillas SQL), los tests de integración y la evaluación.

Uso por línea de comandos:
    python -m dataops_agent.admin inicializar --dsn-admin "postgresql://..."
    python -m dataops_agent.admin reiniciar-proyecto --dsn-admin "..." --esquema proyecto_demo
"""

from __future__ import annotations

import argparse
import os
import re
from importlib import resources

import psycopg

from dataops_agent.config import Config, validar_identificador

_PATRON_PASSWORD = re.compile(r"^[A-Za-z0-9_\-.]{8,64}$")


def _plantilla(nombre: str) -> str:
    return resources.files("dataops_agent.sql").joinpath(nombre).read_text(encoding="utf-8")


def _sustituir(plantilla: str, valores: dict[str, str]) -> str:
    for clave, valor in valores.items():
        plantilla = plantilla.replace(f"__{clave}__", valor)
    pendientes = re.findall(r"__[A-Z_]+__", plantilla)
    if pendientes:
        raise ValueError(f"Marcadores sin sustituir: {pendientes}")
    return plantilla


def _validar_password(password: str, rol: str) -> str:
    # La contraseña se inserta en un literal SQL; se limita el alfabeto para que
    # no pueda romper el literal (no hay parámetros en CREATE ROLE).
    if not _PATRON_PASSWORD.match(password):
        raise ValueError(
            f"Contraseña de {rol} no válida: usa 8-64 caracteres [A-Za-z0-9_-.]."
        )
    return password


def sql_roles(cfg: Config) -> str:
    """SQL de creación de roles y del esquema de metadatos."""
    return _sustituir(
        _plantilla("roles.sql"),
        {
            "LECTOR": validar_identificador(cfg.lector_usuario, "rol lector"),
            "ESCRITOR": validar_identificador(cfg.escritor_usuario, "rol escritor"),
            "LECTOR_PASSWORD": _validar_password(cfg.lector_password, "lector"),
            "ESCRITOR_PASSWORD": _validar_password(cfg.escritor_password, "escritor"),
        },
    )


def sql_proyecto(cfg: Config, esquema: str | None = None) -> str:
    """SQL de creación del esquema de un proyecto con sus permisos."""
    return _sustituir(
        _plantilla("proyecto.sql"),
        {
            "ESQUEMA": validar_identificador(esquema or cfg.esquema, "esquema"),
            "LECTOR": validar_identificador(cfg.lector_usuario, "rol lector"),
            "ESCRITOR": validar_identificador(cfg.escritor_usuario, "rol escritor"),
        },
    )


def inicializar(dsn_admin: str, cfg: Config) -> None:
    """Crea roles, metadatos y el esquema del proyecto configurado."""
    with psycopg.connect(dsn_admin, autocommit=True) as conn:
        conn.execute(sql_roles(cfg))
        conn.execute(sql_proyecto(cfg))


def reiniciar_proyecto(dsn_admin: str, cfg: Config, esquema: str | None = None) -> None:
    """Borra y recrea el esquema de un proyecto (solo administración y evaluación)."""
    nombre = validar_identificador(esquema or cfg.esquema, "esquema")
    with psycopg.connect(dsn_admin, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{nombre}" CASCADE')
        conn.execute(sql_proyecto(cfg, nombre))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Administración de DataOps Agent")
    parser.add_argument("accion", choices=["inicializar", "reiniciar-proyecto"])
    parser.add_argument(
        "--dsn-admin",
        default=os.environ.get("DATAOPS_DSN_ADMIN", ""),
        help="DSN de un superusuario (o variable DATAOPS_DSN_ADMIN)",
    )
    parser.add_argument("--esquema", default=None)
    args = parser.parse_args(argv)
    if not args.dsn_admin:
        parser.error("Falta --dsn-admin o DATAOPS_DSN_ADMIN")
    cfg = Config.desde_entorno()
    if args.accion == "inicializar":
        inicializar(args.dsn_admin, cfg)
    else:
        reiniciar_proyecto(args.dsn_admin, cfg, args.esquema)
    print(f"OK: {args.accion}")


if __name__ == "__main__":
    main()
