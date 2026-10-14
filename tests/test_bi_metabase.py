"""Tests del MetabaseAdapter con un Metabase simulado (httpx.MockTransport).

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

from dataops_agent.bi import ErrorBI, MetabaseAdapter, Pregunta
from dataops_agent.config import Config

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
        if ruta == "/api/card" and metodo == "POST":
            if extra := set(cuerpo) - CLAVES_CARD:
                return httpx.Response(400, json={"errors": {k: "clave no permitida" for k in extra}})
            card_id = 100 + len(self.cards)
            self.cards[card_id] = cuerpo
            return httpx.Response(200, json={"id": card_id, **cuerpo})
        if ruta.startswith("/api/card/") and metodo == "PUT":
            card_id = int(ruta.rsplit("/", 1)[1])
            self.cards[card_id].update(cuerpo)
            return httpx.Response(200, json={"id": card_id, **self.cards[card_id]})
        if ruta == "/api/dashboard" and metodo == "POST":
            dash_id = 1 + len(self.dashboards)
            self.dashboards[dash_id] = {"id": dash_id, "dashcards": [], **cuerpo}
            return httpx.Response(200, json=self.dashboards[dash_id])
        if ruta.startswith("/api/dashboard/"):
            dash_id = int(ruta.rsplit("/", 1)[1])
            if metodo == "GET":
                return httpx.Response(200, json=self.dashboards[dash_id])
            for t in cuerpo["dashcards"]:
                if extra := set(t) - CLAVES_DASHCARD:
                    return httpx.Response(400, json={"errors": list(extra)})
                assert t["size_x"] >= 1 and t["size_y"] >= 1 and t["row"] >= 0 and t["col"] >= 0
            # Reemplaza el conjunto completo; asigna ids positivos a las nuevas.
            siguiente = 1000 + sum(len(d["dashcards"]) for d in self.dashboards.values())
            tarjetas = []
            for i, t in enumerate(cuerpo["dashcards"]):
                tarjetas.append({**t, "id": t["id"] if t["id"] > 0 else siguiente + i})
            self.dashboards[dash_id]["dashcards"] = tarjetas
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


def test_dashboard_coloca_tarjetas_y_conserva_las_existentes(
    cfg_mb: Config, simulado: MetabaseSimulado
) -> None:
    mb = _adaptador(cfg_mb, simulado)
    ids = [mb.crear_pregunta(Pregunta(f"p{i}", "SELECT 1")).id for i in range(3)]
    ref = mb.crear_dashboard("Ventas", ids[:2], "Resumen")
    assert ref.url == "http://mb.test/dashboard/1"
    tarjetas = simulado.dashboards[1]["dashcards"]
    assert [(t["row"], t["col"]) for t in tarjetas] == [(0, 0), (0, 12)]
    mb.anadir_a_dashboard(ref.id, [ids[2]])
    tarjetas = simulado.dashboards[1]["dashcards"]
    assert [t["card_id"] for t in tarjetas] == ids  # las dos anteriores siguen
    assert tarjetas[2]["row"] == 6


def test_actualizar_pregunta(cfg_mb: Config, simulado: MetabaseSimulado) -> None:
    mb = _adaptador(cfg_mb, simulado)
    ref = mb.crear_pregunta(Pregunta("p", "SELECT 1"))
    mb.actualizar_pregunta(ref.id, sql="SELECT 2", visualizacion="barras")
    assert simulado.cards[ref.id]["dataset_query"]["native"]["query"] == "SELECT 2"
    assert simulado.cards[ref.id]["display"] == "bar"
    with pytest.raises(ValueError):
        mb.actualizar_pregunta(ref.id)


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
