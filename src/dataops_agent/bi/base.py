"""Interfaz común para herramientas de BI (patrón adaptador).

El servidor MCP solo conoce :class:`BIAdapter`. Metabase es la primera implementación;
para añadir Superset bastaría con escribir ``SupersetAdapter`` con estos mismos
métodos, sin tocar las herramientas MCP ni las skills.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# Visualizaciones admitidas, con nombres neutrales; cada adaptador las traduce.
VISUALIZACIONES = (
    "tabla", "barras", "barras_horizontales", "lineas", "area", "tarta", "numero",
    "tendencia",   # KPI con variación frente al periodo anterior (necesita columna de fecha)
    "progreso",    # KPI frente a un objetivo (ajuste "objetivo")
    "combinado",   # barras + líneas en el mismo gráfico
    "dispersion", "embudo", "cascada",
)
# Visualizaciones de una sola cifra: por defecto ocupan una tarjeta pequeña de KPI.
VISUALIZACIONES_KPI = ("numero", "tendencia", "progreso")
# Formatos de columna admitidos en ``Ajustes.formatos``.
FORMATOS = ("moneda", "porcentaje", "entero", "decimal")
# Tamaños de tarjeta de un dashboard, de menor a mayor.
TAMANOS = ("kpi", "tercio", "medio", "ancho")

_CLAVES_AJUSTES = ("eje_x", "series", "titulo_x", "titulo_y", "apilado", "mostrar_valores",
                   "formatos", "objetivo")


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
    """Presentación de una pregunta con nombres neutrales (cada adaptador los traduce).

    * ``eje_x``: columna de categorías o fechas; ``series``: columnas numéricas a pintar
      (en un KPI, la primera es la cifra que se muestra).
    * ``titulo_x`` / ``titulo_y``: títulos de los ejes.
    * ``apilado``: barras o áreas apiladas; ``mostrar_valores``: etiqueta cada punto.
    * ``formatos``: ``{columna: moneda | porcentaje | entero | decimal}``. ``porcentaje``
      añade " %" y espera valores 0-100, como los que calculan las vistas del proyecto.
    * ``objetivo``: meta de un gráfico de ``progreso``.
    """

    eje_x: str | None = None
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
        return cls(
            eje_x=_texto_opcional(datos.get("eje_x"), "eje_x"),
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
    """Una consulta guardada con su visualización (card en Metabase, chart en Superset)."""

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

    ``tamano`` (kpi, tercio, medio o ancho) solo se aplica a preguntas; si falta, el
    adaptador lo elige según la visualización (KPI pequeños, tablas a todo el ancho).
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


class BIAdapter(ABC):
    """Operaciones de BI que necesita el agente."""

    nombre: str = "bi"

    @abstractmethod
    def estado(self) -> dict[str, Any]:
        """Disponibilidad del servicio y de la conexión a la base de datos."""

    @abstractmethod
    def sincronizar(self) -> None:
        """Pide a la herramienta que relea el esquema (tablas y vistas nuevas)."""

    @abstractmethod
    def crear_pregunta(self, pregunta: Pregunta) -> RefBI: ...

    @abstractmethod
    def actualizar_pregunta(
        self, pregunta_id: int, sql: str | None = None, nombre: str | None = None,
        visualizacion: str | None = None, ajustes: Ajustes | None = None,
    ) -> RefBI: ...

    @abstractmethod
    def crear_dashboard(
        self, nombre: str, elementos: list[Elemento], descripcion: str | None = None
    ) -> RefBI: ...

    @abstractmethod
    def anadir_a_dashboard(self, dashboard_id: int, elementos: list[Elemento]) -> RefBI: ...

    def uso_de_objetos(self, nombres: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """Qué preguntas y dashboards usan cada objeto (para la documentación).

        Por defecto no se sabe: los adaptadores que puedan, lo implementan.
        """
        raise ErrorBI(f"El adaptador {self.nombre} no informa del uso de los objetos.")

    def publicar_descripciones(
        self, esquema: str, descripciones: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Copia en la herramienta de BI las descripciones del modelo.

        ``descripciones``: ``{objeto: {"descripcion": str | None, "columnas": {col: str}}}``.
        El modelo (``COMMENT ON``) es la fuente de verdad: lo que no coincida se sustituye.
        """
        raise ErrorBI(f"El adaptador {self.nombre} no admite publicar descripciones.")
