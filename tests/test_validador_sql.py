"""Tests del validador SQL (primera barrera de seguridad)."""

from __future__ import annotations

import pytest

from dataops_agent.seguridad import Modo, SQLRechazado, ValidadorSQL

ESQUEMA = "proyecto_demo"


@pytest.fixture
def validador() -> ValidadorSQL:
    return ValidadorSQL(ESQUEMA)


# ----------------------------------------------------------------- lecturas válidas
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM pedidos",
        "SELECT * FROM proyecto_demo.pedidos;",
        "SELECT c.nombre, sum(l.cantidad) FROM clientes c "
        "JOIN pedidos p ON p.cliente_id = c.cliente_id "
        "JOIN lineas_pedido l ON l.pedido_id = p.pedido_id GROUP BY c.nombre",
        "WITH t AS (SELECT * FROM pedidos) SELECT count(*) FROM t",
        "SELECT estado FROM pedidos UNION SELECT 'x'",
        "SELECT * FROM generate_series(1, 3)",
        "SELECT pg_typeof(1)",
        "SELECT date_trunc('month', fecha_pedido) AS mes, count(*) FROM pedidos GROUP BY 1",
    ],
)
def test_lecturas_validas(validador: ValidadorSQL, sql: str) -> None:
    resultado = validador.validar(sql, Modo.LECTURA)
    assert resultado.valido, resultado.errores
    assert resultado.tipo == "SELECT"
    assert not resultado.destructiva


# ------------------------------------------------------------- lecturas rechazadas
@pytest.mark.parametrize(
    ("sql", "fragmento_error"),
    [
        ("DELETE FROM pedidos", "Solo se permiten consultas SELECT"),
        ("DROP TABLE pedidos", "Solo se permiten consultas SELECT"),
        ("SELECT 1; DROP TABLE pedidos", "una sentencia"),
        ("SELECT * INTO copia FROM pedidos", "INTO"),
        ("SELECT * FROM pedidos FOR UPDATE", "FOR UPDATE"),
        ("WITH d AS (DELETE FROM pedidos RETURNING *) SELECT * FROM d", "anidada"),
        ("SELECT * FROM public.clientes", "fuera del esquema"),
        ("SELECT * FROM otro_proyecto.pedidos", "fuera del esquema"),
        ("SELECT * FROM otra_bd.proyecto_demo.pedidos", "otra base de datos"),
        ("SELECT * FROM pg_authid", "catálogo"),
        ("SELECT * FROM pg_catalog.pg_roles", "fuera del esquema"),
        ("SELECT * FROM information_schema.tables", "fuera del esquema"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT pg_sleep(100)", "pg_sleep"),
        ("SELECT query_to_xml('select * from public.x', true, true, '')", "query_to_xml"),
        ("SELECT set_config('search_path', 'public', false)", "set_config"),
        ("SELECT dblink('host=x', 'select 1')", "dblink"),
        ("SELECT \"pg_read_file\"('/etc/passwd')", "pg_read_file"),  # entre comillas
        ("SELECT \"PG_SLEEP\"(5)", "pg_sleep"),
        ("SELECT ts_stat('select rolname from pg_roles')", "ts_stat"),
        ("SELECT ts_rewrite('a'::tsquery, 'select 1')", "ts_rewrite"),
        ("COPY pedidos TO '/tmp/x'", "Solo se permiten"),
        ("SET ROLE postgres", "Solo se permiten"),
        ("", "ninguna sentencia"),
        ("SELEC * FORM x", ""),
    ],
)
def test_lecturas_rechazadas(validador: ValidadorSQL, sql: str, fragmento_error: str) -> None:
    resultado = validador.validar(sql, Modo.LECTURA)
    assert not resultado.valido
    assert any(fragmento_error in e for e in resultado.errores), resultado.errores


# ---------------------------------------------------------------- escrituras
@pytest.mark.parametrize(
    ("sql", "tipo", "destructiva"),
    [
        ("CREATE TABLE clientes (cliente_id integer PRIMARY KEY, nombre text)",
         "CREATE TABLE", False),
        ("CREATE TABLE proyecto_demo.resumen AS SELECT 1 AS x", "CREATE TABLE", False),
        ("CREATE OR REPLACE VIEW v_ventas AS SELECT * FROM pedidos", "CREATE VIEW", False),
        ("INSERT INTO clientes (cliente_id) VALUES (1)", "INSERT", False),
        ("ALTER TABLE pedidos ADD CONSTRAINT fk_cli FOREIGN KEY (cliente_id) "
         "REFERENCES clientes (cliente_id)", "ALTER TABLE", False),
        ("ALTER TABLE pedidos DROP COLUMN canal", "ALTER TABLE", True),
        ("DROP TABLE pedidos", "DROP TABLE", True),
        ("DROP VIEW IF EXISTS v_ventas", "DROP VIEW", True),
        ("TRUNCATE pedidos", "TRUNCATE", True),
        ("DELETE FROM pedidos WHERE estado = 'cancelado'", "DELETE", True),
    ],
)
def test_escrituras_validas(
    validador: ValidadorSQL, sql: str, tipo: str, destructiva: bool
) -> None:
    resultado = validador.validar(sql, Modo.ESCRITURA)
    assert resultado.valido, resultado.errores
    assert resultado.tipo == tipo
    assert resultado.destructiva is destructiva


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE pedidos SET estado = 'x'",
        "GRANT SELECT ON pedidos TO PUBLIC",
        "CREATE INDEX i ON pedidos (estado)",
        "CREATE SCHEMA otro",
        "DROP SCHEMA proyecto_demo CASCADE",
        "CREATE TABLE public.x (i int)",
        "ALTER TABLE pedidos ADD CONSTRAINT fk FOREIGN KEY (x) REFERENCES public.y (x)",
        "INSERT INTO pedidos SELECT * FROM otro.pedidos",
        "CREATE VIEW v AS SELECT * FROM pg_catalog.pg_shadow",
        "DO $$ BEGIN PERFORM 1; END $$",
        "VACUUM pedidos",
        "BEGIN",
        "INSERT INTO a VALUES (1); DELETE FROM a",
    ],
)
def test_escrituras_rechazadas(validador: ValidadorSQL, sql: str) -> None:
    assert not validador.validar(sql, Modo.ESCRITURA).valido


def test_tablas_referenciadas(validador: ValidadorSQL) -> None:
    resultado = validador.validar(
        "WITH t AS (SELECT * FROM pedidos) SELECT * FROM t JOIN clientes USING (cliente_id)",
        Modo.LECTURA,
    )
    # La CTE "t" no cuenta como tabla.
    assert resultado.tablas == ("clientes", "pedidos")


def test_exigir_lanza_excepcion(validador: ValidadorSQL) -> None:
    with pytest.raises(SQLRechazado) as info:
        validador.exigir("DROP TABLE x", Modo.LECTURA)
    assert info.value.errores


def test_esquema_sin_distinguir_mayusculas() -> None:
    resultado = ValidadorSQL("proyecto_demo").validar(
        'SELECT * FROM PROYECTO_DEMO.pedidos', Modo.LECTURA
    )
    assert resultado.valido
