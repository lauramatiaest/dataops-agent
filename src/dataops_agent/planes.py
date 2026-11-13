"""Patrón planificar → aplicar.

Ninguna herramienta de escritura toca la base de datos directamente. Primero se crea
un :class:`Plan` con el SQL exacto que se ejecutará, su impacto estimado y los
bloqueos detectados; el agente se lo enseña a la persona y, después, llama a
``aplicar_plan(plan_id)``. Ventajas:

* La persona (y la auditoría) ven exactamente qué se va a ejecutar ANTES de hacerlo.
* El plan es inmutable: su huella SHA-256 se comprueba al aplicar, así que no se puede
  ejecutar algo distinto de lo que se revisó.
* Un plan solo se aplica una vez y caduca tras un tiempo (el estado de la BD cambia).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from dataops_agent.transformaciones import Transformacion


class TipoPlan(StrEnum):
    CREAR_TABLA = "crear_tabla"
    CARGA = "carga"
    CLAVE = "clave_primaria"
    RELACION = "relacion"
    VISTA = "vista"
    BORRADO = "borrado"


class EstadoPlan(StrEnum):
    PENDIENTE = "pendiente"
    APLICADO = "aplicado"
    FALLIDO = "fallido"
    DESCARTADO = "descartado"


class TipoPaso(StrEnum):
    SQL = "sql"  # sentencia SQL que se valida y ejecuta tal cual
    CARGA = "carga"  # COPY de un fichero a una tabla (lo construye el servidor)


@dataclass(frozen=True)
class Paso:
    """Una acción del plan. Todas se ejecutan en una única transacción."""

    tipo: TipoPaso
    descripcion: str
    sql: str
    # Solo para pasos de carga: tabla destino, fichero y correspondencia de columnas.
    tabla: str | None = None
    fichero: str | None = None
    columnas: tuple[tuple[str, str], ...] = ()  # (columna_en_fichero, columna_en_tabla)
    filas_esperadas: int | None = None
    # SHA-256 del fichero al planificar: si cambia antes de aplicar, no se carga.
    huella_fichero: str | None = None
    # Reparación declarativa aplicada al leer el fichero (el fichero no se modifica).
    transformacion: Transformacion | None = None


@dataclass
class Plan:
    tipo: TipoPlan
    resumen: str
    pasos: list[Paso]
    impacto: dict[str, Any] = field(default_factory=dict)
    destructivo: bool = False
    bloqueos: list[str] = field(default_factory=list)  # si hay alguno, no se aplica
    advertencias: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"plan_{secrets.token_hex(4)}")
    estado: EstadoPlan = EstadoPlan.PENDIENTE
    creado: datetime = field(default_factory=lambda: datetime.now(UTC))
    huella: str = ""

    def __post_init__(self) -> None:
        self.huella = self.calcular_huella()

    def calcular_huella(self) -> str:
        """SHA-256 de lo que se ejecutará: pasos y carácter destructivo."""
        contenido = json.dumps(
            {"pasos": [asdict(p) for p in self.pasos], "destructivo": self.destructivo},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(contenido.encode()).hexdigest()

    @property
    def aplicable(self) -> bool:
        return self.estado is EstadoPlan.PENDIENTE and not self.bloqueos

    def sql_completo(self) -> str:
        return "\n".join(f"{p.sql};" for p in self.pasos)

    def como_dict(self) -> dict[str, Any]:
        """Representación que devuelve la herramienta MCP al agente."""
        siguiente = (
            "aplicar_plan_destructivo (requiere aprobación humana)"
            if self.destructivo
            else "aplicar_plan"
        )
        return {
            "plan_id": self.id,
            "tipo": str(self.tipo),
            "resumen": self.resumen,
            "sql": self.sql_completo(),
            "pasos": [p.descripcion for p in self.pasos],
            "impacto": self.impacto,
            "destructivo": self.destructivo,
            "bloqueos": self.bloqueos,
            "advertencias": self.advertencias,
            "estado": str(self.estado),
            "aplicable": self.aplicable,
            "siguiente_paso": (
                f"Muestra el plan a la persona y usa {siguiente}('{self.id}')."
                if self.aplicable
                else "No aplicable: resuelve los bloqueos y vuelve a planificar."
            ),
        }


class ErrorPlan(ValueError):
    """El plan no existe, ha caducado, ya se aplicó o ha sido alterado."""


class AlmacenPlanes:
    """Guarda los planes en memoria durante la vida del servidor MCP.

    Claude Code mantiene vivo el proceso del servidor durante la sesión, y planificar
    y aplicar ocurren en la misma sesión. Si el servidor se reinicia, el agente vuelve
    a planificar (el estado de la BD puede haber cambiado de todos modos).
    """

    def __init__(self, caducidad: timedelta) -> None:
        self._planes: dict[str, Plan] = {}
        self._caducidad = caducidad
        self._cerrojo = threading.Lock()

    def guardar(self, plan: Plan) -> Plan:
        with self._cerrojo:
            self._planes[plan.id] = plan
        return plan

    def obtener_para_aplicar(self, plan_id: str, ahora: datetime | None = None) -> Plan:
        """Devuelve el plan si puede aplicarse; si no, lanza :class:`ErrorPlan`."""
        with self._cerrojo:
            plan = self._planes.get(plan_id)
        if plan is None:
            raise ErrorPlan(f"No existe el plan {plan_id}. Vuelve a planificar.")
        if plan.estado is not EstadoPlan.PENDIENTE:
            raise ErrorPlan(f"El plan {plan_id} ya está en estado '{plan.estado}'.")
        if (ahora or datetime.now(UTC)) - plan.creado > self._caducidad:
            raise ErrorPlan(f"El plan {plan_id} ha caducado. Vuelve a planificar.")
        if plan.calcular_huella() != plan.huella:
            raise ErrorPlan(f"El plan {plan_id} ha sido alterado; no se aplica.")
        if plan.bloqueos:
            raise ErrorPlan(f"El plan {plan_id} tiene bloqueos: {'; '.join(plan.bloqueos)}")
        return plan

    def obtener(self, plan_id: str) -> Plan:
        """Devuelve el plan en cualquier estado (p. ej. para validar una carga aplicada)."""
        with self._cerrojo:
            plan = self._planes.get(plan_id)
        if plan is None:
            raise ErrorPlan(f"No existe el plan {plan_id} en esta sesión.")
        return plan

    def descartar(self, plan_id: str) -> Plan:
        """Retira un plan pendiente (p. ej. sustituido por otro): ya no podrá aplicarse."""
        with self._cerrojo:
            plan = self._planes.get(plan_id)
            if plan is None:
                raise ErrorPlan(f"No existe el plan {plan_id} en esta sesión.")
            if plan.estado is not EstadoPlan.PENDIENTE:
                raise ErrorPlan(f"El plan {plan_id} está en estado '{plan.estado}'.")
            plan.estado = EstadoPlan.DESCARTADO
        return plan

    def marcar(self, plan_id: str, estado: EstadoPlan) -> None:
        with self._cerrojo:
            self._planes[plan_id].estado = estado

    def pendientes(self) -> list[Plan]:
        with self._cerrojo:
            return [p for p in self._planes.values() if p.estado is EstadoPlan.PENDIENTE]
