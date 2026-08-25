"""Validación estática de SQL con sqlglot antes de enviarlo a PostgreSQL.

Es la primera barrera de seguridad (la segunda son los privilegios de los roles y la
tercera el statement_timeout). Reglas:

1. Exactamente UNA sentencia: se rechazan cadenas como ``SELECT 1; DROP TABLE x``.
2. Lista de sentencias permitidas según el modo:
   - LECTURA: solo SELECT (incluye WITH y UNION), sin SELECT ... INTO ni FOR UPDATE.
   - ESCRITURA: además CREATE TABLE/VIEW, INSERT, ALTER TABLE, DROP TABLE/VIEW,
     TRUNCATE y DELETE. Todo lo demás (GRANT, COPY, DO, SET, VACUUM, UPDATE...) se
     rechaza. Lo que sqlglot no entiende llega como ``Command`` y también se rechaza.
3. No se puede salir del esquema del proyecto: toda tabla referenciada debe estar sin
   calificar (se resuelve con search_path = esquema del proyecto) o calificada con ese
   esquema. Se rechazan catálogos, otros esquemas y nombres ``pg_*`` (pg_catalog se
   consulta implícitamente aunque no esté en search_path).
4. Funciones peligrosas bloqueadas (lectura de ficheros, dblink, ``query_to_xml`` o
   ``ts_stat``, que ejecutan SQL arbitrario dentro de una cadena y saltarían este
   validador, etc.). El nombre se compara sin comillas ni mayúsculas.
5. Se marca como DESTRUCTIVA cualquier sentencia DROP, TRUNCATE, DELETE o ALTER ... DROP.

El validador es conservador a propósito: ante la duda, rechaza.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

# sqlglot avisa por logging cuando cae en ``Command``; en un servidor MCP por stdio no
# debe escribirse nada en stdout, y el aviso no aporta: el resultado ya se rechaza.
logging.getLogger("sqlglot").setLevel(logging.ERROR)


class Modo(StrEnum):
    """Modo de validación: qué familia de sentencias se admite."""

    LECTURA = "lectura"
    ESCRITURA = "escritura"


class SQLRechazado(ValueError):
    """El SQL no supera la validación. ``errores`` explica por qué."""

    def __init__(self, errores: tuple[str, ...]):
        super().__init__("; ".join(errores))
        self.errores = errores


@dataclass(frozen=True)
class ResultadoValidacion:
    """Resultado de validar una sentencia."""

    valido: bool
    tipo: str | None = None  # p. ej. "SELECT", "CREATE TABLE", "DROP VIEW"
    destructiva: bool = False
    tablas: tuple[str, ...] = ()
    errores: tuple[str, ...] = field(default_factory=tuple)


# Funciones que nunca se admiten (comparación en minúsculas).
_FUNCIONES_PROHIBIDAS = frozenset(
    {
        "set_config",
        "query_to_xml",
        "query_to_xml_and_xmlschema",
        "query_to_xmlschema",
        "cursor_to_xml",
        "table_to_xml",
        "table_to_xml_and_xmlschema",
        "schema_to_xml",
        "database_to_xml",
        "current_setting",
        # Como query_to_xml, reciben una consulta dentro de una cadena y la ejecutan:
        "ts_stat",
        "ts_rewrite",
    }
)
# Prefijos prohibidos: funciones de administración (pg_*), objetos grandes (lo_*)
# y acceso a otras bases de datos (dblink*).
_PREFIJOS_PROHIBIDOS = ("pg_", "lo_", "dblink")
# Excepciones inocuas de los prefijos anteriores.
_FUNCIONES_PERMITIDAS_PG = frozenset({"pg_typeof"})

# Tipos de nodo que modifican datos o esquema; sirven para detectar escrituras
# escondidas dentro de una lectura (p. ej. ``WITH d AS (DELETE ...) SELECT ...``).
_NODOS_ESCRITURA: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Copy,
    exp.Grant,
    exp.Command,
)


class ValidadorSQL:
    """Valida sentencias SQL para un esquema de proyecto concreto."""

    def __init__(self, esquema: str) -> None:
        self.esquema = esquema.lower()

    # ------------------------------------------------------------------ API pública
    def validar(self, sql: str, modo: Modo) -> ResultadoValidacion:
        """Valida ``sql`` y devuelve el resultado (nunca lanza excepción)."""
        try:
            sentencias = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
        except ParseError as error:
            return _rechazo(f"SQL no analizable: {_primera_linea(str(error))}")

        if len(sentencias) == 0:
            return _rechazo("No hay ninguna sentencia SQL.")
        if len(sentencias) > 1:
            return _rechazo(
                f"Solo se admite una sentencia por llamada (hay {len(sentencias)})."
            )

        arbol = sentencias[0]
        tipo, errores = self._comprobar_tipo(arbol, modo)
        errores += self._comprobar_escrituras_anidadas(arbol, modo)
        tablas, errores_tablas = self._comprobar_tablas(arbol)
        errores += errores_tablas
        errores += self._comprobar_funciones(arbol)

        if errores:
            return ResultadoValidacion(
                valido=False, tipo=tipo, tablas=tablas, errores=tuple(errores)
            )
        return ResultadoValidacion(
            valido=True, tipo=tipo, destructiva=_es_destructiva(arbol), tablas=tablas
        )

    def exigir(self, sql: str, modo: Modo) -> ResultadoValidacion:
        """Como :meth:`validar`, pero lanza :class:`SQLRechazado` si no es válido."""
        resultado = self.validar(sql, modo)
        if not resultado.valido:
            raise SQLRechazado(resultado.errores)
        return resultado

    def calificar(self, sql: str) -> str:
        """Devuelve el SELECT con las tablas sin esquema calificadas con el del proyecto.

        Se usa para las preguntas de BI: Metabase ejecuta el SQL con su propia conexión,
        que no tiene el search_path del servidor MCP.
        """
        arbol = sqlglot.parse_one(sql, read="postgres")
        nombres_cte = {cte.alias_or_name.lower() for cte in arbol.find_all(exp.CTE)}
        for tabla in arbol.find_all(exp.Table):
            if tabla.name and not tabla.db and tabla.name.lower() not in nombres_cte:
                tabla.set("db", exp.to_identifier(self.esquema))
        return arbol.sql(dialect="postgres", pretty=True)

    # ------------------------------------------------------------ reglas internas
    def _comprobar_tipo(
        self, arbol: exp.Expression, modo: Modo
    ) -> tuple[str | None, list[str]]:
        """Regla 2: lista de sentencias permitidas."""
        if isinstance(arbol, exp.Query):  # SELECT, UNION, INTERSECT, EXCEPT
            errores: list[str] = []
            if arbol.find(exp.Into):
                errores.append("SELECT ... INTO no está permitido (crea tablas).")
            if arbol.find(exp.Lock):
                errores.append("SELECT ... FOR UPDATE/SHARE no está permitido.")
            return "SELECT", errores

        if modo is Modo.LECTURA:
            return _nombre_tipo(arbol), [
                f"Solo se permiten consultas SELECT en modo lectura "
                f"(recibido: {_nombre_tipo(arbol)})."
            ]

        if isinstance(arbol, exp.Create):
            tipo_objeto = str(arbol.args.get("kind") or "").upper()
            if tipo_objeto in {"TABLE", "VIEW"}:
                return f"CREATE {tipo_objeto}", []
            return f"CREATE {tipo_objeto}", [f"CREATE {tipo_objeto} no está permitido."]
        if isinstance(arbol, exp.Drop):
            tipo_objeto = str(arbol.args.get("kind") or "").upper()
            if tipo_objeto in {"TABLE", "VIEW"}:
                return f"DROP {tipo_objeto}", []
            return f"DROP {tipo_objeto}", [f"DROP {tipo_objeto} no está permitido."]
        if isinstance(arbol, exp.Alter):
            if str(arbol.args.get("kind") or "").upper() != "TABLE":
                return "ALTER", ["Solo se permite ALTER TABLE."]
            return "ALTER TABLE", []
        if isinstance(arbol, exp.Insert):
            return "INSERT", []
        if isinstance(arbol, exp.TruncateTable):
            return "TRUNCATE", []
        if isinstance(arbol, exp.Delete):
            return "DELETE", []
        return _nombre_tipo(arbol), [f"Sentencia no permitida: {_nombre_tipo(arbol)}."]

    def _comprobar_escrituras_anidadas(self, arbol: exp.Expression, modo: Modo) -> list[str]:
        """Detecta sentencias de escritura dentro de otra (CTE con DELETE, etc.)."""
        errores = []
        for nodo in arbol.find_all(*_NODOS_ESCRITURA):
            if nodo is arbol:
                continue
            # Excepción legítima: la acción DROP COLUMN/CONSTRAINT dentro de ALTER TABLE.
            if isinstance(arbol, exp.Alter) and isinstance(nodo, exp.Drop):
                continue
            errores.append(
                f"Sentencia {_nombre_tipo(nodo)} anidada no permitida"
                + (" en modo lectura." if modo is Modo.LECTURA else ".")
            )
        return errores

    def _comprobar_tablas(self, arbol: exp.Expression) -> tuple[tuple[str, ...], list[str]]:
        """Regla 3: todas las tablas deben pertenecer al esquema del proyecto."""
        nombres_cte = {cte.alias_or_name.lower() for cte in arbol.find_all(exp.CTE)}
        tablas: list[str] = []
        errores: list[str] = []
        for tabla in arbol.find_all(exp.Table):
            nombre = tabla.name.lower()
            esquema = (tabla.db or "").lower()
            if not nombre:
                # p. ej. FROM generate_series(...): es una función, no una tabla.
                continue
            if tabla.catalog:
                errores.append(f"No se permite referenciar otra base de datos: {tabla.sql()}.")
            elif esquema and esquema != self.esquema:
                errores.append(
                    f"Acceso fuera del esquema del proyecto '{self.esquema}': {tabla.sql()}."
                )
            elif not esquema and nombre in nombres_cte:
                continue
            elif nombre.startswith("pg_"):
                errores.append(f"No se permite acceder al catálogo del sistema: {nombre}.")
            tablas.append(nombre)
        return tuple(sorted(set(tablas))), errores

    def _comprobar_funciones(self, arbol: exp.Expression) -> list[str]:
        """Regla 4: funciones prohibidas."""
        errores = []
        for funcion in arbol.find_all(exp.Func):
            nombre = _nombre_funcion(funcion)
            if nombre in _FUNCIONES_PERMITIDAS_PG:
                continue
            if nombre in _FUNCIONES_PROHIBIDAS or nombre.startswith(_PREFIJOS_PROHIBIDOS):
                errores.append(f"Función no permitida: {nombre}().")
        return errores


# ------------------------------------------------------------------ utilidades
def _rechazo(motivo: str) -> ResultadoValidacion:
    return ResultadoValidacion(valido=False, errores=(motivo,))


def _primera_linea(texto: str) -> str:
    return texto.strip().splitlines()[0] if texto.strip() else texto


def _nombre_tipo(nodo: exp.Expression) -> str:
    if isinstance(nodo, exp.Command):
        return str(nodo.this).upper()
    return type(nodo).__name__.upper()


def _nombre_funcion(funcion: exp.Func) -> str:
    """Nombre en minúsculas y SIN comillas: ``"pg_read_file"(...)`` cuenta como pg_read_file."""
    if isinstance(funcion, exp.Anonymous):
        return funcion.name.strip('"').lower()
    return funcion.sql_name().strip('"').lower()


def _es_destructiva(arbol: exp.Expression) -> bool:
    """Regla 5: DROP, TRUNCATE, DELETE o ALTER TABLE ... DROP."""
    if isinstance(arbol, exp.Drop | exp.TruncateTable | exp.Delete):
        return True
    return isinstance(arbol, exp.Alter) and arbol.find(exp.Drop) is not None
