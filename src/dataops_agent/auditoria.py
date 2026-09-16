"""Registro de auditoría: quién, qué, cuándo, con qué resultado y en qué plan.

Cada evento se escribe en DOS destinos:

* Un fichero JSONL (una línea JSON por evento) que siempre está disponible, incluso si
  la base de datos falla, y que es fácil de revisar o procesar en la evaluación.
* La tabla ``dataops_meta.auditoria`` de PostgreSQL, sobre la que el rol escritor solo
  tiene INSERT y SELECT (no puede modificar ni borrar el histórico).

La escritura en base de datos usa una conexión propia en autocommit (la proporciona
quien construye el registro), de modo que el evento queda guardado aunque la operación
auditada haga ROLLBACK.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class Resultado(StrEnum):
    OK = "ok"
    ERROR = "error"
    RECHAZADO = "rechazado"  # bloqueado por una barrera de seguridad
    PENDIENTE_CONFIRMACION = "pendiente_confirmacion"


@dataclass
class EventoAuditoria:
    """Un evento auditable. ``detalle`` admite cualquier dato serializable a JSON."""

    herramienta: str
    operacion: str
    resultado: Resultado
    sql: str | None = None
    plan_id: str | None = None
    destructiva: bool = False
    detalle: dict[str, Any] = field(default_factory=dict)
    duracion_ms: int | None = None
    # Rellenados por el registro al guardar:
    actor: str = ""
    rol_bd: str | None = None
    esquema: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    instante: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def como_dict(self) -> dict[str, Any]:
        datos = asdict(self)
        datos["resultado"] = str(self.resultado)
        return datos


EscritorBD = Callable[[EventoAuditoria], None]


class RegistroAuditoria:
    """Guarda eventos de auditoría en JSONL y, si está disponible, en PostgreSQL."""

    def __init__(
        self,
        ruta_jsonl: Path,
        actor: str,
        esquema: str,
        escritor_bd: EscritorBD | None = None,
    ) -> None:
        self.ruta_jsonl = ruta_jsonl
        self.actor = actor
        self.esquema = esquema
        self._escritor_bd = escritor_bd
        self._cerrojo = threading.Lock()
        self.ruta_jsonl.parent.mkdir(parents=True, exist_ok=True)

    def registrar(self, evento: EventoAuditoria) -> EventoAuditoria:
        """Completa el evento con actor y esquema y lo persiste en ambos destinos."""
        evento.actor = evento.actor or self.actor
        evento.esquema = evento.esquema or self.esquema

        bd_ok = True
        if self._escritor_bd is not None:
            try:
                self._escritor_bd(evento)
            except Exception as error:  # la auditoría nunca debe tumbar la operación
                bd_ok = False
                # stderr, nunca stdout: stdout es el canal del protocolo MCP (stdio).
                print(f"[auditoría] fallo al escribir en BD: {error}", file=sys.stderr)

        linea = evento.como_dict()
        linea["guardado_en_bd"] = bd_ok and self._escritor_bd is not None
        with self._cerrojo, self.ruta_jsonl.open("a", encoding="utf-8") as fichero:
            fichero.write(json.dumps(linea, ensure_ascii=False, default=str) + "\n")
        return evento

    def leer_jsonl(self) -> list[dict[str, Any]]:
        """Devuelve todos los eventos del fichero JSONL (útil en tests y evaluación)."""
        if not self.ruta_jsonl.exists():
            return []
        with self.ruta_jsonl.open(encoding="utf-8") as fichero:
            return [json.loads(linea) for linea in fichero if linea.strip()]
