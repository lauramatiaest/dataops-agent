"""Consultas de introspección sobre el esquema del proyecto.

Es código propio y de confianza (no SQL del agente), así que puede consultar
``pg_catalog``. Todas las consultas reciben el esquema y los nombres de objeto como
parámetros o como ``psycopg.sql.Identifier``: nunca se concatenan cadenas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql


@dataclass(frozen=True)
class Columna:
    nombre: str
    tipo: str  # tal como lo muestra PostgreSQL (format_type)
    nulable: bool


def ident(esquema: str, objeto: str) -> sql.Composed:
    """``"esquema"."objeto"`` con las comillas correctas."""
    return sql.SQL("{}.{}").format(sql.Identifier(esquema), sql.Identifier(objeto))


def _regclass(esquema: str, objeto: str) -> str:
    return f'"{esquema}"."{objeto}"'


def objetos(conn: psycopg.Connection, esquema: str) -> dict[str, str]:
    """{nombre: 'tabla' | 'vista'} de los objetos del esquema."""
    filas = conn.execute(
        """
        SELECT c.relname, CASE c.relkind WHEN 'v' THEN 'vista' ELSE 'tabla' END
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v')
        ORDER BY c.relname
        """,
        (esquema,),
    ).fetchall()
    return dict(filas)


def tipo_objeto(conn: psycopg.Connection, esquema: str, objeto: str) -> str | None:
    return objetos(conn, esquema).get(objeto)


def columnas(conn: psycopg.Connection, esquema: str, objeto: str) -> list[Columna]:
    filas = conn.execute(
        """
        SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull
        FROM pg_attribute a
        WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (_regclass(esquema, objeto),),
    ).fetchall()
    return [Columna(*fila) for fila in filas]


def conjuntos_unicos(conn: psycopg.Connection, esquema: str, tabla: str) -> list[list[str]]:
    """Columnas cubiertas por PRIMARY KEY o UNIQUE (cada restricción, una lista)."""
    filas = conn.execute(
        """
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM pg_constraint con
        CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        WHERE con.conrelid = to_regclass(%s) AND con.contype IN ('p', 'u')
        GROUP BY con.oid
        """,
        (_regclass(esquema, tabla),),
    ).fetchall()
    return [list(f[0]) for f in filas]


def clave_primaria(conn: psycopg.Connection, esquema: str, tabla: str) -> list[str]:
    fila = conn.execute(
        """
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM pg_constraint con
        CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        WHERE con.conrelid = to_regclass(%s) AND con.contype = 'p'
        """,
        (_regclass(esquema, tabla),),
    ).fetchone()
    return list(fila[0]) if fila and fila[0] else []


def claves_foraneas(conn: psycopg.Connection, esquema: str) -> list[dict[str, Any]]:
    filas = conn.execute(
        """
        SELECT con.conname, hija.relname, padre.relname,
               (SELECT array_agg(a.attname ORDER BY k.ord)
                  FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum),
               (SELECT array_agg(a.attname ORDER BY k.ord)
                  FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum)
        FROM pg_constraint con
        JOIN pg_class hija ON hija.oid = con.conrelid
        JOIN pg_class padre ON padre.oid = con.confrelid
        JOIN pg_namespace n ON n.oid = hija.relnamespace
        WHERE con.contype = 'f' AND n.nspname = %s
        ORDER BY con.conname
        """,
        (esquema,),
    ).fetchall()
    return [
        {
            "nombre": f[0],
            "tabla": f[1],
            "tabla_referenciada": f[2],
            "columnas": list(f[3]),
            "columnas_referenciadas": list(f[4]),
        }
        for f in filas
    ]


def dependientes(conn: psycopg.Connection, esquema: str, objeto: str) -> list[str]:
    """Vistas que usan el objeto y tablas con claves foráneas que lo referencian."""
    regclass = _regclass(esquema, objeto)
    vistas = conn.execute(
        """
        SELECT DISTINCT 'vista ' || v.relname
        FROM pg_depend d
        JOIN pg_rewrite r ON r.oid = d.objid
        JOIN pg_class v ON v.oid = r.ev_class
        WHERE d.refobjid = to_regclass(%s) AND v.oid <> d.refobjid
        """,
        (regclass,),
    ).fetchall()
    referencias = conn.execute(
        """
        SELECT DISTINCT 'clave foránea ' || con.conname || ' de ' || h.relname
        FROM pg_constraint con JOIN pg_class h ON h.oid = con.conrelid
        WHERE con.contype = 'f' AND con.confrelid = to_regclass(%s)
          AND con.conrelid <> con.confrelid
        """,
        (regclass,),
    ).fetchall()
    return sorted(f[0] for f in vistas + referencias)


def contar(conn: psycopg.Connection, esquema: str, objeto: str) -> int:
    fila = conn.execute(
        sql.SQL("SELECT count(*) FROM {}").format(ident(esquema, objeto))
    ).fetchone()
    return int(fila[0]) if fila else 0


def describir_esquema(conn: psycopg.Connection, esquema: str) -> dict[str, Any]:
    """Resumen completo del esquema para la herramienta ``listar_esquema``."""
    fks = claves_foraneas(conn, esquema)
    resultado = []
    for nombre, tipo in objetos(conn, esquema).items():
        entrada: dict[str, Any] = {
            "nombre": nombre,
            "tipo": tipo,
            "filas": contar(conn, esquema, nombre),
            "columnas": [
                {"nombre": c.nombre, "tipo": c.tipo, "nulable": c.nulable}
                for c in columnas(conn, esquema, nombre)
            ],
        }
        if tipo == "tabla":
            entrada["clave_primaria"] = clave_primaria(conn, esquema, nombre)
            entrada["claves_foraneas"] = [
                {k: v for k, v in fk.items() if k != "tabla"}
                for fk in fks
                if fk["tabla"] == nombre
            ]
        resultado.append(entrada)
    return {"esquema": esquema, "objetos": resultado}
