"""Servidor MCP de DataOps Agent.

Capa fina: cada herramienta valida la entrada, delega en el módulo correspondiente,
audita la llamada y devuelve un diccionario JSON. Toda la lógica está en módulos
probados por separado (planificador, aplicador, validaciones, bi...).

Herramientas (agrupadas por riesgo):

* Lectura: listar_ficheros, perfilar_fichero, listar_esquema, consultar_solo_lectura,
  comprobar_relacion, validar_carga, ver_auditoria, listar_planes_pendientes.
* Planificación (no modifican nada): planificar_tabla, planificar_carga,
  planificar_carga_transformada, planificar_clave, planificar_relacion,
  planificar_vista, planificar_borrado, descartar_plan.
* Escritura: aplicar_plan (solo planes NO destructivos) y aplicar_plan_destructivo
  (Claude Code pide siempre aprobación humana por la regla "ask" de
  .claude/settings.json).
* BI: estado_bi, sincronizar_bi, crear_pregunta, actualizar_pregunta,
  crear_dashboard, anadir_a_dashboard.

Arranque: ``dataops-mcp`` (o ``python -m dataops_agent.servidor``), transporte stdio.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from dataops_agent import catalogo, validaciones
from dataops_agent.aplicador import Aplicador, RequiereConfirmacion
from dataops_agent.auditoria import EventoAuditoria, RegistroAuditoria, Resultado
from dataops_agent.bi import BIAdapter, ErrorBI, MetabaseAdapter, Pregunta
from dataops_agent.conexion import GestorConexiones
from dataops_agent.config import Config, cargar_dotenv, validar_identificador
from dataops_agent.ficheros import EXTENSIONES, ErrorFichero, abrir
from dataops_agent.perfilado import perfilar
from dataops_agent.planes import AlmacenPlanes, ErrorPlan, TipoPaso
from dataops_agent.planificador import ColumnaDef, ErrorPlanificacion, Planificador
from dataops_agent.seguridad import Modo, SQLRechazado, ValidadorSQL
from dataops_agent.transformaciones import OrigenTransformado, Transformacion

INSTRUCCIONES = """\
Servidor de operaciones de datos seguras sobre PostgreSQL y Metabase.
Reglas: (1) nunca escribas sin un plan: usa planificar_* y enseña el plan (SQL, impacto
y bloqueos) antes de aplicar_plan; (2) si un plan tiene bloqueos, no insistas: explica
la evidencia y pregunta; (3) las operaciones destructivas solo con
aplicar_plan_destructivo y con aprobación expresa de la persona; (4) valida cada carga
con validar_carga; (5) no inventes relaciones: compruébalas con comprobar_relacion.
"""

_LECTURA = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_PLANIFICA = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_ESCRIBE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
_DESTRUYE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
_BI = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

# Errores esperables que se devuelven al agente como respuesta (no como fallo del servidor).
_ERRORES_CONTROLADOS = (
    SQLRechazado, ErrorPlan, ErrorPlanificacion, ErrorFichero, RequiereConfirmacion,
    ErrorBI, ValueError, KeyError, psycopg.Error, httpx.HTTPError,
)


def a_json(valor: Any) -> Any:
    """Convierte tipos de PostgreSQL a tipos JSON."""
    if isinstance(valor, Decimal):
        # float basta para mostrar resultados; los importes enormes se dejan en texto.
        return float(valor) if abs(valor) < Decimal("1e15") else str(valor)
    if isinstance(valor, datetime | date):
        return valor.isoformat()
    if isinstance(valor, dict):
        return {k: a_json(v) for k, v in valor.items()}
    if isinstance(valor, list | tuple):
        return [a_json(v) for v in valor]
    if isinstance(valor, bytes | memoryview):
        return "<binario>"
    return valor


class Servicio:
    """Agrupa las dependencias del servidor y la lógica de auditoría de cada llamada."""

    def __init__(self, cfg: Config, bi: BIAdapter | None = None) -> None:
        self.cfg = cfg
        self.gestor = GestorConexiones(cfg)
        self.validador = ValidadorSQL(cfg.esquema)
        self.almacen = AlmacenPlanes(timedelta(minutes=cfg.caducidad_plan_min))
        self.planificador = Planificador(cfg, self.gestor, self.validador, self.almacen)
        self.aplicador = Aplicador(cfg, self.gestor, self.validador, self.almacen)
        self.auditoria = RegistroAuditoria(
            cfg.dir_auditoria / "auditoria.jsonl", cfg.actor, cfg.esquema,
            escritor_bd=self.gestor.escribir_auditoria,
        )
        self.bi = bi

    def auditado(
        self,
        herramienta: str,
        operacion: str,
        accion: Callable[[], dict[str, Any]],
        *,
        sql: str | None = None,
        rol: str = "lector",
        destructiva: bool = False,
        detalle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ejecuta ``accion``, registra el evento y devuelve {"ok": ..., ...}."""
        inicio = time.monotonic()
        evento = EventoAuditoria(
            herramienta=herramienta, operacion=operacion, resultado=Resultado.OK, sql=sql,
            destructiva=destructiva, detalle=dict(detalle or {}),
            rol_bd=self.cfg.lector_usuario if rol == "lector" else self.cfg.escritor_usuario,
        )
        try:
            respuesta = {"ok": True, **accion()}
            evento.plan_id = respuesta.get("plan_id")
            evento.sql = evento.sql or respuesta.get("sql")
            evento.destructiva = destructiva or bool(respuesta.get("destructivo"))
            if respuesta.get("bloqueos"):
                evento.detalle["bloqueos"] = respuesta["bloqueos"]
        except RequiereConfirmacion as error:
            evento.resultado = Resultado.PENDIENTE_CONFIRMACION
            evento.destructiva = True
            respuesta = {"ok": False, "requiere_aprobacion": True, "error": str(error)}
        except SQLRechazado as error:
            evento.resultado = Resultado.RECHAZADO
            evento.detalle["errores"] = list(error.errores)
            respuesta = {"ok": False, "rechazado": True, "errores": list(error.errores)}
        except _ERRORES_CONTROLADOS as error:
            evento.resultado = Resultado.ERROR
            evento.detalle["error"] = str(error)
            respuesta = {"ok": False, "error": _primera_linea(str(error))}
        except Exception as error:  # último recurso: nada sale sin auditar
            evento.resultado = Resultado.ERROR
            evento.detalle["error_inesperado"] = f"{type(error).__name__}: {error}"
            respuesta = {"ok": False, "error": f"Error inesperado ({type(error).__name__}): "
                                               f"{_primera_linea(str(error))}"}
        evento.duracion_ms = int((time.monotonic() - inicio) * 1000)
        self.auditoria.registrar(evento)
        return a_json(respuesta)


def crear_servidor(cfg: Config, bi: BIAdapter | None = None) -> MCPServer:
    """Construye el servidor MCP con todas sus herramientas."""
    s = Servicio(cfg, bi)
    servidor = MCPServer(name="dataops", instructions=INSTRUCCIONES, version="0.1.0")

    # ============================================================ LECTURA
    @servidor.tool(annotations=_LECTURA)
    def listar_ficheros() -> dict[str, Any]:
        """Lista los ficheros CSV/JSON disponibles en el directorio de datos."""

        def accion() -> dict[str, Any]:
            base = cfg.dir_datos
            ficheros = sorted(
                str(p.relative_to(base)) for p in base.rglob("*")
                if p.is_file() and p.suffix.lower() in EXTENSIONES
            )
            return {"directorio": str(base), "ficheros": ficheros}

        return s.auditado("listar_ficheros", "LISTAR", accion)

    @servidor.tool(annotations=_LECTURA)
    def perfilar_fichero(ruta: str) -> dict[str, Any]:
        """Perfila un CSV/JSON: tipos inferidos, nulos, cardinalidad, candidatas a clave
        y advertencias. `ruta` es relativa al directorio de datos."""
        return s.auditado(
            "perfilar_fichero", "PERFILAR",
            lambda: perfilar(abrir(cfg.dir_datos, ruta)).como_dict(),
            detalle={"ruta": ruta},
        )

    @servidor.tool(annotations=_LECTURA)
    def listar_esquema() -> dict[str, Any]:
        """Tablas y vistas del esquema del proyecto con columnas, tipos, filas, clave
        primaria y claves foráneas."""

        def accion() -> dict[str, Any]:
            with s.gestor.lectura() as conn:
                return catalogo.describir_esquema(conn, cfg.esquema)

        return s.auditado("listar_esquema", "LISTAR", accion)

    @servidor.tool(annotations=_LECTURA)
    def consultar_solo_lectura(sql: str, max_filas: int | None = None) -> dict[str, Any]:
        """Ejecuta UNA consulta SELECT sobre el esquema del proyecto con rol de solo
        lectura, timeout y límite de filas. Las tablas sin esquema se resuelven en el
        del proyecto."""
        limite = max(1, min(max_filas or cfg.max_filas, 1000))

        def accion() -> dict[str, Any]:
            s.validador.exigir(sql, Modo.LECTURA)
            with s.gestor.lectura() as conn, conn.cursor(name="consulta_agente") as cursor:
                cursor.execute(sql)  # type: ignore[arg-type]  # validado arriba
                filas = cursor.fetchmany(limite + 1)
                columnas = [d.name for d in cursor.description or []]
            return {
                "columnas": columnas,
                "filas": [list(f) for f in filas[:limite]],
                "n_filas": min(len(filas), limite),
                "truncado": len(filas) > limite,
            }

        return s.auditado("consultar_solo_lectura", "SELECT", accion, sql=sql)

    @servidor.tool(annotations=_LECTURA)
    def comprobar_relacion(
        tabla_hija: str, columna_hija: str, tabla_padre: str, columna_padre: str
    ) -> dict[str, Any]:
        """Evidencia de una posible relación SIN crearla: cobertura (% de valores de la
        hija que existen en el padre), nº de huérfanos y muestra."""

        def accion() -> dict[str, Any]:
            for v in (tabla_hija, columna_hija, tabla_padre, columna_padre):
                validar_identificador(v)
            with s.gestor.lectura() as conn:
                return validaciones.huerfanos(conn, cfg.esquema, tabla_hija, columna_hija,
                                 tabla_padre, columna_padre)

        return s.auditado("comprobar_relacion", "COMPROBAR", accion,
                          detalle={"hija": f"{tabla_hija}.{columna_hija}",
                                   "padre": f"{tabla_padre}.{columna_padre}"})

    @servidor.tool(annotations=_LECTURA)
    def validar_carga(
        tabla: str,
        ficheros: list[str],
        clave: list[str] | None = None,
        columnas_suma: list[str] | None = None,
        relaciones: list[list[str]] | None = None,
        plan_carga: str | None = None,
    ) -> dict[str, Any]:
        """Validación determinista tras una carga: recuento frente a los ficheros de
        origen, sumas de control de `columnas_suma`, unicidad de `clave` y huérfanos de
        `relaciones` ([[columna_hija, tabla_padre, columna_padre], ...]).
        Si la carga se hizo con planificar_carga_transformada, pasa su `plan_carga`: los
        ficheros de ese plan se comparan ya transformados (como se cargaron)."""

        def accion() -> dict[str, Any]:
            validar_identificador(tabla, "tabla")
            origenes: list[Any] = [abrir(cfg.dir_datos, f) for f in ficheros]
            if plan_carga:
                transformados = {
                    Path(p.fichero).resolve(): p.transformacion
                    for p in s.almacen.obtener(plan_carga).pasos
                    if p.tipo is TipoPaso.CARGA and p.fichero and p.transformacion
                }
                origenes = [
                    OrigenTransformado(o, transformados[o.ruta.resolve()])
                    if o.ruta.resolve() in transformados
                    else o
                    for o in origenes
                ]
            with s.gestor.lectura() as conn:
                comprobaciones = validaciones.validar_carga(
                    conn, cfg.esquema, tabla, origenes, clave, columnas_suma
                )
                for ch, padre, cp in relaciones or []:
                    for v in (ch, padre, cp):
                        validar_identificador(v)
                    comprobaciones.append(
                        validaciones.comprobar_huerfanos(conn, cfg.esquema, tabla, ch, padre, cp)
                    )
            todas_ok = all(c.ok for c in comprobaciones)
            return {"valido": todas_ok, "comprobaciones": [c.como_dict() for c in comprobaciones]}

        return s.auditado(
            "validar_carga", "VALIDAR", accion,
            detalle={"tabla": tabla, **({"plan_carga": plan_carga} if plan_carga else {})},
        )

    @servidor.tool(annotations=_LECTURA)
    def ver_auditoria(ultimos: int = 20) -> dict[str, Any]:
        """Últimos eventos del registro de auditoría (quién, qué, cuándo, resultado)."""
        eventos = s.auditoria.leer_jsonl()[-max(1, min(ultimos, 200)):]
        campos = ("instante", "actor", "herramienta", "operacion", "plan_id", "resultado",
                  "destructiva", "sql")
        return {"ok": True, "eventos": [{k: e.get(k) for k in campos} for e in eventos]}

    @servidor.tool(annotations=_LECTURA)
    def listar_planes_pendientes() -> dict[str, Any]:
        """Planes creados en esta sesión que aún no se han aplicado."""
        return {"ok": True, "planes": [p.como_dict() for p in s.almacen.pendientes()]}

    @servidor.tool(annotations=_PLANIFICA)
    def descartar_plan(plan_id: str, motivo: str = "") -> dict[str, Any]:
        """Retira un plan pendiente que ya no se va a usar (p. ej. sustituido por otro).
        No toca la base de datos; el plan deja de poder aplicarse."""
        return s.auditado(
            "descartar_plan", "DESCARTAR PLAN",
            lambda: {"plan_id": s.almacen.descartar(plan_id).id, "estado": "descartado"},
            detalle={"motivo": motivo} if motivo else None,
        )

    # ======================================================== PLANIFICACIÓN
    @servidor.tool(annotations=_PLANIFICA)
    def planificar_tabla(
        nombre: str,
        columnas: list[ColumnaDef] | None = None,
        clave_primaria: list[str] | None = None,
        desde_fichero: str | None = None,
    ) -> dict[str, Any]:
        """Plan para crear una tabla. Da `columnas` explícitas o `desde_fichero` (infiere
        los tipos del perfil). No modifica nada: devuelve plan_id, SQL e impacto."""
        return s.auditado(
            "planificar_tabla", "PLAN CREATE TABLE",
            lambda: s.planificador.tabla(
                nombre, columnas, clave_primaria, desde_fichero
            ).como_dict(),
        )

    @servidor.tool(annotations=_PLANIFICA)
    def planificar_carga(tabla: str, fichero: str, modo: str = "anadir") -> dict[str, Any]:
        """Plan para cargar un fichero en una tabla. modo='anadir' o 'reemplazar'
        (vacía la tabla: destructivo). Detecta columnas renombradas, tipos inválidos y
        claves repetidas antes de cargar."""
        return s.auditado(
            "planificar_carga", "PLAN CARGA",
            lambda: s.planificador.carga(tabla, fichero, modo).como_dict(),
        )

    @servidor.tool(annotations=_PLANIFICA)
    def planificar_carga_transformada(
        tabla: str,
        fichero: str,
        renombrar: dict[str, str] | None = None,
        dividir: dict[str, int] | None = None,
        quitar_duplicados_exactos: bool = False,
        modo: str = "anadir",
    ) -> dict[str, Any]:
        """Plan para cargar una entrega defectuosa REPARÁNDOLA al leerla, sin modificar el
        fichero: `renombrar` {columna_fichero: nombre_esperado}, `dividir`
        {columna_fichero: 10|100|1000} (cambio de unidad, p. ej. céntimos → euros) y
        `quitar_duplicados_exactos` (filas idénticas en todas las columnas). El plan
        muestra el efecto cuantificado de cada transformación y pasa por las mismas
        comprobaciones que planificar_carga. Úsalo solo con la conformidad de la persona."""

        def accion() -> dict[str, Any]:
            transformacion = Transformacion.crear(renombrar, dividir, quitar_duplicados_exactos)
            return s.planificador.carga(tabla, fichero, modo, transformacion).como_dict()

        return s.auditado(
            "planificar_carga_transformada", "PLAN CARGA TRANSFORMADA", accion,
            detalle={
                "fichero": fichero,
                "renombrar": renombrar or {},
                "dividir": dividir or {},
                "quitar_duplicados_exactos": quitar_duplicados_exactos,
            },
        )

    @servidor.tool(annotations=_PLANIFICA)
    def planificar_clave(tabla: str, columnas: list[str]) -> dict[str, Any]:
        """Plan para añadir la clave primaria; comprueba antes unicidad y nulos."""
        return s.auditado(
            "planificar_clave", "PLAN PRIMARY KEY",
            lambda: s.planificador.clave(tabla, columnas).como_dict(),
        )

    @servidor.tool(annotations=_PLANIFICA)
    def planificar_relacion(
        tabla_hija: str, columna_hija: str, tabla_padre: str, columna_padre: str
    ) -> dict[str, Any]:
        """Plan de clave foránea. Comprueba tipos, unicidad del padre e integridad
        referencial (huérfanos) ANTES de crearla; con huérfanos el plan queda bloqueado."""
        return s.auditado(
            "planificar_relacion", "PLAN FOREIGN KEY",
            lambda: s.planificador.relacion(
                tabla_hija, columna_hija, tabla_padre, columna_padre
            ).como_dict(),
        )

    @servidor.tool(annotations=_PLANIFICA)
    def planificar_vista(nombre: str, consulta: str, reemplazar: bool = False) -> dict[str, Any]:
        """Plan para crear o redefinir (reemplazar=true) una vista a partir de un SELECT.
        Prueba la consulta e informa de columnas, filas y objetos dependientes."""
        return s.auditado(
            "planificar_vista", "PLAN CREATE VIEW",
            lambda: s.planificador.vista(nombre, consulta, reemplazar).como_dict(),
            sql=consulta,
        )

    @servidor.tool(annotations=_PLANIFICA)
    def planificar_borrado(tipo: str, objeto: str, condicion: str | None = None) -> dict[str, Any]:
        """Plan DESTRUCTIVO: tipo='tabla' (DROP TABLE), 'vista' (DROP VIEW), 'vaciar'
        (TRUNCATE) o 'filas' (DELETE con `condicion` WHERE obligatoria). Sin CASCADE."""
        return s.auditado(
            "planificar_borrado", "PLAN DESTRUCTIVO",
            lambda: s.planificador.borrado(tipo, objeto, condicion).como_dict(),
            destructiva=True,
        )

    # ============================================================ ESCRITURA
    @servidor.tool(annotations=_ESCRIBE)
    def aplicar_plan(plan_id: str) -> dict[str, Any]:
        """Aplica un plan NO destructivo ya revisado (una transacción; se deshace entero
        si algo falla). Los planes destructivos se rechazan aquí."""
        return s.auditado(
            "aplicar_plan", "APLICAR",
            lambda: s.aplicador.aplicar(plan_id, permitir_destructivo=False),
            rol="escritor", detalle={"plan_id": plan_id},
        )

    @servidor.tool(annotations=_DESTRUYE)
    def aplicar_plan_destructivo(plan_id: str, motivo: str) -> dict[str, Any]:
        """Aplica un plan DESTRUCTIVO (DROP/TRUNCATE/DELETE). Úsala SOLO si la persona lo
        ha aprobado expresamente; Claude Code pedirá además su confirmación. Indica en
        `motivo` qué ha pedido la persona."""
        return s.auditado(
            "aplicar_plan_destructivo", "APLICAR DESTRUCTIVO",
            lambda: s.aplicador.aplicar(plan_id, permitir_destructivo=True),
            rol="escritor", destructiva=True, detalle={"plan_id": plan_id, "motivo": motivo},
        )

    # ================================================================== BI
    def exigir_bi() -> BIAdapter:
        if s.bi is None:
            raise ErrorBI("No hay herramienta de BI configurada.")
        return s.bi

    def sql_para_bi(sql: str) -> str:
        s.validador.exigir(sql, Modo.LECTURA)  # mismas barreras que consultar_solo_lectura
        return s.validador.calificar(sql)

    @servidor.tool(annotations=_LECTURA)
    def estado_bi() -> dict[str, Any]:
        """Comprueba si la herramienta de BI está disponible y conectada a la BD."""
        return s.auditado("estado_bi", "BI ESTADO", lambda: exigir_bi().estado())

    @servidor.tool(annotations=_BI)
    def sincronizar_bi() -> dict[str, Any]:
        """Pide a la herramienta de BI que relea el esquema (tras crear tablas o vistas)."""

        def accion() -> dict[str, Any]:
            exigir_bi().sincronizar()
            return {"mensaje": "Sincronización solicitada (tarda unos segundos)."}

        return s.auditado("sincronizar_bi", "BI SYNC", accion)

    @servidor.tool(annotations=_BI)
    def crear_pregunta(
        nombre: str, sql: str, visualizacion: str = "tabla", descripcion: str | None = None
    ) -> dict[str, Any]:
        """Crea una pregunta (consulta guardada) en la herramienta de BI. `sql` debe ser
        un SELECT válido del proyecto (idealmente sobre una vista). visualizacion: tabla,
        barras, barras_horizontales, lineas, area, tarta o numero."""

        def accion() -> dict[str, Any]:
            sql_final = sql_para_bi(sql)
            pregunta = Pregunta(nombre, sql_final, visualizacion, descripcion)
            ref = exigir_bi().crear_pregunta(pregunta)
            return {**ref.como_dict(), "sql": sql_final}

        return s.auditado("crear_pregunta", "BI PREGUNTA", accion, sql=sql)

    @servidor.tool(annotations=_BI)
    def actualizar_pregunta(
        pregunta_id: int, sql: str | None = None, nombre: str | None = None,
        visualizacion: str | None = None,
    ) -> dict[str, Any]:
        """Modifica una pregunta existente (SQL, nombre o visualización)."""

        def accion() -> dict[str, Any]:
            sql_final = sql_para_bi(sql) if sql is not None else None
            return exigir_bi().actualizar_pregunta(
                pregunta_id, sql_final, nombre, visualizacion
            ).como_dict()

        return s.auditado("actualizar_pregunta", "BI PREGUNTA", accion, sql=sql)

    @servidor.tool(annotations=_BI)
    def crear_dashboard(
        nombre: str, pregunta_ids: list[int], descripcion: str | None = None
    ) -> dict[str, Any]:
        """Crea un dashboard con las preguntas indicadas (dos por fila)."""
        return s.auditado(
            "crear_dashboard", "BI DASHBOARD",
            lambda: exigir_bi().crear_dashboard(nombre, pregunta_ids, descripcion).como_dict(),
        )

    @servidor.tool(annotations=_BI)
    def anadir_a_dashboard(dashboard_id: int, pregunta_ids: list[int]) -> dict[str, Any]:
        """Añade preguntas a un dashboard existente conservando las que ya tiene."""
        return s.auditado(
            "anadir_a_dashboard", "BI DASHBOARD",
            lambda: exigir_bi().anadir_a_dashboard(dashboard_id, pregunta_ids).como_dict(),
        )

    servidor.servicio = s  # type: ignore[attr-defined]  # acceso para tests
    return servidor


def _primera_linea(texto: str) -> str:
    texto = texto.strip()
    return texto.splitlines()[0] if texto else texto


def main() -> None:
    """Punto de entrada: carga .env del proyecto y sirve por stdio."""
    raiz = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    # DATAOPS_ENV_FILE permite indicar el .env cuando el proceso no arranca en el proyecto.
    cargar_dotenv(Path(os.environ.get("DATAOPS_ENV_FILE") or raiz / ".env"))
    os.chdir(raiz)  # rutas relativas (datos/, auditoria/) respecto al proyecto
    cfg = Config.desde_entorno()
    crear_servidor(cfg, MetabaseAdapter(cfg)).run()


if __name__ == "__main__":
    main()
