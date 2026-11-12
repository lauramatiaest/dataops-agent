"""Generador de datos: determinismo, coherencia del manifiesto y carga real."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

pytest.importorskip("faker")

from dataops_agent.config import Config
from dataops_agent.servidor import Servicio
from datos import generar_datos


def _huellas(directorio: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directorio.iterdir())}


@pytest.fixture(scope="module")
def generados(tmp_path_factory: pytest.TempPathFactory) -> Path:
    salida = tmp_path_factory.mktemp("datos")
    generar_datos.main(["--semilla", "42", "--salida", str(salida)])
    return salida


def test_misma_semilla_mismos_numeros(generados: Path, tmp_path: Path) -> None:
    generar_datos.main(["--semilla", "42", "--salida", str(tmp_path)])
    assert _huellas(generados / "demo") == _huellas(tmp_path / "demo")
    assert _huellas(generados / "estropeado") == _huellas(tmp_path / "estropeado")


def test_semilla_distinta_datos_distintos(generados: Path, tmp_path: Path) -> None:
    generar_datos.main(["--semilla", "7", "--salida", str(tmp_path)])
    assert _huellas(generados / "demo") != _huellas(tmp_path / "demo")


def test_integridad_de_la_demo(generados: Path) -> None:
    base = generar_datos.generar_base(42)
    skus = {p["sku"] for p in base.productos}
    clientes = {c["cliente_id"] for c in base.clientes}
    pedidos = {p["pedido_id"] for p in base.pedidos}
    assert all(linea["ref_producto"] in skus for linea in base.lineas)
    assert all(p["cliente_id"] in clientes for p in base.pedidos)
    assert all(linea["pedido_id"] in pedidos for linea in base.lineas)


def test_variante_estropeada(generados: Path) -> None:
    verdad = json.loads((generados / "estropeado" / "manifiesto.json").read_text(encoding="utf-8"))
    cabecera = (generados / "estropeado" / "lineas_pedido_2026_10.csv").read_text(
        encoding="utf-8").splitlines()[0]
    assert "precio" in cabecera.split(",") and "precio_unitario" not in cabecera
    assert verdad["recibido"]["filas_lineas"] == verdad["correcto"]["lineas_unicas"] + 45


def test_carga_real_cuadra_con_el_manifiesto(
    generados: Path, cfg: Config, conn_admin: psycopg.Connection
) -> None:
    """Carga los 4 ficheros con el servidor y compara con el manifiesto."""
    s = Servicio(replace(cfg, dir_datos=generados))
    claves = {"clientes": "cliente_id", "pedidos": "pedido_id", "lineas_pedido": "linea_id"}
    for tabla, fichero in [("clientes", "demo/clientes.csv"), ("productos", "demo/productos.json"),
                           ("pedidos", "demo/pedidos.csv"), ("lineas_pedido", "demo/lineas_pedido.csv")]:
        clave = [claves[tabla]] if tabla in claves else ["sku"]
        s.aplicador.aplicar(s.planificador.tabla(tabla, desde_fichero=fichero, clave_primaria=clave).id)
        s.aplicador.aplicar(s.planificador.carga(tabla, fichero).id)

    manifiesto = json.loads((generados / "demo" / "manifiesto.json").read_text(encoding="utf-8"))
    for tabla, filas in manifiesto["filas"].items():
        assert conn_admin.execute(f"SELECT count(*) FROM proyecto_test.{tabla}").fetchone()[0] == filas
    importe = conn_admin.execute(
        """SELECT sum(round(l.cantidad * l.precio_unitario * (100 - l.descuento_pct) / 100, 2))
           FROM proyecto_test.lineas_pedido l JOIN proyecto_test.pedidos p USING (pedido_id)
           WHERE p.estado <> 'cancelado'"""
    ).fetchone()[0]
    assert importe == Decimal(manifiesto["sumas"]["importe_sin_cancelados"])

    # La relación con nombres distintos tiene evidencia completa (cobertura 100 %)...
    plan = s.planificador.relacion("lineas_pedido", "ref_producto", "productos", "sku")
    assert plan.aplicable and plan.impacto["evidencia"]["cobertura_pct"] == 100.0

    # ...y la entrega estropeada se detecta ANTES de cargar.
    plan_roto = s.planificador.carga("lineas_pedido", "estropeado/lineas_pedido_2026_10.csv")
    assert any("'precio'" in b and "precio_unitario" in b for b in plan_roto.bloqueos)
    plan_dup = s.planificador.carga("pedidos", "estropeado/pedidos_2026_10.csv")
    assert any("se repite" in b for b in plan_dup.bloqueos)
