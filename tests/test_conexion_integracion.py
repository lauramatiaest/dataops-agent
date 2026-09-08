"""Barreras de sesión y privilegios contra un PostgreSQL real."""

from __future__ import annotations

import psycopg
import pytest

from dataops_agent.auditoria import EventoAuditoria, Resultado
from dataops_agent.conexion import GestorConexiones
from dataops_agent.config import Config


def test_search_path_y_timeout(cfg: Config) -> None:
    gestor = GestorConexiones(cfg)
    with gestor.lectura() as conn:
        assert conn.execute("SHOW search_path").fetchone()[0] == cfg.esquema
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "5s"


def test_lectura_no_puede_escribir(cfg: Config) -> None:
    with GestorConexiones(cfg).lectura() as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("CREATE TABLE x (i integer)")


def test_timeout_corta_consultas_largas(cfg: Config) -> None:
    # pg_sleep la bloquea el validador; aquí se prueba la barrera de la BD por debajo.
    with GestorConexiones(cfg).lectura() as conn, pytest.raises(psycopg.errors.QueryCanceled):
        conn.execute("SELECT pg_sleep(10)")


def test_escritor_limitado_a_su_esquema(cfg: Config) -> None:
    gestor = GestorConexiones(cfg)
    with gestor.escritura() as conn:
        conn.execute("CREATE TABLE t (i integer)")  # en el esquema del proyecto: OK
    with gestor.escritura() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("CREATE TABLE public.t (i integer)")
    with gestor.lectura() as conn:  # el lector ve lo que crea el escritor
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0


def test_auditoria_solo_anexado(cfg: Config, conn_admin: psycopg.Connection) -> None:
    gestor = GestorConexiones(cfg)
    evento = EventoAuditoria("prueba", "TEST", Resultado.OK, detalle={"n": 1})
    gestor.escribir_auditoria(evento)
    fila = conn_admin.execute(
        "SELECT actor, resultado, detalle FROM dataops_meta.auditoria WHERE id = %s",
        (evento.id,),
    ).fetchone()
    assert fila == ("", "ok", {"n": 1})
    with gestor.escritura() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM dataops_meta.auditoria")
