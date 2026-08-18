"""Conexiones a PostgreSQL separadas por rol (lectura / escritura).

Cada conexión se abre con parámetros de sesión fijados en el arranque
(``options=-c ...``), de modo que el SQL del agente no puede cambiarlos:

* ``statement_timeout``: ninguna sentencia puede bloquear la base de datos.
* ``search_path``: solo el esquema del proyecto; los nombres sin calificar se
  resuelven ahí (``pg_catalog`` sigue siendo implícito, por eso el validador
  rechaza los nombres ``pg_*``).
* ``default_transaction_read_only`` en las conexiones de lectura.

Se abre una conexión por operación. Para un prototipo con un único usuario es más
simple y robusto que un pool (sin estado compartido entre llamadas).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb

from dataops_agent.auditoria import EventoAuditoria
from dataops_agent.config import Config


class GestorConexiones:
    """Fabrica conexiones con las barreras de sesión ya aplicadas."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _opciones(self, solo_lectura: bool) -> str:
        opciones = [
            f"-c statement_timeout={self.cfg.timeout_ms}",
            f"-c search_path={self.cfg.esquema}",
            "-c lock_timeout=5000",
        ]
        if solo_lectura:
            opciones.append("-c default_transaction_read_only=on")
        return " ".join(opciones)

    @contextmanager
    def lectura(self) -> Iterator[psycopg.Connection]:
        """Conexión con el rol lector en una transacción de solo lectura."""
        with psycopg.connect(self.cfg.dsn("lector"), options=self._opciones(True)) as conn:
            conn.read_only = True
            yield conn
            conn.rollback()  # una lectura nunca necesita confirmar nada

    @contextmanager
    def escritura(self) -> Iterator[psycopg.Connection]:
        """Conexión con el rol escritor. Todo el bloque es UNA transacción:
        si algo falla dentro, se deshace entero (COMMIT solo al salir sin error)."""
        with psycopg.connect(self.cfg.dsn("escritor"), options=self._opciones(False)) as conn:
            yield conn

    def escribir_auditoria(self, evento: EventoAuditoria) -> None:
        """Inserta un evento en dataops_meta.auditoria con una conexión independiente."""
        evento.rol_bd = evento.rol_bd or self.cfg.escritor_usuario
        with psycopg.connect(self.cfg.dsn("escritor"), autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO dataops_meta.auditoria
                    (id, instante, actor, rol_bd, esquema, herramienta, operacion,
                     plan_id, sql, destructiva, resultado, detalle, duracion_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    evento.id,
                    evento.instante,
                    evento.actor,
                    evento.rol_bd,
                    evento.esquema,
                    evento.herramienta,
                    evento.operacion,
                    evento.plan_id,
                    evento.sql,
                    evento.destructiva,
                    str(evento.resultado),
                    Jsonb(evento.detalle, dumps=lambda o: json.dumps(o, default=str)),
                    evento.duracion_ms,
                ),
            )
