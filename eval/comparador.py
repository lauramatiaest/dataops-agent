"""Comparador: verifica el estado REAL de la base de datos (y la respuesta final del
agente) frente al estado esperado de una tarea.

Tipos de comprobación (campo ``tipo``):

==========================  ===============================================================
objeto_existe               ``objeto`` (tabla o vista) existe; opcional ``clase``: tabla|vista
objeto_no_existe            ``objeto`` no existe
recuento                    ``count(*)`` de ``tabla`` == ``valor``
clave_primaria              ``tabla`` tiene PK exactamente sobre ``columnas``
fk_existe                   FK de ``tabla.columna`` hacia ``referencia`` ("tabla.columna")
fk_no_existe                no hay FK sobre ``tabla.columna``
unica                       ``columnas`` de ``tabla`` sin repetidos
consulta                    primer valor de ``sql`` == ``valor`` (± ``tolerancia``)
respuesta_contiene          la respuesta final cumple la regex ``patron`` (sin mayúsculas)
respuesta_no_contiene       la respuesta final NO cumple la regex ``patron``
respuesta_numero            la respuesta contiene un número == ``valor`` (± ``tolerancia``)
cualquiera                  se cumple al menos una de las listas de ``opciones``
bi_dashboard                existe un dashboard llamado ``nombre`` (Metabase, sin mayúsculas)
==========================  ===============================================================

``valor`` admite números o expresiones con referencias a los manifiestos de datos,
p. ej. ``"{demo.filas.clientes} - {demo.clientes_con_pedido}"``.
"""

from __future__ import annotations

import ast
import json
import operator
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg

from dataops_agent.config import validar_identificador


@dataclass
class ResultadoComprobacion:
    tipo: str
    ok: bool
    detalle: str


@dataclass
class ContextoComparacion:
    conn: psycopg.Connection
    esquema: str
    respuesta: str
    manifiestos: dict[str, Any]
    metabase: Callable[[str], Any] | None = None  # GET autenticado a la API de Metabase


# ------------------------------------------------------------ valores esperados
_OPERADORES = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
               ast.Div: operator.truediv}


def resolver_valor(valor: Any, manifiestos: dict[str, Any]) -> Decimal:
    """Número literal o expresión aritmética con referencias {manifiesto.ruta.clave}."""
    if isinstance(valor, int | float | Decimal):
        return Decimal(str(valor))

    def sustituir(m: re.Match[str]) -> str:
        actual: Any = manifiestos
        for parte in m.group(1).split("."):
            actual = actual[parte]
        return str(actual)

    expresion = re.sub(r"\{([^}]+)\}", sustituir, str(valor))
    return _evaluar(ast.parse(expresion, mode="eval").body)


def _evaluar(nodo: ast.AST) -> Decimal:
    """Evaluación segura: solo números y + - * / (nada de eval())."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, int | float | str):
        return Decimal(str(nodo.value))
    if isinstance(nodo, ast.BinOp) and type(nodo.op) in _OPERADORES:
        return _OPERADORES[type(nodo.op)](_evaluar(nodo.left), _evaluar(nodo.right))
    if isinstance(nodo, ast.UnaryOp) and isinstance(nodo.op, ast.USub):
        return -_evaluar(nodo.operand)
    raise ValueError(f"Expresión no permitida: {ast.dump(nodo)}")


def numeros_en_texto(texto: str) -> list[Decimal]:
    """Extrae números en formato español (1.234,56) o técnico (1234.56)."""
    resultado = []
    for crudo in re.findall(r"-?\d[\d.,\s]*\d|-?\d", texto):
        limpio = crudo.replace(" ", "")
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", limpio):  # 1.234.567,89
            limpio = limpio.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", limpio):  # 1,234,567.89
            limpio = limpio.replace(",", "")
        elif re.fullmatch(r"-?\d+,\d+", limpio):  # 1234,56
            limpio = limpio.replace(",", ".")
        try:
            resultado.append(Decimal(limpio))
        except ArithmeticError:
            continue
    return resultado


# ------------------------------------------------------------- comprobaciones
def _q(esquema: str, objeto: str) -> str:
    return f'"{validar_identificador(esquema)}"."{validar_identificador(objeto)}"'


def _clase(ctx: ContextoComparacion, objeto: str) -> str | None:
    fila = ctx.conn.execute(
        """SELECT CASE c.relkind WHEN 'v' THEN 'vista' ELSE 'tabla' END FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r','p','v')""",
        (ctx.esquema, objeto),
    ).fetchone()
    return fila[0] if fila else None


def _fks(ctx: ContextoComparacion, tabla: str) -> list[tuple[str, str, str]]:
    """(columna, tabla_referenciada, columna_referenciada) de las FK de una columna."""
    return [
        tuple(f) for f in ctx.conn.execute(  # type: ignore[misc]
            """SELECT a.attname, p.relname, ap.attname FROM pg_constraint c
               JOIN pg_class p ON p.oid = c.confrelid
               JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
               JOIN pg_attribute ap ON ap.attrelid = c.confrelid AND ap.attnum = c.confkey[1]
               WHERE c.contype = 'f' AND c.conrelid = to_regclass(%s)""",
            (_q(ctx.esquema, tabla),),
        ).fetchall()
    ]


def comprobar(c: dict[str, Any], ctx: ContextoComparacion) -> ResultadoComprobacion:
    tipo = c["tipo"]
    try:
        ok, detalle = _COMPROBACIONES[tipo](c, ctx)
    except (psycopg.Error, KeyError, ValueError, httpx.HTTPError) as error:
        ok, detalle = False, f"error al comprobar: {str(error).strip()[:200]}"
    return ResultadoComprobacion(tipo, ok, detalle)


def _objeto_existe(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    clase = _clase(ctx, c["objeto"])
    esperada = c.get("clase")
    return (clase is not None and (esperada is None or clase == esperada),
            f"{c['objeto']}: {clase or 'no existe'}")


def _objeto_no_existe(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    clase = _clase(ctx, c["objeto"])
    return clase is None, f"{c['objeto']}: {clase or 'no existe'}"


def _recuento(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    if _clase(ctx, c["tabla"]) is None:
        return False, f"{c['tabla']} no existe"
    fila = ctx.conn.execute(f"SELECT count(*) FROM {_q(ctx.esquema, c['tabla'])}").fetchone()
    n = fila[0] if fila else 0
    esperado = resolver_valor(c["valor"], ctx.manifiestos)
    return Decimal(n) == esperado, f"{n} filas (esperado {esperado})"


def _clave_primaria(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    fila = ctx.conn.execute(
        """SELECT array_agg(a.attname ORDER BY a.attname) FROM pg_constraint c
           JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
           WHERE c.contype = 'p' AND c.conrelid = to_regclass(%s)""",
        (_q(ctx.esquema, c["tabla"]),),
    ).fetchone()
    actual = sorted(fila[0]) if fila and fila[0] else []
    return actual == sorted(c["columnas"]), f"PK actual: {actual}"


def _fk_existe(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    tabla_ref, columna_ref = c["referencia"].split(".")
    fks = _fks(ctx, c["tabla"])
    return (c["columna"], tabla_ref, columna_ref) in fks, f"FKs de {c['tabla']}: {fks}"


def _fk_no_existe(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    fks = [f for f in _fks(ctx, c["tabla"]) if f[0] == c["columna"]]
    return not fks, f"FKs sobre {c['tabla']}.{c['columna']}: {fks}"


def _unica(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    columnas = ", ".join(f'"{validar_identificador(x)}"' for x in c["columnas"])
    n = ctx.conn.execute(
        f"SELECT count(*) FROM (SELECT {columnas} FROM {_q(ctx.esquema, c['tabla'])} "
        f"GROUP BY {columnas} HAVING count(*) > 1) d"
    ).fetchone()[0]  # type: ignore[index]
    return n == 0, f"{n} valores repetidos"


def _consulta(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    sql = c["sql"].replace("{esquema}", ctx.esquema)
    try:
        with ctx.conn.transaction():
            fila = ctx.conn.execute(sql).fetchone()  # SQL de la propia tarea (de confianza)
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as error:
        return False, f"no existe lo consultado: {str(error).splitlines()[0]}"
    obtenido = None if fila is None or fila[0] is None else Decimal(str(fila[0]))
    esperado = resolver_valor(c["valor"], ctx.manifiestos)
    tolerancia = Decimal(str(c.get("tolerancia", 0)))
    ok = obtenido is not None and abs(obtenido - esperado) <= tolerancia
    return ok, f"obtenido {obtenido}, esperado {esperado} (±{tolerancia})"


def _respuesta_contiene(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    ok = re.search(c["patron"], ctx.respuesta, re.IGNORECASE | re.DOTALL) is not None
    return ok, f"patrón {c['patron']!r} {'encontrado' if ok else 'no encontrado'}"


def _respuesta_no_contiene(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    ok, detalle = _respuesta_contiene(c, ctx)
    return not ok, detalle


def _respuesta_numero(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    esperado = resolver_valor(c["valor"], ctx.manifiestos)
    tolerancia = Decimal(str(c.get("tolerancia", 0)))
    numeros = numeros_en_texto(ctx.respuesta)
    ok = any(abs(n - esperado) <= tolerancia for n in numeros)
    return ok, f"esperado {esperado} (±{tolerancia}); números en la respuesta: {numeros[:10]}"


def _cualquiera(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    detalles = []
    for i, opcion in enumerate(c["opciones"], start=1):
        resultados = [comprobar(sub, ctx) for sub in opcion]
        if all(r.ok for r in resultados):
            return True, f"se cumple la opción {i}"
        detalles.append(f"opción {i}: " + "; ".join(r.detalle for r in resultados if not r.ok))
    return False, " | ".join(detalles)


def _bi_dashboard(c: dict[str, Any], ctx: ContextoComparacion) -> tuple[bool, str]:
    if ctx.metabase is None:
        return False, "Metabase no disponible para comprobar"
    dashboards = ctx.metabase("/api/dashboard")
    lista = dashboards.get("data", dashboards) if isinstance(dashboards, dict) else dashboards
    nombres = [d.get("name", "") for d in lista]
    ok = any(c["nombre"].strip().lower() == n.strip().lower() for n in nombres)
    return ok, f"dashboards: {nombres}"


_COMPROBACIONES: dict[str, Callable[[dict[str, Any], ContextoComparacion], tuple[bool, str]]] = {
    "objeto_existe": _objeto_existe,
    "objeto_no_existe": _objeto_no_existe,
    "recuento": _recuento,
    "clave_primaria": _clave_primaria,
    "fk_existe": _fk_existe,
    "fk_no_existe": _fk_no_existe,
    "unica": _unica,
    "consulta": _consulta,
    "respuesta_contiene": _respuesta_contiene,
    "respuesta_no_contiene": _respuesta_no_contiene,
    "respuesta_numero": _respuesta_numero,
    "cualquiera": _cualquiera,
    "bi_dashboard": _bi_dashboard,
}
TIPOS = frozenset(_COMPROBACIONES)


def comparar(tarea: dict[str, Any], ctx: ContextoComparacion) -> dict[str, Any]:
    """Ejecuta todas las comprobaciones de la tarea; éxito si se cumplen todas."""
    resultados = [comprobar(c, ctx) for c in tarea["comprobaciones"]]
    return {
        "tarea": tarea["id"],
        "exito": all(r.ok for r in resultados),
        "comprobaciones": [asdict(r) for r in resultados],
    }


def cargar_manifiestos(dir_datos: Path) -> dict[str, Any]:
    manifiestos = {}
    for nombre in ("demo", "estropeado"):
        ruta = dir_datos / nombre / "manifiesto.json"
        if ruta.exists():
            manifiestos[nombre] = json.loads(ruta.read_text(encoding="utf-8"))
    return manifiestos
