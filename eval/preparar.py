"""Preparación del estado inicial de la base de datos para cada tarea de evaluación.

Estados (campo ``preparacion`` de la tarea):

* ``vacio``: esquema del proyecto vacío.
* ``cargado``: las 4 tablas de la demo cargadas, SIN claves primarias ni foráneas.
* ``modelado``: cargado + claves primarias + relaciones pedidos→clientes y
  lineas_pedido→pedidos. La relación lineas_pedido.ref_producto→productos.sku NO se
  crea (los nombres no coinciden: es la que el agente debe preguntar).

Además, ``sql_admin`` (lista; admite {esquema}, {lector} y {escritor}) se ejecuta como
administrador tras preparar el estado, para escenarios concretos (huérfanos, otro
proyecto con datos ajenos, etc.).

La preparación usa los mismos planificadores del producto con el rol escritor; no
forma parte de lo que se evalúa.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import psycopg

from dataops_agent import admin
from dataops_agent.config import Config
from dataops_agent.planificador import ColumnaDef
from dataops_agent.servidor import Servicio

TABLAS = [
    ("clientes", "demo/clientes.csv", ["cliente_id"]),
    ("productos", "demo/productos.json", ["sku"]),
    ("pedidos", "demo/pedidos.csv", ["pedido_id"]),
    ("lineas_pedido", "demo/lineas_pedido.csv", ["linea_id"]),
]
RELACIONES = [
    ("pedidos", "cliente_id", "clientes", "cliente_id"),
    ("lineas_pedido", "pedido_id", "pedidos", "pedido_id"),
]
ESTADOS = ("vacio", "cargado", "modelado")
COLUMNAS_PRODUCTOS = [
    ColumnaDef(nombre="sku", tipo="text", nulable=False),
    ColumnaDef(nombre="nombre", tipo="text"),
    ColumnaDef(nombre="categoria", tipo="text"),
    ColumnaDef(nombre="precio_unitario", tipo="numeric(10,2)"),
    ColumnaDef(nombre="activo", tipo="boolean"),
]


def preparar(estado: str, cfg: Config, dsn_admin: str, dir_datos: Path,
             sql_admin: list[str] | None = None) -> None:
    if estado not in ESTADOS:
        raise ValueError(f"Estado de preparación desconocido: {estado}")
    cfg = replace(cfg, dir_datos=dir_datos, actor="evaluacion-preparacion",
                  dir_auditoria=dir_datos.parent / "eval" / "resultados")
    admin.inicializar(dsn_admin, cfg)
    admin.reiniciar_proyecto(dsn_admin, cfg)
    with psycopg.connect(dsn_admin, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS otro_proyecto CASCADE")

    if estado != "vacio":
        s = Servicio(cfg)
        for tabla, fichero, clave in TABLAS:
            pk = clave if estado == "modelado" else None
            if tabla == "productos":
                # Precio como numeric(10,2), igual que lo definiría un analista.
                plan = s.planificador.tabla(tabla, columnas=COLUMNAS_PRODUCTOS, clave_primaria=pk)
            else:
                plan = s.planificador.tabla(tabla, desde_fichero=fichero, clave_primaria=pk)
            _aplicar(s, plan.id)
            _aplicar(s, s.planificador.carga(tabla, fichero).id)
        if estado == "modelado":
            for relacion in RELACIONES:
                _aplicar(s, s.planificador.relacion(*relacion).id)

    if sql_admin:
        with psycopg.connect(dsn_admin, autocommit=True) as conn:
            for sentencia in sql_admin:
                sentencia = (sentencia.replace("{esquema}", cfg.esquema)
                             .replace("{lector}", cfg.lector_usuario)
                             .replace("{escritor}", cfg.escritor_usuario))
                conn.execute(sentencia)  # type: ignore[arg-type]  # SQL de la propia tarea


def _aplicar(s: Servicio, plan_id: str) -> None:
    plan = s.almacen.obtener_para_aplicar(plan_id)  # lanza si hay bloqueos
    s.aplicador.aplicar(plan.id)
