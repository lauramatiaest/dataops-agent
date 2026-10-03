"""Validación determinista: recuentos, sumas de control, unicidad y huérfanos."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import psycopg

from dataops_agent.config import Config
from dataops_agent.ficheros import abrir
from dataops_agent.validaciones import (
    Comprobacion,
    comprobar_huerfanos,
    comprobar_unicidad,
    resumen_fichero,
    validar_carga,
)


# ------------------------------------------------------- unitarios (sin BD)
def test_resumen_fichero(tmp_path: Path) -> None:
    (tmp_path / "f.csv").write_text("Importe,x\n1.10,a\n2.20,b\n,c\nabc,d\n", encoding="utf-8")
    n, sumas, avisos = resumen_fichero(abrir(tmp_path, "f.csv"), ["importe"])
    assert n == 4
    assert sumas == {"importe": Decimal("3.30")}  # Decimal: sin errores de coma flotante
    assert avisos and "abc" in avisos[0]


def test_comprobacion_serializable() -> None:
    datos = Comprobacion("x", True, Decimal("1.5"), Decimal("1.5")).como_dict()
    assert datos["esperado"] == "1.5"


# ------------------------------------------------------- integración (con BD)
def _preparar(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE proyecto_test.clientes (id int PRIMARY KEY);
        INSERT INTO proyecto_test.clientes VALUES (1), (2);
        CREATE TABLE proyecto_test.pedidos (id int, cliente_id int, importe numeric);
        INSERT INTO proyecto_test.pedidos VALUES (1, 1, 10.5), (2, 2, 4.5), (2, 3, 1), (3, NULL, 0);
        """
    )


def test_unicidad_y_huerfanos(cfg: Config, conn_admin: psycopg.Connection) -> None:
    _preparar(conn_admin)
    assert comprobar_unicidad(conn_admin, cfg.esquema, "clientes", ["id"]).ok
    rep = comprobar_unicidad(conn_admin, cfg.esquema, "pedidos", ["id"])
    assert not rep.ok and rep.obtenido.startswith("1 valores repetidos")
    hu = comprobar_huerfanos(conn_admin, cfg.esquema, "pedidos", "cliente_id", "clientes", "id")
    assert not hu.ok and hu.obtenido == 1  # el 3; el NULL no cuenta como huérfano
    assert "['3']" in hu.detalle


def test_validar_carga_frente_al_origen(cfg: Config, conn_admin: psycopg.Connection) -> None:
    _preparar(conn_admin)
    (cfg.dir_datos / "p.csv").write_text(
        "id,cliente_id,importe\n1,1,10.5\n2,2,4.5\n2,3,1\n3,,0\n", encoding="utf-8"
    )
    resultado = validar_carga(
        conn_admin, cfg.esquema, "pedidos", [abrir(cfg.dir_datos, "p.csv")],
        clave=["id"], columnas_suma=["importe"],
    )
    por_nombre = {c.nombre.split(" ")[0]: c for c in resultado}
    assert por_nombre["recuento"].ok and por_nombre["recuento"].obtenido == 4
    assert por_nombre["suma"].ok and por_nombre["suma"].esperado == Decimal("16.0")
    assert not por_nombre["unicidad"].ok  # id 2 repetido en origen y en destino


def test_validar_carga_detecta_diferencias(cfg: Config, conn_admin: psycopg.Connection) -> None:
    _preparar(conn_admin)
    # El origen dice 5 filas y 1600 de importe (céntimos): no cuadra con la tabla.
    (cfg.dir_datos / "p.csv").write_text(
        "id,importe\n1,1050\n2,450\n3,100\n4,0\n5,0\n", encoding="utf-8"
    )
    resultado = validar_carga(
        conn_admin, cfg.esquema, "pedidos", [abrir(cfg.dir_datos, "p.csv")],
        columnas_suma=["importe"],
    )
    assert [c.ok for c in resultado] == [False, False]
