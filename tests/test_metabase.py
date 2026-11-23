"""Tests de la integración con Metabase, con un Metabase simulado (httpx.MockTransport).

El simulador reproduce lo verificado en la API de Metabase v0.64.2.8, incluido que
los esquemas de entrada son "cerrados" (rechazan claves desconocidas) y que
``PUT /api/dashboard/:id`` reemplaza todas las tarjetas.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from dataops_agent.config import Config
from dataops_agent.metabase import (
    Ajustes,
    Elemento,
    ErrorBI,
    MetabaseAdapter,
    Pregunta,
    colocar,
    elementos_dashboard,
)

CLAVES_CARD = {"name", "display", "visualization_settings", "dataset_query", "description",
               "collection_id", "type", "parameters", "dashboard_id", "cache_ttl"}
CLAVES_DASHCARD = {"id", "card_id", "row", "col", "size_x", "size_y", "dashboard_tab_id",
                   "parameter_mappings", "visualization_settings", "series", "action_id",
                   "inline_parameters"}


class MetabaseSimulado:
    def __init__(self) -> None:
        self.peticiones: list[httpx.Request] = []
        self.sesiones_emitidas = 0
        self.sesion_valida = "sesion-1"
        self.setup_token: str | None = "token-setup"
        self.bases: list[dict[str, Any]] = [{"id": 2, "name": "DataOps (solo lectura)"}]
        self.cards: dict[int, dict[str, Any]] = {}
        self.dashboards: dict[int, dict[str, Any]] = {}

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        self.peticiones.append(peticion)
        ruta, metodo = peticion.url.path, peticion.method
        cuerpo = json.loads(peticion.content) if peticion.content else {}

        if ruta == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if ruta == "/api/session/properties":
            return httpx.Response(200, json={"setup-token": self.setup_token})
        if ruta == "/api/setup":
            assert set(cuerpo) == {"token", "user", "prefs"}
            assert cuerpo["token"] == self.setup_token
            self.setup_token = None
            return httpx.Response(200, json={"id": self.sesion_valida})
        if ruta == "/api/session" and metodo == "POST":
            assert set(cuerpo) == {"username", "password"}
            self.sesiones_emitidas += 1
            return httpx.Response(200, json={"id": self.sesion_valida})

        if peticion.headers.get("X-Metabase-Session") != self.sesion_valida \
                and peticion.headers.get("X-API-Key") != "clave-api":
            return httpx.Response(401, text="Unauthenticated")

        if ruta == "/api/database" and metodo == "GET":
            return httpx.Response(200, json={"data": self.bases, "total": len(self.bases)})
        if ruta == "/api/database" and metodo == "POST":
            assert {"name", "engine", "details"} <= set(cuerpo)
            nueva = {"id": 10 + len(self.bases), "name": cuerpo["name"]}
            self.bases.append(nueva)
            return httpx.Response(200, json=nueva)
        if ruta.endswith("/sync_schema"):
            return httpx.Response(200, json={"status": "ok"})
        if ruta == "/api/dashboard" and metodo == "GET":
            return httpx.Response(200, json=[
                {"id": d["id"], "name": d.get("name")} for d in self.dashboards.values()
            ])
        if ruta == "/api/card" and metodo == "POST":
            if extra := set(cuerpo) - CLAVES_CARD:
                return httpx.Response(400, json={"errors": {k: "clave no permitida" for k in extra}})
            card_id = 100 + len(self.cards)
            self.cards[card_id] = cuerpo
            return httpx.Response(200, json={"id": card_id, **cuerpo})
        if ruta == "/api/dashboard" and metodo == "POST":
            dash_id = 1 + len(self.dashboards)
            self.dashboards[dash_id] = {"id": dash_id, "dashcards": [], **cuerpo}
            return httpx.Response(200, json=self.dashboards[dash_id])
        if ruta.startswith("/api/dashboard/"):
            dash_id = int(ruta.rsplit("/", 1)[1])
            assert cuerpo.get("width", "full") in ("fixed", "full")
            for t in cuerpo["dashcards"]:
                if extra := set(t) - CLAVES_DASHCARD:
                    return httpx.Response(400, json={"errors": list(extra)})
                assert t["size_x"] >= 1 and t["size_y"] >= 1 and t["row"] >= 0 and t["col"] >= 0
                assert t["col"] + t["size_x"] <= 24
                if t["card_id"] is None:  # tarjeta virtual: título o texto
                    virtual = t["visualization_settings"]["virtual_card"]
                    assert virtual["display"] in ("heading", "text") and t["visualization_settings"]["text"]
            # Fija el conjunto completo de tarjetas; asigna ids positivos a las nuevas.
            siguiente = 1000 + sum(len(d["dashcards"]) for d in self.dashboards.values())
            tarjetas = []
            for i, t in enumerate(cuerpo["dashcards"]):
                tarjetas.append({**t, "id": t["id"] if t["id"] > 0 else siguiente + i})
            self.dashboards[dash_id]["dashcards"] = tarjetas
            if "width" in cuerpo:
                self.dashboards[dash_id]["width"] = cuerpo["width"]
            return httpx.Response(200, json=self.dashboards[dash_id])
        return httpx.Response(404, text=f"no simulado: {metodo} {ruta}")


@pytest.fixture
def simulado() -> MetabaseSimulado:
    return MetabaseSimulado()


@pytest.fixture
def cfg_mb() -> Config:
    return Config(metabase_url="http://mb.test", metabase_usuario="a@b.c",
                  metabase_password="x", lector_password="l")


def _adaptador(cfg: Config, simulado: MetabaseSimulado) -> MetabaseAdapter:
    cliente = httpx.Client(base_url=cfg.metabase_url, transport=httpx.MockTransport(simulado))
    return MetabaseAdapter(cfg, cliente)


def test_crear_pregunta_nativa(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    mb = _adaptador(cfg_mb, simulado)
    ref = mb.crear_pregunta(Pregunta("Ventas por mes", "SELECT 1", "lineas", "desc"))
    assert ref.url == "http://mb.test/question/100"
    card = simulado.cards[100]
    assert card["display"] == "line"
    assert card["dataset_query"] == {"type": "native", "native": {"query": "SELECT 1"}, "database": 2}
    assert card["visualization_settings"] == {}
    # Autenticación por sesión con la cabecera correcta:
    assert simulado.peticiones[-1].headers["X-Metabase-Session"] == "sesion-1"


def test_api_key(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    mb = _adaptador(replace(cfg_mb, metabase_api_key="clave-api"), simulado)
    mb.crear_pregunta(Pregunta("x", "SELECT 1"))
    assert simulado.sesiones_emitidas == 0
    assert simulado.peticiones[-1].headers["X-API-Key"] == "clave-api"


def test_reintenta_si_la_sesion_caduca(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    mb = _adaptador(cfg_mb, simulado)
    mb.crear_pregunta(Pregunta("a", "SELECT 1"))
    simulado.sesion_valida = "sesion-2"  # Metabase invalida la sesión anterior
    mb.crear_pregunta(Pregunta("b", "SELECT 2"))
    assert simulado.sesiones_emitidas == 2


def test_dashboard_coloca_tarjetas(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    mb = _adaptador(cfg_mb, simulado)
    ids = [mb.crear_pregunta(Pregunta(f"p{i}", "SELECT 1", "barras")).id for i in range(3)]
    ref = mb.crear_dashboard("Ventas", elementos_dashboard(ids, None), "Resumen")
    assert ref.url == "http://mb.test/dashboard/1"
    tarjetas = simulado.dashboards[1]["dashcards"]
    assert [t["card_id"] for t in tarjetas] == ids
    assert [(t["row"], t["col"]) for t in tarjetas] == [(0, 0), (0, 12), (6, 0)]


def test_base_de_datos_no_registrada(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    simulado.bases = []
    mb = _adaptador(cfg_mb, simulado)
    with pytest.raises(ErrorBI, match="configurar_metabase"):
        mb.crear_pregunta(Pregunta("p", "SELECT 1"))
    assert mb.registrar_base_datos("postgres", 5432) == 10
    assert mb.registrar_base_datos("postgres", 5432) == 10  # idempotente


def test_setup_inicial(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    mb = _adaptador(cfg_mb, simulado)
    assert mb.configurar_admin("a@b.c", "Clave-Larga-2026") is True
    assert mb.configurar_admin("a@b.c", "Clave-Larga-2026") is False


def test_estado_y_errores_http(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    mb = _adaptador(cfg_mb, simulado)
    assert mb.estado() == {"disponible": True, "url": "http://mb.test", "base_datos_id": 2}

    def caido(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("rechazada")

    mb_caido = MetabaseAdapter(cfg_mb, httpx.Client(base_url="http://mb.test",
                                                    transport=httpx.MockTransport(caido)))
    assert mb_caido.estado()["disponible"] is False


def test_visualizacion_no_valida() -> None:
    with pytest.raises(ValueError, match="Visualización"):
        Pregunta("p", "SELECT 1", "radar")


# ------------------------------------------------------------ bloque B: dashboards completos
def test_ajustes_se_traducen_a_metabase(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    mb = _adaptador(cfg_mb, simulado)
    ajustes = Ajustes.desde_dict({
        "eje_x": "mes", "series": ["importe", "pedidos"], "titulo_y": "Euros",
        "apilado": True, "mostrar_valores": True, "formatos": {"importe": "moneda"},
    })
    mb.crear_pregunta(Pregunta("Ventas", "SELECT 1", "barras", ajustes=ajustes))
    card = simulado.cards[100]
    assert card["display"] == "bar"
    vs = card["visualization_settings"]
    assert vs["graph.dimensions"] == ["mes"] and vs["graph.metrics"] == ["importe", "pedidos"]
    assert vs["graph.y_axis.title_text"] == "Euros"
    assert vs["stackable.stack_type"] == "stacked" and vs["graph.show_values"] is True
    # La clave de column_settings es el JSON compacto que usa Metabase:
    assert vs["column_settings"] == {
        '["name","importe"]': {"number_style": "currency", "currency": "EUR",
                               "currency_style": "symbol"}
    }


@pytest.mark.parametrize(("visualizacion", "esperado"), [
    ("tarta", {"pie.dimension": "categoria", "pie.metric": "importe"}),
    ("embudo", {"funnel.dimension": "categoria", "funnel.metric": "importe"}),
    ("numero", {"scalar.field": "importe"}),
])
def test_ajustes_segun_visualizacion(
    cfg_mb: Config, simulado: MetabaseSimulado, visualizacion: str, esperado: dict[str, Any]
) -> None:
    mb = _adaptador(cfg_mb, simulado)
    ajustes = Ajustes.desde_dict({"eje_x": "categoria", "series": ["importe"]})
    mb.crear_pregunta(Pregunta("p", "SELECT 1", visualizacion, ajustes=ajustes))
    assert simulado.cards[100]["visualization_settings"] == esperado


def test_combinado_con_doble_eje_no_repite_el_titulo(
    cfg_mb: Config, simulado: MetabaseSimulado
) -> None:
    """Metabase pondría el mismo título en los dos ejes (visto en Metabase real)."""
    mb = _adaptador(cfg_mb, simulado)
    dos = Ajustes.desde_dict({"eje_x": "mes", "series": ["importe", "pedidos"],
                              "titulo_y": "Importe (€)"})
    una = Ajustes.desde_dict({"eje_x": "mes", "series": ["importe"], "titulo_y": "Importe (€)"})
    mb.crear_pregunta(Pregunta("a", "SELECT 1", "combinado", ajustes=dos))
    mb.crear_pregunta(Pregunta("b", "SELECT 1", "combinado", ajustes=una))
    assert "graph.y_axis.title_text" not in simulado.cards[100]["visualization_settings"]
    assert simulado.cards[101]["visualization_settings"]["graph.y_axis.title_text"] == "Importe (€)"


def test_desglose_por_segunda_dimension(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    """Importe por mes desglosado por canal: dos dimensiones, una serie por canal."""
    mb = _adaptador(cfg_mb, simulado)
    ajustes = Ajustes.desde_dict({"eje_x": "mes", "desglose": "canal", "series": ["importe"],
                                  "apilado": True})
    mb.crear_pregunta(Pregunta("Canal", "SELECT 1", "barras", ajustes=ajustes))
    vs = simulado.cards[100]["visualization_settings"]
    assert vs["graph.dimensions"] == ["mes", "canal"] and vs["graph.metrics"] == ["importe"]
    assert vs["stackable.stack_type"] == "stacked"
    with pytest.raises(ValueError, match="eje_x"):
        Ajustes.desde_dict({"desglose": "canal"})


def test_texto_largo_ocupa_mas_filas() -> None:
    largo = colocar([Elemento(texto="x" * 400)])
    assert largo[0]["size_y"] == 3


def test_progreso_necesita_objetivo(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    with pytest.raises(ValueError, match="objetivo"):
        Pregunta("p", "SELECT 1", "progreso")
    mb = _adaptador(cfg_mb, simulado)
    mb.crear_pregunta(Pregunta("p", "SELECT 1", "progreso",
                               ajustes=Ajustes.desde_dict({"objetivo": 50000})))
    assert simulado.cards[100]["display"] == "progress"
    assert simulado.cards[100]["visualization_settings"] == {"progress.goal": 50000.0}


@pytest.mark.parametrize("datos", [
    {"color": "rojo"},                       # clave desconocida
    {"formatos": {"importe": "dolares"}},    # formato no admitido
    {"series": [1, 2]},                      # series que no son columnas
    {"objetivo": "mucho"},
    {"apilado": "si"},
])
def test_ajustes_no_validos(datos: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Ajustes.desde_dict(datos)


def test_colocar_por_filas_con_secciones() -> None:
    elementos = [Elemento(pregunta_id=i, tamano="kpi") for i in (1, 2, 3, 4, 5)] + [
        Elemento(seccion="Evolución"),
        Elemento(pregunta_id=6, tamano="medio"),
        Elemento(pregunta_id=7, tamano="medio"),
        Elemento(pregunta_id=8, tamano="ancho"),
        Elemento(texto="Nota: excluye pedidos cancelados"),
        Elemento(pregunta_id=9, tamano="tercio"),
    ]
    t = colocar(elementos)
    pos = [(x["card_id"], x["row"], x["col"], x["size_x"], x["size_y"]) for x in t]
    assert pos == [
        (1, 0, 0, 6, 3), (2, 0, 6, 6, 3), (3, 0, 12, 6, 3), (4, 0, 18, 6, 3),
        (5, 3, 0, 6, 3),                       # el quinto KPI baja de fila
        (None, 6, 0, 24, 1),                   # la sección cierra la fila
        (6, 7, 0, 12, 6), (7, 7, 12, 12, 6),
        (8, 13, 0, 24, 7),
        (None, 20, 0, 24, 1),                  # texto corto: una fila
        (9, 21, 0, 8, 6),
    ]
    assert t[5]["visualization_settings"]["virtual_card"]["display"] == "heading"
    assert t[5]["visualization_settings"]["text"] == "Evolución"
    assert len({x["id"] for x in t}) == len(t) and all(x["id"] < 0 for x in t)


def test_dashboard_con_secciones_y_ancho_completo(
    cfg_mb: Config, simulado: MetabaseSimulado
) -> None:
    mb = _adaptador(cfg_mb, simulado)
    kpi = mb.crear_pregunta(Pregunta("Ventas", "SELECT 1", "numero")).id
    linea = mb.crear_pregunta(Pregunta("Evolución", "SELECT 1", "lineas")).id
    mb.crear_dashboard("Panel", [Elemento(texto="Ventas sin cancelados"),
                                 Elemento(pregunta_id=kpi, tamano="kpi"),
                                 Elemento(seccion="Evolución"), Elemento(pregunta_id=linea)])
    dash = simulado.dashboards[1]
    assert dash["width"] == "full"
    assert [(t["card_id"], t["row"], t["size_x"], t["size_y"]) for t in dash["dashcards"]] == [
        (None, 0, 24, 1), (kpi, 1, 6, 3), (None, 4, 24, 1), (linea, 5, 12, 6)
    ]


@pytest.mark.parametrize("datos", [
    {"pregunta_id": 1, "seccion": "x"},     # dos cosas a la vez
    {},                                      # ninguna
    {"seccion": "x", "tamano": "kpi"},       # tamaño en algo que no es pregunta
    {"pregunta_id": 1, "tamano": "gigante"},
    {"pregunta_id": "1"},
    {"grafico": 1},
])
def test_elementos_no_validos(datos: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Elemento.desde_dict(datos)


def test_ids_o_elementos_pero_no_ambos() -> None:
    with pytest.raises(ValueError, match="no los dos"):
        elementos_dashboard([1], [{"pregunta_id": 2}])
    assert elementos_dashboard(None, None) == []


