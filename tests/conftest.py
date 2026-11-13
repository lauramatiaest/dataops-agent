"""Fixtures compartidas.

Los tests marcados con ``integracion`` necesitan un PostgreSQL real. Se activan
definiendo ``DATAOPS_TEST_DSN`` con un DSN de superusuario, por ejemplo:

    export DATAOPS_TEST_DSN="host=127.0.0.1 port=5432 user=dataops_admin password=... dbname=dataops"

Cada test de integración recibe un esquema de proyecto recién creado y vacío.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from dataops_agent import admin
from dataops_agent.config import Config

DSN_ADMIN = os.environ.get("DATAOPS_TEST_DSN", "")
ESQUEMA_TEST = "proyecto_test"


def _conninfo(dsn: str) -> dict[str, str]:
    return psycopg.conninfo.conninfo_to_dict(dsn)  # type: ignore[return-value]


@pytest.fixture(scope="session")
def cfg_base() -> Config:
    if not DSN_ADMIN:
        pytest.skip("DATAOPS_TEST_DSN no definido: se omiten los tests de integración")
    info = _conninfo(DSN_ADMIN)
    cfg = Config(
        pg_host=str(info.get("host", "127.0.0.1")),
        pg_puerto=int(info.get("port", 5432)),
        pg_bd=str(info.get("dbname", "dataops")),
        esquema=ESQUEMA_TEST,
        lector_usuario="dataops_lector_test",
        lector_password="lector_test_123",
        escritor_usuario="dataops_escritor_test",
        escritor_password="escritor_test_123",
        timeout_ms=5_000,
        max_filas=50,
    )
    admin.inicializar(DSN_ADMIN, cfg)
    return cfg


@pytest.fixture
def cfg(cfg_base: Config, tmp_path: Path) -> Iterator[Config]:
    """Config con un esquema de proyecto vacío y directorios temporales."""
    admin.reiniciar_proyecto(DSN_ADMIN, cfg_base)
    dir_datos = tmp_path / "datos"
    dir_datos.mkdir()
    yield replace(cfg_base, dir_datos=dir_datos, dir_auditoria=tmp_path / "auditoria")


@pytest.fixture
def conn_admin(cfg: Config) -> Iterator[psycopg.Connection]:
    """Conexión de superusuario para preparar y comprobar estados en los tests."""
    with psycopg.connect(DSN_ADMIN, autocommit=True) as conn:
        yield conn


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Todo test que use las fixtures de BD queda marcado como de integración.
    for item in items:
        if {"cfg", "cfg_base", "conn_admin"} & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integracion)
