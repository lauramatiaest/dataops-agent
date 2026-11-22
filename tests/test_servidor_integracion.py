"""Servidor MCP de extremo a extremo: cliente MCP en proceso + PostgreSQL real.

Recorre el flujo de la demo con llamadas MCP reales (tools/list, tools/call):
perfilar → planificar → aplicar → validar → relación → vista → pregunta y dashboard,
y comprueba las barreras (SQL rechazado, destructivo sin aprobación, auditoría).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import psycopg
import pytest
from mcp import Client

from dataops_agent.bi import MetabaseAdapter
from dataops_agent.config import Config
from dataops_agent.servidor import crear_servidor
from tests.test_bi_metabase import MetabaseSimulado

CLIENTES = "cliente_id,nombre,ciudad\n1,Ana,Madrid\n2,Luis,Sevilla\n"
PEDIDOS = "pedido_id,cliente_id,importe,estado\n10,1,100.50,entregado\n11,1,20.00,cancelado\n12,2,5.25,entregado\n"


class Sesion:
    """Envoltorio síncrono: cada llamada abre un cliente MCP en proceso."""

    def __init__(self, servidor: Any) -> None:
        self.servidor = servidor

    def _ejecutar(self, corrutina_factory):  # type: ignore[no-untyped-def]
        async def principal():  # type: ignore[no-untyped-def]
            async with Client(self.servidor) as cliente:
                return await corrutina_factory(cliente)

        return asyncio.run(principal())

    def herramientas(self) -> dict[str, Any]:
        resultado = self._ejecutar(lambda c: c.list_tools())
        return {t.name: t for t in resultado.tools}

    def llamar(self, herramienta: str, /, **argumentos: Any) -> dict[str, Any]:
        resultado = self._ejecutar(lambda c: c.call_tool(herramienta, argumentos))
        assert not resultado.is_error, resultado.content
        if resultado.structured_content is not None:
            datos = resultado.structured_content
            return datos.get("result", datos) if set(datos) == {"result"} else datos
        return json.loads(resultado.content[0].text)


@pytest.fixture
def entorno(cfg: Config) -> tuple[Sesion, MetabaseSimulado, Config]:
    cfg = replace(cfg, metabase_url="http://mb.test", metabase_usuario="a@b.c",
                  metabase_password="x")
    simulado = MetabaseSimulado()
    bi = MetabaseAdapter(cfg, httpx.Client(base_url=cfg.metabase_url,
                                           transport=httpx.MockTransport(simulado)))
    (cfg.dir_datos / "clientes.csv").write_text(CLIENTES, encoding="utf-8")
    (cfg.dir_datos / "pedidos.csv").write_text(PEDIDOS, encoding="utf-8")
    return Sesion(crear_servidor(cfg, bi)), simulado, cfg


def test_herramientas_y_anotaciones(entorno) -> None:
    sesion, _, _ = entorno
    herramientas = sesion.herramientas()
    esperadas = {
        "perfilar_fichero", "listar_esquema", "consultar_solo_lectura", "planificar_tabla",
        "planificar_carga", "planificar_relacion", "planificar_vista", "planificar_borrado",
        "aplicar_plan", "aplicar_plan_destructivo", "crear_pregunta", "crear_dashboard",
        "validar_carga", "comprobar_relacion",
    }
    assert esperadas <= set(herramientas)
    assert herramientas["aplicar_plan_destructivo"].annotations.destructive_hint is True
    assert herramientas["consultar_solo_lectura"].annotations.read_only_hint is True


def test_flujo_completo_de_la_demo(entorno, conn_admin: psycopg.Connection) -> None:
    sesion, simulado, cfg = entorno
    perfil = sesion.llamar("perfilar_fichero", ruta="pedidos.csv")
    assert perfil["ok"] and perfil["candidatas_clave"] == ["pedido_id"]

    for tabla, clave in [("clientes", "cliente_id"), ("pedidos", "pedido_id")]:
        plan = sesion.llamar("planificar_tabla", nombre=tabla, desde_fichero=f"{tabla}.csv",
                             clave_primaria=[clave])
        assert plan["aplicable"], plan
        assert sesion.llamar("aplicar_plan", plan_id=plan["plan_id"])["estado"] == "aplicado"
        carga = sesion.llamar("planificar_carga", tabla=tabla, fichero=f"{tabla}.csv")
        sesion.llamar("aplicar_plan", plan_id=carga["plan_id"])

    validacion = sesion.llamar("validar_carga", tabla="pedidos", ficheros=["pedidos.csv"],
                               clave=["pedido_id"], columnas_suma=["importe"],
                               relaciones=[["cliente_id", "clientes", "cliente_id"]])
    assert validacion["valido"], validacion

    relacion = sesion.llamar("planificar_relacion", tabla_hija="pedidos",
                             columna_hija="cliente_id", tabla_padre="clientes",
                             columna_padre="cliente_id")
    sesion.llamar("aplicar_plan", plan_id=relacion["plan_id"])

    vista = sesion.llamar(
        "planificar_vista", nombre="v_ventas_estado",
        consulta="SELECT estado, sum(importe) AS total FROM pedidos GROUP BY estado",
    )
    sesion.llamar("aplicar_plan", plan_id=vista["plan_id"])

    consulta = sesion.llamar("consultar_solo_lectura",
                             sql="SELECT estado, total FROM v_ventas_estado ORDER BY estado")
    assert consulta["filas"] == [["cancelado", 20.0], ["entregado", 105.75]]

    pregunta = sesion.llamar("crear_pregunta", nombre="Ventas por estado",
                             sql="SELECT * FROM v_ventas_estado", visualizacion="barras")
    # El SQL se califica con el esquema para que Metabase lo resuelva:
    assert "proyecto_test.v_ventas_estado" in simulado.cards[pregunta["id"]]["dataset_query"]["native"]["query"]
    dashboard = sesion.llamar("crear_dashboard", nombre="Ventas", pregunta_ids=[pregunta["id"]])
    assert dashboard["url"].endswith("/dashboard/1")

    # Dashboard ordenado con secciones, KPI y ajustes de presentación (bloque B):
    kpi = sesion.llamar("crear_pregunta", nombre="Total", visualizacion="numero",
                        sql="SELECT sum(total) AS total FROM v_ventas_estado",
                        ajustes={"formatos": {"total": "moneda"}})
    panel = sesion.llamar("crear_dashboard", nombre="Panel", elementos=[
        {"seccion": "Resumen"}, {"pregunta_id": kpi["id"]},
        {"seccion": "Detalle"}, {"pregunta_id": pregunta["id"], "tamano": "ancho"},
    ])
    assert panel["ok"] and len(simulado.dashboards[2]["dashcards"]) == 4
    sin_objetivo = sesion.llamar("crear_pregunta", nombre="Meta", visualizacion="progreso",
                                 sql="SELECT total FROM v_ventas_estado")
    assert sin_objetivo["ok"] is False and "objetivo" in sin_objetivo["error"]

    esquema = sesion.llamar("listar_esquema")
    pedidos = next(o for o in esquema["objetos"] if o["nombre"] == "pedidos")
    assert pedidos["filas"] == 3 and pedidos["claves_foraneas"][0]["tabla_referenciada"] == "clientes"

    # Auditoría: cada aplicación queda registrada (JSONL y BD) con su plan_id.
    eventos = [json.loads(linea) for linea in
               (cfg.dir_auditoria / "auditoria.jsonl").read_text(encoding="utf-8").splitlines()]
    aplicaciones = [e for e in eventos if e["herramienta"] == "aplicar_plan"]
    assert len(aplicaciones) == 6
    assert all(e["resultado"] == "ok" and e["plan_id"] and e["sql"] for e in aplicaciones)
    filas = conn_admin.execute(
        "SELECT plan_id FROM dataops_meta.auditoria WHERE id = ANY(%s::uuid[])",
        ([e["id"] for e in aplicaciones],),
    ).fetchall()
    assert sorted(f[0] for f in filas) == sorted(e["plan_id"] for e in aplicaciones)

    # M-19: la descripción guardada en el modelo llega a Metabase al sincronizar,
    # aunque Metabase ya tuviera otra (la primera que importó).
    doc = sesion.llamar("planificar_documentacion", descripciones={
        "clientes": {"descripcion": "Una fila por cliente.",
                     "columnas": {"cliente_id": "Identificador del cliente."}},
    })
    sesion.llamar("aplicar_plan", plan_id=doc["plan_id"])
    sync = sesion.llamar("sincronizar_bi")
    assert sync["descripciones"]["actualizadas"] == ["clientes", "clientes.cliente_id"]
    assert simulado.tablas[0]["description"] == "Una fila por cliente."
    assert sesion.llamar("sincronizar_bi", descripciones=False).get("descripciones") is None


def test_barreras(entorno, conn_admin: psycopg.Connection) -> None:
    sesion, _, cfg = entorno
    rechazo = sesion.llamar("consultar_solo_lectura", sql="DELETE FROM pedidos")
    assert rechazo["ok"] is False and rechazo["rechazado"]
    rechazo = sesion.llamar("consultar_solo_lectura", sql="SELECT * FROM dataops_meta.auditoria")
    assert rechazo["rechazado"]
    rechazo = sesion.llamar("crear_pregunta", nombre="x", sql="SELECT 1; DROP TABLE x")
    assert rechazo["rechazado"]

    plan = sesion.llamar("planificar_tabla", nombre="clientes", desde_fichero="clientes.csv")
    sesion.llamar("aplicar_plan", plan_id=plan["plan_id"])
    carga = sesion.llamar("planificar_carga", tabla="clientes", fichero="clientes.csv")
    sesion.llamar("aplicar_plan", plan_id=carga["plan_id"])

    borrado = sesion.llamar("planificar_borrado", tipo="tabla", objeto="clientes")
    assert borrado["destructivo"] and "aplicar_plan_destructivo" in borrado["siguiente_paso"]
    intento = sesion.llamar("aplicar_plan", plan_id=borrado["plan_id"])
    assert intento["ok"] is False and intento["requiere_aprobacion"]
    assert conn_admin.execute("SELECT count(*) FROM proyecto_test.clientes").fetchone()[0] == 2

    # Con aprobación (en Claude Code la daría la persona) sí se aplica:
    hecho = sesion.llamar("aplicar_plan_destructivo", plan_id=borrado["plan_id"],
                          motivo="La persona pidió borrar la tabla de prueba")
    assert hecho["estado"] == "aplicado"

    eventos = conn_admin.execute(
        "SELECT herramienta, resultado, destructiva FROM dataops_meta.auditoria "
        "WHERE esquema = 'proyecto_test' ORDER BY instante"
    ).fetchall()
    assert ("consultar_solo_lectura", "rechazado", False) in eventos
    assert ("aplicar_plan", "pendiente_confirmacion", True) in eventos
    assert ("aplicar_plan_destructivo", "ok", True) in eventos
    # Cada evento del JSONL de esta sesión está también en la tabla:
    lineas = (cfg.dir_auditoria / "auditoria.jsonl").read_text(encoding="utf-8").splitlines()
    ids = [json.loads(linea)["id"] for linea in lineas]
    en_bd = conn_admin.execute(
        "SELECT count(*) FROM dataops_meta.auditoria WHERE id = ANY(%s::uuid[])", (ids,)
    ).fetchone()[0]
    assert en_bd == len(ids) == 10


def test_limite_de_filas(entorno) -> None:
    sesion, _, _ = entorno
    resultado = sesion.llamar("consultar_solo_lectura",
                              sql="SELECT g FROM generate_series(1, 500) AS g", max_filas=10)
    assert resultado["n_filas"] == 10 and resultado["truncado"] is True


def test_fichero_fuera_del_directorio(entorno) -> None:
    sesion, _, _ = entorno
    resultado = sesion.llamar("perfilar_fichero", ruta="../../etc/passwd")
    assert resultado["ok"] is False and "fuera del directorio" in resultado["error"]


def test_bi_caido_queda_auditado(cfg: Config) -> None:
    """Si Metabase no responde, la herramienta devuelve error y la llamada se audita."""

    def caido(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexión rechazada")

    cfg = replace(cfg, metabase_url="http://mb.test", metabase_usuario="a@b.c", metabase_password="x")
    bi = MetabaseAdapter(cfg, httpx.Client(base_url=cfg.metabase_url, transport=httpx.MockTransport(caido)))
    sesion = Sesion(crear_servidor(cfg, bi))
    resultado = sesion.llamar("crear_pregunta", nombre="x", sql="SELECT 1")
    assert resultado["ok"] is False and "rechazada" in resultado["error"]
    [evento] = [json.loads(x) for x in (cfg.dir_auditoria / "auditoria.jsonl").read_text().splitlines()]
    assert evento["herramienta"] == "crear_pregunta" and evento["resultado"] == "error"
