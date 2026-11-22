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
* ``GET  /api/database/:id/metadata``        → tables[{id, name, schema, description,
  fields[{id, name, description}]}]
* ``PUT  /api/table/:id`` y ``PUT /api/field/:id`` → {"description": ...}
* ``POST /api/card``                         → {name, display, visualization_settings,
  dataset_query}
* ``GET  /api/card/:id``                     → incluye "display" (para elegir el tamaño)
* ``PUT  /api/card/:id``                     → campos opcionales (p. ej. dataset_query)
* ``POST /api/dashboard``                    → {name, description?}
* ``PUT  /api/dashboard/:id``                → {"dashcards": [...]} REEMPLAZA todas las tarjetas
  (desde 0.48; ``POST /api/dashboard/:id/cards`` se eliminó en 0.47) y admite
  ``"width": "full"`` (dashboard a todo el ancho, desde 0.47)
* Tarjetas de título y de texto: dashcards con ``card_id`` nulo y
  ``visualization_settings.virtual_card.display`` = "heading" o "text" más ``text``

Ajustes de visualización usados (``visualization_settings`` de una card):
``graph.dimensions``, ``graph.metrics``, ``graph.x_axis.title_text``,
``graph.y_axis.title_text``, ``stackable.stack_type``, ``graph.show_values``,
``pie.dimension``, ``pie.metric``, ``funnel.dimension``, ``funnel.metric``,
``scalar.field``, ``progress.goal`` y ``column_settings`` (clave ``["name","col"]``
con ``number_style``/``currency``, ``suffix`` o ``decimals``).

Las consultas se envían en el formato "nativo" clásico
``{"type": "native", "native": {"query": ...}, "database": id}``, que Metabase sigue
aceptando y normaliza internamente a MBQL 5.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from dataops_agent.bi.base import (
    VISUALIZACIONES_KPI,
    Ajustes,
    BIAdapter,
    Elemento,
    ErrorBI,
    Pregunta,
    RefBI,
)
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
    "tendencia": "smartscalar",
    "progreso": "progress",
    "combinado": "combo",
    "dispersion": "scatter",
    "embudo": "funnel",
    "cascada": "waterfall",
}
_NEUTRAL = {v: k for k, v in _DISPLAY.items()}
_ANCHO_REJILLA = 24  # columnas de la rejilla de un dashboard de Metabase
# (ancho, alto) en unidades de la rejilla para cada tamaño neutral.
_TAMANOS = {"kpi": (6, 3), "tercio": (8, 6), "medio": (12, 6), "ancho": (24, 7)}
_FORMATOS = {
    "moneda": {"number_style": "currency", "currency": "EUR", "currency_style": "symbol"},
    "porcentaje": {"suffix": " %"},
    "entero": {"decimals": 0},
    "decimal": {"decimals": 2},
}
# Campos de una dashcard existente que hay que reenviar para conservarla tal cual.
_CAMPOS_DASHCARD = ("id", "card_id", "row", "col", "size_x", "size_y",
                    "visualization_settings", "parameter_mappings", "dashboard_tab_id")


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
            "visualization_settings": ajustes_visualizacion(pregunta.visualizacion,
                                                            pregunta.ajustes),
            "dataset_query": self._consulta_nativa(pregunta.sql),
        }
        if pregunta.descripcion:
            cuerpo["description"] = pregunta.descripcion
        datos = self._peticion("POST", "/api/card", json=cuerpo)
        return RefBI(int(datos["id"]), pregunta.nombre, f"{self.publica}/question/{datos['id']}")

    def actualizar_pregunta(
        self, pregunta_id: int, sql: str | None = None, nombre: str | None = None,
        visualizacion: str | None = None, ajustes: Ajustes | None = None,
    ) -> RefBI:
        cuerpo: dict[str, Any] = {}
        if sql is not None:
            cuerpo["dataset_query"] = self._consulta_nativa(sql)
        if nombre is not None:
            cuerpo["name"] = nombre
        if visualizacion is not None or ajustes is not None:
            if visualizacion is None:
                actual = self._peticion("GET", f"/api/card/{pregunta_id}")
                visualizacion = _NEUTRAL.get(actual.get("display", ""), "tabla")
            valida = Pregunta("x", "x", visualizacion, ajustes=ajustes or Ajustes())
            cuerpo["display"] = _DISPLAY[valida.visualizacion]
            if ajustes is not None:
                cuerpo["visualization_settings"] = ajustes_visualizacion(visualizacion, ajustes)
        if not cuerpo:
            raise ValueError("No hay nada que actualizar.")
        datos = self._peticion("PUT", f"/api/card/{pregunta_id}", json=cuerpo)
        return RefBI(pregunta_id, datos.get("name", nombre or ""),
                     f"{self.publica}/question/{pregunta_id}")

    def crear_dashboard(
        self, nombre: str, elementos: list[Elemento], descripcion: str | None = None
    ) -> RefBI:
        cuerpo: dict[str, Any] = {"name": nombre}
        if descripcion:
            cuerpo["description"] = descripcion
        datos = self._peticion("POST", "/api/dashboard", json=cuerpo)
        ref = RefBI(int(datos["id"]), nombre, f"{self.publica}/dashboard/{datos['id']}")
        if elementos:
            self.anadir_a_dashboard(ref.id, elementos)
        return ref

    def anadir_a_dashboard(self, dashboard_id: int, elementos: list[Elemento]) -> RefBI:
        """Añade elementos debajo de los existentes, conservándolos.

        ``PUT /api/dashboard/:id`` reemplaza el conjunto completo de tarjetas, así que
        se envían las actuales (con su id y sus ajustes, que es donde va el texto de las
        tarjetas de título) más las nuevas (con id negativo).
        """
        if not elementos:
            raise ValueError("No hay nada que añadir al dashboard.")
        actual = self._peticion("GET", f"/api/dashboard/{dashboard_id}")
        existentes = [
            {k: t[k] for k in _CAMPOS_DASHCARD if t.get(k) is not None}
            | {"card_id": t.get("card_id")}
            for t in actual.get("dashcards", [])
        ]
        fila_libre = max((t["row"] + t["size_y"] for t in existentes), default=0)
        tamanos = {e.pregunta_id: self._tamano_por_defecto(e.pregunta_id)
                   for e in elementos if e.pregunta_id is not None and e.tamano is None}
        nuevas = colocar(elementos, tamanos, fila_inicial=fila_libre)
        self._peticion("PUT", f"/api/dashboard/{dashboard_id}",
                       json={"dashcards": existentes + nuevas, "width": "full"})
        return RefBI(
            dashboard_id, actual.get("name", ""), f"{self.publica}/dashboard/{dashboard_id}"
        )

    def _tamano_por_defecto(self, pregunta_id: int) -> str:
        """KPI pequeños, tablas a todo el ancho y el resto a media anchura."""
        display = self._peticion("GET", f"/api/card/{pregunta_id}").get("display")
        visualizacion = _NEUTRAL.get(display or "", "tabla")
        if visualizacion in VISUALIZACIONES_KPI:
            return "kpi"
        return "ancho" if visualizacion == "tabla" else "medio"

    def publicar_descripciones(
        self, esquema: str, descripciones: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Metabase solo copia el comentario de PostgreSQL la primera vez (cuando su
        descripción está vacía), así que una descripción corregida en el modelo no le
        llega al sincronizar (I-30). Aquí se escribe por la API, solo donde difiere."""
        metadatos = self._peticion("GET", f"/api/database/{self.id_base_datos()}/metadata")
        tablas = {t.get("name"): t for t in metadatos.get("tables", [])
                  if t.get("schema") == esquema}
        actualizadas: list[str] = []
        sin_cambios = 0
        no_encontradas: list[str] = []
        for objeto, datos in sorted(descripciones.items()):
            tabla = tablas.get(objeto)
            if tabla is None:
                no_encontradas.append(objeto)
                continue
            texto = datos.get("descripcion")
            if texto and tabla.get("description") != texto:
                self._peticion("PUT", f"/api/table/{tabla['id']}", json={"description": texto})
                actualizadas.append(objeto)
            elif texto:
                sin_cambios += 1
            campos = {c.get("name"): c for c in tabla.get("fields", [])}
            for columna, texto_col in sorted((datos.get("columnas") or {}).items()):
                campo = campos.get(columna)
                if campo is None:
                    no_encontradas.append(f"{objeto}.{columna}")
                elif campo.get("description") != texto_col:
                    self._peticion("PUT", f"/api/field/{campo['id']}",
                                   json={"description": texto_col})
                    actualizadas.append(f"{objeto}.{columna}")
                else:
                    sin_cambios += 1
        resultado: dict[str, Any] = {"actualizadas": actualizadas, "sin_cambios": sin_cambios}
        if no_encontradas:
            resultado["no_encontradas"] = no_encontradas
            resultado["aviso"] = ("Metabase aún no conoce estos objetos (la sincronización "
                                  "tarda unos segundos): vuelve a llamar a sincronizar_bi.")
        return resultado

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
            # Formato antiguo ({"native": {"query": ...}}) o nuevo de Metabase
            # ({"stages": [{"native": ...}]}): se buscan todos sus textos.
            texto = "\n".join(_textos(consulta))
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


def _textos(valor: Any) -> list[str]:
    """Todas las cadenas de una estructura JSON (dict/list anidados)."""
    if isinstance(valor, str):
        return [valor]
    if isinstance(valor, dict):
        return [t for v in valor.values() for t in _textos(v)]
    if isinstance(valor, list):
        return [t for v in valor for t in _textos(v)]
    return []


def ajustes_visualizacion(visualizacion: str, ajustes: Ajustes) -> dict[str, Any]:
    """Traduce los ajustes neutrales a ``visualization_settings`` de Metabase."""
    vs: dict[str, Any] = {}
    if visualizacion == "tarta":
        dimension, metrica = "pie.dimension", "pie.metric"
    elif visualizacion == "embudo":
        dimension, metrica = "funnel.dimension", "funnel.metric"
    else:
        dimension, metrica = "graph.dimensions", "graph.metrics"
    if visualizacion in VISUALIZACIONES_KPI:
        if ajustes.series:
            vs["scalar.field"] = ajustes.series[0]
        if visualizacion == "tendencia" and ajustes.eje_x:
            vs["graph.dimensions"] = [ajustes.eje_x]
    else:
        if ajustes.eje_x:
            vs[dimension] = ajustes.eje_x if dimension != "graph.dimensions" else [ajustes.eje_x]
        if ajustes.series:
            vs[metrica] = (ajustes.series[0] if metrica != "graph.metrics"
                           else list(ajustes.series))
    if ajustes.titulo_x:
        vs["graph.x_axis.title_text"] = ajustes.titulo_x
    if ajustes.titulo_y:
        vs["graph.y_axis.title_text"] = ajustes.titulo_y
    if ajustes.apilado:
        vs["stackable.stack_type"] = "stacked"
    if ajustes.mostrar_valores:
        vs["graph.show_values"] = True
    if ajustes.objetivo is not None:
        vs["progress.goal"] = ajustes.objetivo
    if ajustes.formatos:
        vs["column_settings"] = {
            json.dumps(["name", columna], separators=(",", ":")): dict(_FORMATOS[formato])
            for columna, formato in ajustes.formatos
        }
    return vs


def _tarjeta_virtual(tipo: str, texto: str) -> dict[str, Any]:
    return {
        "virtual_card": {"name": None, "display": tipo, "visualization_settings": {},
                         "dataset_query": {}, "archived": False},
        "text": texto,
    }


def colocar(
    elementos: list[Elemento], tamanos: dict[int, str] | None = None, fila_inicial: int = 0
) -> list[dict[str, Any]]:
    """Coloca los elementos de izquierda a derecha en la rejilla de 24 columnas.

    Las preguntas se empaquetan en filas según su tamaño (4 KPI, 3 tercios, 2 medios
    o 1 ancho por fila). Una sección o un texto cierra la fila en curso y ocupa todo
    el ancho, de modo que agrupa debajo las preguntas que le siguen.
    """
    tamanos = tamanos or {}
    tarjetas: list[dict[str, Any]] = []
    fila, col, alto_fila = fila_inicial, 0, 0

    def nueva_fila() -> None:
        nonlocal fila, col, alto_fila
        fila, col, alto_fila = fila + alto_fila, 0, 0

    for elemento in elementos:
        nuevo_id = -(len(tarjetas) + 1)  # negativo = tarjeta nueva
        if elemento.pregunta_id is None:
            nueva_fila()
            if elemento.seccion is not None:
                alto, ajustes = 1, _tarjeta_virtual("heading", elemento.seccion)
            else:
                texto = elemento.texto or ""
                alto = min(max(2, texto.count("\n") + 1), 6)
                ajustes = _tarjeta_virtual("text", texto)
            tarjetas.append({"id": nuevo_id, "card_id": None, "row": fila, "col": 0,
                             "size_x": _ANCHO_REJILLA, "size_y": alto,
                             "visualization_settings": ajustes})
            fila += alto
            continue
        ancho, alto = _TAMANOS[elemento.tamano or tamanos[elemento.pregunta_id]]
        if col + ancho > _ANCHO_REJILLA:
            nueva_fila()
        tarjetas.append({"id": nuevo_id, "card_id": elemento.pregunta_id, "row": fila,
                         "col": col, "size_x": ancho, "size_y": alto})
        col += ancho
        alto_fila = max(alto_fila, alto)
    return tarjetas
