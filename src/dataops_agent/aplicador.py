"""Aplicación de planes: la ÚNICA ruta por la que se escribe en la base de datos.

Garantías al aplicar un plan:

1. El plan existe, está pendiente, no ha caducado, no tiene bloqueos y su huella no
   ha cambiado (lo comprueba :class:`AlmacenPlanes`).
2. Un plan destructivo solo se aplica por la herramienta ``aplicar_plan_destructivo``,
   que Claude Code siempre somete a aprobación humana (regla ``ask`` del proyecto).
3. Cada sentencia se vuelve a validar con sqlglot justo antes de ejecutarse.
4. Todos los pasos van en UNA transacción: o se aplica todo, o nada.
5. Las cargas se verifican dentro de la propia transacción (filas insertadas frente a
   filas del fichero) y el fichero no puede haber cambiado desde que se planificó.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

from dataops_agent import catalogo
from dataops_agent.conexion import GestorConexiones
from dataops_agent.config import Config, validar_identificador
from dataops_agent.ficheros import FicheroTabular, abrir, huella
from dataops_agent.planes import AlmacenPlanes, EstadoPlan, Paso, Plan, TipoPaso
from dataops_agent.seguridad import Modo, ValidadorSQL
from dataops_agent.transformaciones import OrigenTransformado


class RequiereConfirmacion(PermissionError):
    """El plan es destructivo y se intentó aplicar sin la herramienta con aprobación."""


class ErrorVerificacion(RuntimeError):
    """La verificación posterior a un paso no cuadra; se deshace la transacción."""


@dataclass
class Aplicador:
    cfg: Config
    gestor: GestorConexiones
    validador: ValidadorSQL
    almacen: AlmacenPlanes

    def preparar(self, plan_id: str, permitir_destructivo: bool) -> Plan:
        """Comprueba que el plan se puede aplicar por esta vía, sin ejecutar nada."""
        plan = self.almacen.obtener_para_aplicar(plan_id)
        if plan.destructivo and not permitir_destructivo:
            raise RequiereConfirmacion(
                f"El plan {plan_id} es DESTRUCTIVO. Enséñaselo a la persona y, solo si lo "
                f"aprueba, usa aplicar_plan_destructivo('{plan_id}')."
            )
        for paso in plan.pasos:
            if paso.tipo is TipoPaso.SQL:
                self.validador.exigir(paso.sql, Modo.ESCRITURA)
        return plan

    def aplicar(self, plan_id: str, permitir_destructivo: bool = False) -> dict[str, Any]:
        plan = self.preparar(plan_id, permitir_destructivo)
        inicio = time.monotonic()
        verificaciones: list[dict[str, Any]] = []
        try:
            with self.gestor.escritura() as conn:
                for paso in plan.pasos:
                    if paso.tipo is TipoPaso.SQL:
                        conn.execute(paso.sql)  # type: ignore[arg-type]  # validado arriba
                    else:
                        verificaciones.append(self._cargar(conn, paso))
            # Si se llega aquí, la transacción se ha confirmado (COMMIT).
        except Exception:
            self.almacen.marcar(plan.id, EstadoPlan.FALLIDO)
            raise
        self.almacen.marcar(plan.id, EstadoPlan.APLICADO)
        return {
            "plan_id": plan.id,
            "estado": "aplicado",
            "resumen": plan.resumen,
            "sql": plan.sql_completo(),
            "destructivo": plan.destructivo,
            "verificaciones": verificaciones,
            "duracion_ms": int((time.monotonic() - inicio) * 1000),
        }

    def _cargar(self, conn: psycopg.Connection, paso: Paso) -> dict[str, Any]:
        """Ejecuta un paso de carga con COPY y verifica el recuento en la transacción."""
        if paso.tabla is None or paso.fichero is None or not paso.columnas:
            raise ErrorVerificacion("Paso de carga incompleto.")
        fichero: FicheroTabular | OrigenTransformado = abrir(self.cfg.dir_datos, paso.fichero)
        if paso.huella_fichero and huella(fichero.ruta) != paso.huella_fichero:
            raise ErrorVerificacion(
                f"El fichero {fichero.ruta.name} ha cambiado desde que se planificó la carga."
            )
        if paso.transformacion is not None:
            # La misma transformación que se revisó en el plan (forma parte de su huella).
            fichero = OrigenTransformado(fichero, paso.transformacion)
        tabla = validar_identificador(paso.tabla, "tabla")
        destinos = [validar_identificador(d, "columna") for _, d in paso.columnas]
        origenes = [o for o, _ in paso.columnas]

        antes = catalogo.contar(conn, self.cfg.esquema, tabla)
        sentencia = sql.SQL("COPY {} ({}) FROM STDIN").format(
            catalogo.ident(self.cfg.esquema, tabla),
            sql.SQL(", ").join(sql.Identifier(d) for d in destinos),
        )
        with conn.cursor() as cursor, cursor.copy(sentencia) as copia:
            for fila in fichero.filas():
                copia.write_row([fila.get(o) for o in origenes])
        despues = catalogo.contar(conn, self.cfg.esquema, tabla)

        insertadas = despues - antes
        if paso.filas_esperadas is not None and insertadas != paso.filas_esperadas:
            raise ErrorVerificacion(
                f"Se insertaron {insertadas} filas y se esperaban {paso.filas_esperadas}; "
                "se deshace la carga."
            )
        return {
            "comprobacion": f"recuento de la carga en {tabla}",
            "ok": True,
            "filas_antes": antes,
            "filas_insertadas": insertadas,
            "filas_esperadas": paso.filas_esperadas,
        }
