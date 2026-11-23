"""Integración con Metabase (la herramienta de dashboards) mediante su API REST.

El agente trabaja con nombres neutrales (``Pregunta``, ``Ajustes``, ``Elemento``) y este
módulo los traduce a la API de Metabase.

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
from dataclasses import dataclass
from typing import Any

import httpx

from dataops_agent.config import Config

# Visualizaciones admitidas, con nombres neutrales; este módulo las traduce.
VISUALIZACIONES = (
    "tabla", "barras", "barras_horizontales", "lineas", "area", "tarta", "numero",
    "tendencia",   # KPI con variación frente al periodo anterior (necesita columna de fecha)
    "progreso",    # KPI frente a un objetivo (ajuste "objetivo")
    "combinado",   # barras + líneas en el mismo gráfico
    "dispersion", "embudo", "cascada",
)
# Visualizaciones de una sola cifra (KPI).
VISUALIZACIONES_KPI = ("numero", "tendencia", "progreso")
# Formatos de columna admitidos en ``Ajustes.formatos``.
FORMATOS = ("moneda", "porcentaje", "entero", "decimal")
# Tamaños de tarjeta de un dashboard, de menor a mayor.
TAMANOS = ("kpi", "tercio", "medio", "ancho")

_CLAVES_AJUSTES = ("eje_x", "desglose", "series", "titulo_x", "titulo_y", "apilado",
                   "mostrar_valores", "formatos", "objetivo")


class ErrorBI(RuntimeError):
    """Fallo al hablar con la herramienta de BI."""


def _texto_opcional(valor: Any, clave: str) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError(f"'{clave}' debe ser un texto no vacío.")
    return valor.strip()


@dataclass(frozen=True)
class Ajustes:
    """Presentación de una pregunta con nombres neutrales (se traducen a Metabase).

    * ``eje_x``: columna de categorías o fechas; ``series``: columnas numéricas a pintar
      (en un KPI, la primera es la cifra que se muestra).
    * ``desglose``: segunda columna de categorías que parte cada valor del eje en una
      serie por categoría (p. ej. importe por mes desglosado por canal); con ``apilado``
      las series se apilan.
    * ``titulo_x`` / ``titulo_y``: títulos de los ejes.
    * ``apilado``: barras o áreas apiladas; ``mostrar_valores``: etiqueta cada punto.
    * ``formatos``: ``{columna: moneda | porcentaje | entero | decimal}``. ``porcentaje``
      añade " %" y espera valores 0-100, como los que calculan las vistas del proyecto.
    * ``objetivo``: meta de un gráfico de ``progreso``.
    """

    eje_x: str | None = None
    desglose: str | None = None
    series: tuple[str, ...] = ()
    titulo_x: str | None = None
    titulo_y: str | None = None
    apilado: bool = False
    mostrar_valores: bool = False
    formatos: tuple[tuple[str, str], ...] = ()
    objetivo: float | None = None

    @classmethod
    def desde_dict(cls, datos: dict[str, Any] | None) -> Ajustes:
        if not datos:
            return cls()
        if not isinstance(datos, dict):
            raise ValueError("'ajustes' debe ser un objeto.")
        if extra := sorted(set(datos) - set(_CLAVES_AJUSTES)):
            raise ValueError(
                f"Ajustes desconocidos: {', '.join(extra)}. "
                f"Admitidos: {', '.join(_CLAVES_AJUSTES)}."
            )
        series = datos.get("series") or []
        if isinstance(series, str):
            series = [series]
        if not isinstance(series, list) or not all(isinstance(c, str) and c for c in series):
            raise ValueError("'series' debe ser una lista de nombres de columna.")
        formatos = datos.get("formatos") or {}
        if not isinstance(formatos, dict):
            raise ValueError("'formatos' debe ser un objeto {columna: formato}.")
        for columna, formato in formatos.items():
            if formato not in FORMATOS:
                raise ValueError(
                    f"Formato no admitido para {columna}: {formato}. "
                    f"Opciones: {', '.join(FORMATOS)}."
                )
        objetivo = datos.get("objetivo")
        if objetivo is not None and (isinstance(objetivo, bool)
                                     or not isinstance(objetivo, int | float)):
            raise ValueError("'objetivo' debe ser un número.")
        for clave in ("apilado", "mostrar_valores"):
            if not isinstance(datos.get(clave, False), bool):
                raise ValueError(f"'{clave}' debe ser true o false.")
        if datos.get("desglose") is not None and datos.get("eje_x") is None:
            raise ValueError("'desglose' necesita también 'eje_x'.")
        return cls(
            eje_x=_texto_opcional(datos.get("eje_x"), "eje_x"),
            desglose=_texto_opcional(datos.get("desglose"), "desglose"),
            series=tuple(series),
            titulo_x=_texto_opcional(datos.get("titulo_x"), "titulo_x"),
            titulo_y=_texto_opcional(datos.get("titulo_y"), "titulo_y"),
            apilado=datos.get("apilado", False),
            mostrar_valores=datos.get("mostrar_valores", False),
            formatos=tuple(sorted(formatos.items())),
            objetivo=None if objetivo is None else float(objetivo),
        )


@dataclass(frozen=True)
class Pregunta:
    """Una consulta guardada con su visualización (una *card* de Metabase)."""

    nombre: str
    sql: str
    visualizacion: str = "tabla"
    descripcion: str | None = None
    ajustes: Ajustes = Ajustes()

    def __post_init__(self) -> None:
        if self.visualizacion not in VISUALIZACIONES:
            raise ValueError(
                f"Visualización no admitida: {self.visualizacion}. "
                f"Opciones: {', '.join(VISUALIZACIONES)}."
            )
        if self.visualizacion == "progreso" and self.ajustes.objetivo is None:
            raise ValueError("Una pregunta de 'progreso' necesita el ajuste 'objetivo'.")


@dataclass(frozen=True)
class Elemento:
    """Un elemento de un dashboard: una pregunta, un título de sección o un texto.

    ``tamano`` (kpi, tercio, medio o ancho) solo se aplica a preguntas; si falta, "medio".
    """

    pregunta_id: int | None = None
    tamano: str | None = None
    seccion: str | None = None
    texto: str | None = None

    def __post_init__(self) -> None:
        if sum(x is not None for x in (self.pregunta_id, self.seccion, self.texto)) != 1:
            raise ValueError(
                "Cada elemento lleva exactamente uno de: pregunta_id, seccion o texto."
            )
        if self.tamano is not None and self.tamano not in TAMANOS:
            raise ValueError(f"Tamaño no admitido: {self.tamano}. Opciones: {', '.join(TAMANOS)}.")
        if self.tamano is not None and self.pregunta_id is None:
            raise ValueError("'tamano' solo se aplica a preguntas.")

    @classmethod
    def desde_dict(cls, datos: dict[str, Any]) -> Elemento:
        if not isinstance(datos, dict):
            raise ValueError("Cada elemento del dashboard debe ser un objeto.")
        if extra := sorted(set(datos) - {"pregunta_id", "tamano", "seccion", "texto"}):
            raise ValueError(f"Claves desconocidas en un elemento: {', '.join(extra)}.")
        pregunta_id = datos.get("pregunta_id")
        if pregunta_id is not None and (isinstance(pregunta_id, bool)
                                        or not isinstance(pregunta_id, int)):
            raise ValueError("'pregunta_id' debe ser un entero.")
        return cls(
            pregunta_id=pregunta_id,
            tamano=datos.get("tamano"),
            seccion=_texto_opcional(datos.get("seccion"), "seccion"),
            texto=_texto_opcional(datos.get("texto"), "texto"),
        )


def elementos_dashboard(
    pregunta_ids: list[int] | None, elementos: list[dict[str, Any]] | None
) -> list[Elemento]:
    """Une las dos formas de indicar el contenido: lista simple de ids o elementos."""
    if pregunta_ids and elementos:
        raise ValueError("Indica 'pregunta_ids' o 'elementos', no los dos.")
    if elementos:
        return [Elemento.desde_dict(e) for e in elementos]
    return [Elemento(pregunta_id=i) for i in pregunta_ids or []]


@dataclass(frozen=True)
class RefBI:
    """Referencia a un objeto creado en la herramienta de BI."""

    id: int
    nombre: str
    url: str

    def como_dict(self) -> dict[str, Any]:
        return {"id": self.id, "nombre": self.nombre, "url": self.url}


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
_ANCHO_REJILLA = 24  # columnas de la rejilla de un dashboard de Metabase
# (ancho, alto) en unidades de la rejilla para cada tamaño neutral.
_TAMANOS = {"kpi": (6, 3), "tercio": (8, 6), "medio": (12, 6), "ancho": (24, 7)}
_FORMATOS = {
    "moneda": {"number_style": "currency", "currency": "EUR", "currency_style": "symbol"},
    "porcentaje": {"suffix": " %"},
    "entero": {"decimals": 0},
    "decimal": {"decimals": 2},
}


class MetabaseAdapter:
    """Las operaciones de BI que usa el servidor MCP, sobre la API REST de Metabase."""

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

    # ------------------------------------------------ preguntas y dashboards
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

    def crear_dashboard(
        self, nombre: str, elementos: list[Elemento], descripcion: str | None = None
    ) -> RefBI:
        cuerpo: dict[str, Any] = {"name": nombre}
        if descripcion:
            cuerpo["description"] = descripcion
        datos = self._peticion("POST", "/api/dashboard", json=cuerpo)
        dashboard_id = int(datos["id"])
        if elementos:
            # PUT /api/dashboard/:id fija todas las tarjetas (id negativo = nueva) y el ancho.
            self._peticion("PUT", f"/api/dashboard/{dashboard_id}",
                           json={"dashcards": colocar(elementos), "width": "full"})
        return RefBI(dashboard_id, nombre, f"{self.publica}/dashboard/{dashboard_id}")

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
        if ajustes.eje_x and dimension != "graph.dimensions":
            vs[dimension] = ajustes.eje_x
        elif ajustes.eje_x:
            vs[dimension] = [ajustes.eje_x, *([ajustes.desglose] if ajustes.desglose else [])]
        if ajustes.series:
            vs[metrica] = (ajustes.series[0] if metrica != "graph.metrics"
                           else list(ajustes.series))
    if ajustes.titulo_x:
        vs["graph.x_axis.title_text"] = ajustes.titulo_x
    # Con doble eje (combinado con varias series) Metabase pone este título en los DOS
    # ejes; sin él, cada eje lleva el nombre de su serie, que es lo correcto.
    doble_eje = visualizacion == "combinado" and len(ajustes.series) > 1
    if ajustes.titulo_y and not doble_eje:
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


def colocar(elementos: list[Elemento]) -> list[dict[str, Any]]:
    """Coloca los elementos de izquierda a derecha en la rejilla de 24 columnas.

    Las preguntas se empaquetan en filas según su tamaño (4 KPI, 3 tercios, 2 medios
    o 1 ancho por fila). Una sección o un texto cierra la fila en curso y ocupa todo
    el ancho, de modo que agrupa debajo las preguntas que le siguen.
    """
    tarjetas: list[dict[str, Any]] = []
    fila, col, alto_fila = 0, 0, 0

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
                # Una fila de rejilla por cada ~180 caracteres o salto de línea.
                lineas = sum(1 + len(linea) // 180 for linea in texto.splitlines() or [""])
                alto = min(max(1, lineas), 6)
                ajustes = _tarjeta_virtual("text", texto)
            tarjetas.append({"id": nuevo_id, "card_id": None, "row": fila, "col": 0,
                             "size_x": _ANCHO_REJILLA, "size_y": alto,
                             "visualization_settings": ajustes})
            fila += alto
            continue
        ancho, alto = _TAMANOS[elemento.tamano or "medio"]
        if col + ancho > _ANCHO_REJILLA:
            nueva_fila()
        tarjetas.append({"id": nuevo_id, "card_id": elemento.pregunta_id, "row": fila,
                         "col": col, "size_x": ancho, "size_y": alto})
        col += ancho
        alto_fila = max(alto_fila, alto)
    return tarjetas
