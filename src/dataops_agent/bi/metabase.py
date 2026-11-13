"""Adaptador para Metabase OSS mediante su API REST.

Versión de referencia: Metabase v0.64.2.8 (la imagen fijada en docker-compose.yml).
Endpoints comprobados en el código fuente y en la especificación OpenAPI de esa
versión (``docs/api.json`` del repositorio de Metabase); ver docs/arquitectura.md:

* ``GET  /api/health``                       → 200 {"status": "ok"} (503 al arrancar)
* ``GET  /api/session/properties``           → incluye "setup-token" (null tras el setup)
* ``POST /api/setup``                        → {token, user{email,password,...}, prefs{site_name}}
* ``POST /api/session``                      → {username, password} → {"id": sesión}
* Cabeceras: ``X-Metabase-Session`` (sesión) o ``X-API-Key`` (clave de API)
* ``GET  /api/database``                     → {"data": [...], "total": n}
* ``POST /api/database``                     → {name, engine, details{host, port, dbname,
  user, password}}
* ``POST /api/database/:id/sync_schema``     → asíncrono, devuelve {"status": "ok"}
* ``POST /api/card``                         → {name, display, visualization_settings,
  dataset_query}
* ``PUT  /api/card/:id``                     → campos opcionales (p. ej. dataset_query)
* ``POST /api/dashboard``                    → {name, description?}
* ``PUT  /api/dashboard/:id``                → {"dashcards": [...]} REEMPLAZA todas las tarjetas
  (desde 0.48; ``POST /api/dashboard/:id/cards`` se eliminó en 0.47)

Las consultas se envían en el formato "nativo" clásico
``{"type": "native", "native": {"query": ...}, "database": id}``, que Metabase sigue
aceptando y normaliza internamente a MBQL 5.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from dataops_agent.bi.base import BIAdapter, ErrorBI, Pregunta, RefBI
from dataops_agent.config import Config

# Traducción de visualizaciones neutrales a los valores "display" de Metabase.
_DISPLAY = {
    "tabla": "table",
    "barras": "bar",
    "barras_horizontales": "row",
    "lineas": "line",
    "area": "area",
    "tarta": "pie",
    "numero": "scalar",
}
_ANCHO_REJILLA = 24  # columnas de la rejilla de un dashboard de Metabase


class MetabaseAdapter(BIAdapter):
    nombre = "metabase"

    def __init__(self, cfg: Config, cliente: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.base = cfg.metabase_url.rstrip("/")
        self.publica = (cfg.metabase_url_publica or cfg.metabase_url).rstrip("/")
        self._http = cliente or httpx.Client(base_url=self.base, timeout=30.0)
        self._cabeceras: dict[str, str] = {}
        self._bd_id: int | None = None

    # ------------------------------------------------------------ HTTP básico
    def _autenticar(self) -> None:
        if self.cfg.metabase_api_key:
            self._cabeceras = {"X-API-Key": self.cfg.metabase_api_key}
            return
        if not (self.cfg.metabase_usuario and self.cfg.metabase_password):
            raise ErrorBI("Faltan METABASE_USUARIO/METABASE_PASSWORD o METABASE_API_KEY.")
        respuesta = self._http.post(
            "/api/session",
            json={"username": self.cfg.metabase_usuario, "password": self.cfg.metabase_password},
        )
        datos = self._json(respuesta, "iniciar sesión")
        if "id" not in datos:
            raise ErrorBI("Metabase no devolvió una sesión (¿MFA activado?).")
        self._cabeceras = {"X-Metabase-Session": str(datos["id"])}

    def _peticion(self, metodo: str, ruta: str, **kwargs: Any) -> Any:
        if not self._cabeceras:
            self._autenticar()
        respuesta = self._http.request(metodo, ruta, headers=self._cabeceras, **kwargs)
        if respuesta.status_code == 401 and not self.cfg.metabase_api_key:
            self._autenticar()  # la sesión pudo caducar: un único reintento
            respuesta = self._http.request(metodo, ruta, headers=self._cabeceras, **kwargs)
        return self._json(respuesta, f"{metodo} {ruta}")

    @staticmethod
    def _json(respuesta: httpx.Response, que: str) -> Any:
        if respuesta.status_code >= 400:
            raise ErrorBI(f"Metabase respondió {respuesta.status_code} al {que}: "
                          f"{respuesta.text[:300]}")
        if not respuesta.content:
            return {}
        try:
            return respuesta.json()
        except ValueError:
            return {"texto": respuesta.text}

    # ----------------------------------------------------------- base de datos
    def id_base_datos(self) -> int:
        """Id de la conexión a PostgreSQL registrada con el nombre configurado."""
        if self._bd_id is None:
            datos = self._peticion("GET", "/api/database")
            lista = datos.get("data", []) if isinstance(datos, dict) else datos
            for bd in lista:
                if bd.get("name") == self.cfg.metabase_bd_nombre:
                    self._bd_id = int(bd["id"])
                    break
            else:
                raise ErrorBI(
                    f"No hay ninguna base de datos '{self.cfg.metabase_bd_nombre}' en Metabase. "
                    "Ejecuta scripts/configurar_metabase.py."
                )
        return self._bd_id

    def registrar_base_datos(self, host: str, puerto: int) -> int:
        """Registra PostgreSQL con el rol LECTOR (Metabase nunca escribe). Idempotente."""
        try:
            return self.id_base_datos()
        except ErrorBI:
            pass
        datos = self._peticion(
            "POST",
            "/api/database",
            json={
                "name": self.cfg.metabase_bd_nombre,
                "engine": "postgres",
                "details": {
                    "host": host,
                    "port": puerto,
                    "dbname": self.cfg.pg_bd,
                    "user": self.cfg.lector_usuario,
                    "password": self.cfg.lector_password,
                },
            },
        )
        self._bd_id = int(datos["id"])
        return self._bd_id

    def consultar_api(self, ruta: str) -> Any:
        """GET autenticado a cualquier ruta de la API (lo usa el comparador de la evaluación)."""
        return self._peticion("GET", ruta)

    # ---------------------------------------------------------- BIAdapter
    def estado(self) -> dict[str, Any]:
        try:
            salud = self._http.get("/api/health")
        except httpx.HTTPError as error:
            return {"disponible": False, "motivo": f"No se puede conectar con {self.base}: {error}"}
        if salud.status_code != 200:
            return {"disponible": False, "motivo": f"Metabase arrancando ({salud.status_code})"}
        try:
            return {"disponible": True, "url": self.publica, "base_datos_id": self.id_base_datos()}
        except ErrorBI as error:
            return {"disponible": True, "url": self.publica, "base_datos_id": None,
                    "motivo": str(error)}

    def sincronizar(self) -> None:
        # Asíncrono en Metabase: devuelve en cuanto la sincronización arranca.
        self._peticion("POST", f"/api/database/{self.id_base_datos()}/sync_schema")

    def crear_pregunta(self, pregunta: Pregunta) -> RefBI:
        cuerpo: dict[str, Any] = {
            "name": pregunta.nombre,
            "display": _DISPLAY[pregunta.visualizacion],
            "visualization_settings": {},
            "dataset_query": self._consulta_nativa(pregunta.sql),
        }
        if pregunta.descripcion:
            cuerpo["description"] = pregunta.descripcion
        datos = self._peticion("POST", "/api/card", json=cuerpo)
        return RefBI(int(datos["id"]), pregunta.nombre, f"{self.publica}/question/{datos['id']}")

    def actualizar_pregunta(
        self, pregunta_id: int, sql: str | None = None, nombre: str | None = None,
        visualizacion: str | None = None,
    ) -> RefBI:
        cuerpo: dict[str, Any] = {}
        if sql is not None:
            cuerpo["dataset_query"] = self._consulta_nativa(sql)
        if nombre is not None:
            cuerpo["name"] = nombre
        if visualizacion is not None:
            cuerpo["display"] = _DISPLAY[Pregunta("x", "x", visualizacion).visualizacion]
        if not cuerpo:
            raise ValueError("No hay nada que actualizar.")
        datos = self._peticion("PUT", f"/api/card/{pregunta_id}", json=cuerpo)
        return RefBI(pregunta_id, datos.get("name", nombre or ""),
                     f"{self.publica}/question/{pregunta_id}")

    def crear_dashboard(
        self, nombre: str, pregunta_ids: list[int], descripcion: str | None = None
    ) -> RefBI:
        cuerpo: dict[str, Any] = {"name": nombre}
        if descripcion:
            cuerpo["description"] = descripcion
        datos = self._peticion("POST", "/api/dashboard", json=cuerpo)
        ref = RefBI(int(datos["id"]), nombre, f"{self.publica}/dashboard/{datos['id']}")
        if pregunta_ids:
            self.anadir_a_dashboard(ref.id, pregunta_ids)
        return ref

    def anadir_a_dashboard(self, dashboard_id: int, pregunta_ids: list[int]) -> RefBI:
        """Añade tarjetas conservando las existentes.

        ``PUT /api/dashboard/:id`` reemplaza el conjunto completo de tarjetas, así que
        se envían las actuales (con su id) más las nuevas (con id negativo).
        """
        actual = self._peticion("GET", f"/api/dashboard/{dashboard_id}")
        existentes = [
            {k: t[k] for k in ("id", "card_id", "row", "col", "size_x", "size_y") if k in t}
            for t in actual.get("dashcards", [])
        ]
        fila_libre = max((t["row"] + t["size_y"] for t in existentes), default=0)
        nuevas = _colocar(pregunta_ids, fila_inicial=fila_libre)
        self._peticion("PUT", f"/api/dashboard/{dashboard_id}",
                       json={"dashcards": existentes + nuevas})
        return RefBI(
            dashboard_id, actual.get("name", ""), f"{self.publica}/dashboard/{dashboard_id}"
        )

    # ---------------------------------------------------- configuración inicial
    def uso_de_objetos(self, nombres: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """Preguntas nativas de la conexión del proyecto cuyo SQL cita cada objeto, y los
        dashboards que contienen esas preguntas."""
        bd = self.id_base_datos()
        # El nombre suelto o calificado (esquema.nombre), sin confundir v_x con v_x_2.
        patrones = {
            n: re.compile(rf"(?<![\w.]){re.escape(n)}\b|\.{re.escape(n)}\b") for n in nombres
        }
        uso: dict[str, dict[str, list[dict[str, Any]]]] = {}
        pregunta_de: dict[int, list[str]] = {}
        for card in self._peticion("GET", "/api/card") or []:
            consulta = card.get("dataset_query") or {}
            if consulta.get("database") != bd or card.get("archived"):
                continue
            texto = str((consulta.get("native") or {}).get("query") or "")
            for nombre, patron in patrones.items():
                if patron.search(texto):
                    ref = {"id": card["id"], "nombre": card.get("name", ""),
                           "url": f"{self.publica}/question/{card['id']}"}
                    uso.setdefault(nombre, {"preguntas": [], "dashboards": []})
                    uso[nombre]["preguntas"].append(ref)
                    pregunta_de.setdefault(int(card["id"]), []).append(nombre)
        if pregunta_de:
            for resumen in self._peticion("GET", "/api/dashboard") or []:
                if resumen.get("archived"):
                    continue
                tablero = self._peticion("GET", f"/api/dashboard/{resumen['id']}")
                ids = {t.get("card_id") for t in tablero.get("dashcards", [])}
                for card_id in ids & set(pregunta_de):
                    for nombre in pregunta_de[card_id]:
                        dashboards = uso[nombre]["dashboards"]
                        if all(d["id"] != tablero["id"] for d in dashboards):
                            dashboards.append({
                                "id": tablero["id"], "nombre": tablero.get("name", ""),
                                "url": f"{self.publica}/dashboard/{tablero['id']}",
                            })
        return uso

    def configurar_regional(self, idioma: str = "es", separadores: str = ",.") -> None:
        """Idioma del sitio y separadores numéricos (",." = coma decimal y punto de miles).

        Ajustes generales de Metabase (``PUT /api/setting/<clave>``); afectan a cómo se
        muestran fechas y números en todas las preguntas y dashboards.
        """
        self._peticion("PUT", "/api/setting/site-locale", json={"value": idioma})
        self._peticion(
            "PUT",
            "/api/setting/custom-formatting",
            json={"value": {"type/Number": {"number_separators": separadores}}},
        )

    def configurar_admin(self, email: str, password: str, sitio: str = "DataOps Agent") -> bool:
        """Completa el asistente inicial de Metabase si aún no se ha hecho.

        Devuelve True si lo ha completado ahora y False si ya estaba hecho.
        """
        propiedades = self._json(self._http.get("/api/session/properties"), "leer propiedades")
        token = propiedades.get("setup-token")
        if not token:
            return False
        datos = self._json(
            self._http.post(
                "/api/setup",
                json={
                    "token": token,
                    "user": {"email": email, "password": password,
                             "first_name": "Admin", "last_name": "DataOps"},
                    "prefs": {"site_name": sitio},
                },
            ),
            "completar el setup",
        )
        if "id" in datos:
            self._cabeceras = {"X-Metabase-Session": str(datos["id"])}
        return True

    # ------------------------------------------------------------- internos
    def _consulta_nativa(self, sql: str) -> dict[str, Any]:
        return {"type": "native", "native": {"query": sql}, "database": self.id_base_datos()}


def _colocar(pregunta_ids: list[int], fila_inicial: int = 0) -> list[dict[str, int]]:
    """Distribuye las tarjetas en dos columnas de 12 x 6 unidades."""
    ancho, alto = _ANCHO_REJILLA // 2, 6
    tarjetas = []
    for i, pregunta_id in enumerate(pregunta_ids):
        tarjetas.append(
            {
                "id": -(i + 1),  # negativo = tarjeta nueva
                "card_id": pregunta_id,
                "row": fila_inicial + (i // 2) * alto,
                "col": (i % 2) * ancho,
                "size_x": ancho,
                "size_y": alto,
            }
        )
    return tarjetas
