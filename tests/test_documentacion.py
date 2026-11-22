"""Documentación del modelo: linaje desde la auditoría y diccionario (sin base de datos)."""

from __future__ import annotations

import pytest

from dataops_agent.documentacion import (
    diccionario_markdown,
    linaje_cargas,
    normalizar_texto,
    sql_legible,
)

EVENTOS = [
    {"operacion": "PLAN CARGA", "resultado": "ok", "plan_id": "plan_a",
     "sql": "COPY proyecto_demo.pedidos (pedido_id, estado) FROM STDIN  -- datos de pedidos.csv;"},
    {"operacion": "APLICAR", "resultado": "ok", "plan_id": "plan_a", "instante": "2026-11-13T10:00"},
    # Planificado pero nunca aplicado: no cuenta.
    {"operacion": "PLAN CARGA", "resultado": "ok", "plan_id": "plan_b",
     "sql": "COPY proyecto_demo.pedidos (pedido_id) FROM STDIN  -- datos de otro.csv;"},
    # Transformada y aplicada.
    {"operacion": "PLAN CARGA TRANSFORMADA", "resultado": "ok", "plan_id": "plan_c",
     "sql": "COPY proyecto_demo.lineas (linea_id) FROM STDIN  -- datos de lineas_rotas.csv;",
     "detalle": {"renombrar": {"precio": "precio_unitario"}, "dividir": {"precio": 100},
                 "quitar_duplicados_exactos": True}},
    {"operacion": "APLICAR", "resultado": "ok", "plan_id": "plan_c", "instante": "2026-11-13T11:00"},
    # Aplicación fallida: no cuenta.
    {"operacion": "PLAN CARGA", "resultado": "ok", "plan_id": "plan_d",
     "sql": "COPY proyecto_demo.lineas (linea_id) FROM STDIN  -- datos de x.csv;"},
    {"operacion": "APLICAR", "resultado": "error", "plan_id": "plan_d"},
]


def test_linaje_solo_cargas_aplicadas() -> None:
    linaje = linaje_cargas(EVENTOS, "proyecto_demo")
    assert [c["fichero"] for c in linaje["pedidos"]] == ["pedidos.csv"]
    assert linaje["pedidos"][0]["aplicado"] == "2026-11-13T10:00"
    (lineas,) = linaje["lineas"]
    assert lineas["fichero"] == "lineas_rotas.csv"
    assert lineas["transformacion"]["dividir"] == {"precio": 100}
    assert linaje_cargas(EVENTOS, "otro_esquema") == {}


def test_linaje_ignora_cargas_anteriores_a_recrear_la_tabla() -> None:
    """La auditoría acumula sesiones: tras recrear la tabla, lo anterior no es su linaje."""
    eventos = [
        {"operacion": "PLAN TABLA", "plan_id": "t1",
         "sql": "CREATE TABLE proyecto_demo.lineas (\n    linea_id integer);"},
        {"operacion": "APLICAR", "resultado": "ok", "plan_id": "t1",
         "instante": "2026-11-13T09:00:00+00:00"},
        {"operacion": "PLAN CARGA TRANSFORMADA", "plan_id": "c1",
         "sql": "COPY proyecto_demo.lineas (linea_id) FROM STDIN  -- datos de rota.csv;"},
        {"operacion": "APLICAR", "resultado": "ok", "plan_id": "c1",
         "instante": "2026-11-13T10:00:00+00:00"},
        # Proyecto reiniciado y tabla creada de nuevo:
        {"operacion": "PLAN TABLA", "plan_id": "t2",
         "sql": "CREATE TABLE proyecto_demo.lineas (\n    linea_id integer);"},
        {"operacion": "APLICAR", "resultado": "ok", "plan_id": "t2",
         "instante": "2026-11-22T16:00:00+00:00"},
        {"operacion": "PLAN CARGA", "plan_id": "c2",
         "sql": "COPY proyecto_demo.lineas (linea_id) FROM STDIN  -- datos de demo.csv;"},
        {"operacion": "APLICAR", "resultado": "ok", "plan_id": "c2",
         "instante": "2026-11-22T16:05:00+00:00"},
        # Una tabla de otro esquema con el mismo nombre no cuenta:
        {"operacion": "PLAN TABLA", "plan_id": "t3",
         "sql": "CREATE TABLE otro.lineas (\n    linea_id integer);"},
        {"operacion": "APLICAR", "resultado": "ok", "plan_id": "t3",
         "instante": "2026-11-23T00:00:00+00:00"},
    ]
    assert [c["fichero"] for c in linaje_cargas(eventos, "proyecto_demo")["lineas"]] == [
        "demo.csv"
    ]


def test_textos_y_sql_legible() -> None:
    assert normalizar_texto("  Una  fila\npor pedido ", "x") == "Una fila por pedido"
    with pytest.raises(ValueError, match="vacía"):
        normalizar_texto("   ", "pedidos")
    with pytest.raises(ValueError, match="máximo"):
        normalizar_texto("a" * 501, "pedidos")
    assert sql_legible("p", "t", None, "Dato d'ejemplo", "tabla") == (
        "COMMENT ON TABLE p.t IS 'Dato d''ejemplo'"
    )
    assert sql_legible("p", "v", "c", "x", "vista").startswith("COMMENT ON COLUMN p.v.c")


def test_diccionario_markdown() -> None:
    modelo = {
        "esquema": "proyecto_demo",
        "pendiente_de_documentar": ["pedidos.estado"],
        "objetos": [
            {"nombre": "clientes", "tipo": "tabla", "filas": 500, "descripcion": "Un cliente",
             "clave_primaria": ["cliente_id"], "claves_foraneas": [], "cargas": [],
             "leido_por": [], "columnas": [
                 {"nombre": "cliente_id", "tipo": "integer", "nulable": False,
                  "descripcion": "Identificador"}]},
            {"nombre": "pedidos", "tipo": "tabla", "filas": 3400, "descripcion": None,
             "clave_primaria": ["pedido_id"],
             "claves_foraneas": [{"nombre": "fk", "columnas": ["cliente_id"],
                                  "tabla_referenciada": "clientes",
                                  "columnas_referenciadas": ["cliente_id"]}],
             "cargas": [{"fichero": "pedidos.csv", "plan_id": "plan_a"}],
             "leido_por": ["v_ventas"],
             "uso_bi": {"preguntas": [{"nombre": "Ventas"}], "dashboards": []},
             "columnas": [
                 {"nombre": "pedido_id", "tipo": "integer", "nulable": False, "descripcion": None},
                 {"nombre": "cliente_id", "tipo": "integer", "nulable": False, "descripcion": None},
                 {"nombre": "importe", "tipo": "numeric(12,2)", "nulable": True,
                  "descripcion": "Total | con IVA"}]},
            {"nombre": "v_ventas", "tipo": "vista", "filas": 21, "descripcion": "Ventas por mes",
             "lee_de": ["pedidos"], "definicion": "SELECT 1", "leido_por": [],
             "columnas": [{"nombre": "mes", "tipo": "date", "nulable": True, "descripcion": None}]},
        ],
    }
    md = diccionario_markdown(modelo)
    assert "erDiagram" in md
    assert 'clientes ||--o{ pedidos : "cliente_id"' in md
    assert "integer cliente_id FK" in md and "numeric_12_2_ importe" in md
    assert "**Filas:** 3.400" in md
    assert "`pedidos.csv`" in md and "La usan las vistas:** v_ventas" in md
    assert "Total \\| con IVA" in md  # la barra no rompe la tabla de Markdown
    assert "### `v_ventas`" in md and "**Lee de:** pedidos" in md
    assert "## Pendiente de documentar" in md
