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
VISUALIZACIONES = ("tabla", "barras", "barras_horizontales", "lineas", "area", "tarta", "numero")


class ErrorBI(RuntimeError):
    """Fallo al hablar con la herramienta de BI."""


@dataclass(frozen=True)
class Pregunta:
    """Una consulta guardada con su visualización (card en Metabase, chart en Superset)."""

    nombre: str
    sql: str
    visualizacion: str = "tabla"
    descripcion: str | None = None

    def __post_init__(self) -> None:
        if self.visualizacion not in VISUALIZACIONES:
            raise ValueError(
                f"Visualización no admitida: {self.visualizacion}. "
                f"Opciones: {', '.join(VISUALIZACIONES)}."
            )


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
        visualizacion: str | None = None,
    ) -> RefBI: ...

    @abstractmethod
    def crear_dashboard(
        self, nombre: str, pregunta_ids: list[int], descripcion: str | None = None
    ) -> RefBI: ...

    @abstractmethod
    def anadir_a_dashboard(self, dashboard_id: int, pregunta_ids: list[int]) -> RefBI: ...
