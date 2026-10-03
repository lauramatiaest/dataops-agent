"""Validación determinista de datos (no depende del modelo de lenguaje).

Comprobaciones que el agente usa tras cada carga y antes de crear relaciones:

* recuento de filas en la tabla frente al fichero de origen;
* sumas de control de columnas numéricas frente al origen;
* unicidad (y ausencia de nulos) de las columnas clave;
* huérfanos: valores de la columna hija que no existen en la columna padre.

Todas devuelven :class:`Comprobacion`, con lo esperado y lo obtenido, para que el
resultado sea explicable y reproducible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg import sql

from dataops_agent.catalogo import ident
from dataops_agent.ficheros import FicheroTabular, nombre_sql


@dataclass(frozen=True)
class Comprobacion:
    nombre: str
    ok: bool
    esperado: Any
    obtenido: Any
    detalle: str = ""

    def como_dict(self) -> dict[str, Any]:
        datos = asdict(self)
        for clave in ("esperado", "obtenido"):
            if isinstance(datos[clave], Decimal):
                datos[clave] = str(datos[clave])
        return datos


# -------------------------------------------------------------- base de datos
def huerfanos(
    conn: psycopg.Connection,
    esquema: str,
    hija: str,
    columna_hija: str,
    padre: str,
    columna_padre: str,
    muestra: int = 5,
) -> dict[str, Any]:
    """Cuántas filas de ``hija`` apuntan a valores inexistentes en ``padre``.

    Devuelve también la cobertura (% de filas hijas con padre) y una muestra de
    valores huérfanos, que es la evidencia que el agente enseña a la persona.
    """
    consulta = sql.SQL(
        """
        SELECT count(*) FILTER (WHERE h.{ch} IS NOT NULL),
               count(*) FILTER (WHERE h.{ch} IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM {padre} p WHERE p.{cp} = h.{ch}))
        FROM {hija} h
        """
    ).format(
        ch=sql.Identifier(columna_hija),
        cp=sql.Identifier(columna_padre),
        hija=ident(esquema, hija),
        padre=ident(esquema, padre),
    )
    no_nulos, n_huerfanos = conn.execute(consulta).fetchone()  # type: ignore[misc]
    valores: list[str] = []
    if n_huerfanos:
        consulta_muestra = sql.SQL(
            """
            SELECT DISTINCT h.{ch}::text FROM {hija} h
            WHERE h.{ch} IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM {padre} p WHERE p.{cp} = h.{ch})
            ORDER BY 1 LIMIT {n}
            """
        ).format(
            ch=sql.Identifier(columna_hija),
            cp=sql.Identifier(columna_padre),
            hija=ident(esquema, hija),
            padre=ident(esquema, padre),
            n=sql.Literal(muestra),
        )
        valores = [f[0] for f in conn.execute(consulta_muestra).fetchall()]
    cobertura = 100.0 if no_nulos == 0 else round(100 * (no_nulos - n_huerfanos) / no_nulos, 2)
    return {
        "filas_hijas_con_valor": no_nulos,
        "huerfanos": n_huerfanos,
        "cobertura_pct": cobertura,
        "muestra_huerfanos": valores,
    }


def comprobar_huerfanos(
    conn: psycopg.Connection, esquema: str, hija: str, ch: str, padre: str, cp: str
) -> Comprobacion:
    datos = huerfanos(conn, esquema, hija, ch, padre, cp)
    return Comprobacion(
        nombre=f"huérfanos {hija}.{ch} → {padre}.{cp}",
        ok=datos["huerfanos"] == 0,
        esperado=0,
        obtenido=datos["huerfanos"],
        detalle=(
            f"cobertura {datos['cobertura_pct']}%; muestra: {datos['muestra_huerfanos']}"
            if datos["huerfanos"]
            else f"cobertura {datos['cobertura_pct']}%"
        ),
    )


def comprobar_unicidad(
    conn: psycopg.Connection, esquema: str, tabla: str, columnas: list[str]
) -> Comprobacion:
    """La combinación de ``columnas`` no se repite y no tiene nulos."""
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in columnas)
    alguna_nula = sql.SQL(" OR ").join(
        sql.SQL("{} IS NULL").format(sql.Identifier(c)) for c in columnas
    )
    duplicados = conn.execute(
        sql.SQL(
            "SELECT count(*) FROM (SELECT {c} FROM {t} GROUP BY {c} HAVING count(*) > 1) d"
        ).format(c=cols, t=ident(esquema, tabla))
    ).fetchone()[0]  # type: ignore[index]
    nulos = conn.execute(
        sql.SQL("SELECT count(*) FROM {t} WHERE {n}").format(t=ident(esquema, tabla), n=alguna_nula)
    ).fetchone()[0]  # type: ignore[index]
    return Comprobacion(
        nombre=f"unicidad de {tabla}({', '.join(columnas)})",
        ok=duplicados == 0 and nulos == 0,
        esperado="0 valores repetidos y 0 nulos",
        obtenido=f"{duplicados} valores repetidos y {nulos} nulos",
    )


# ------------------------------------------------------------------- ficheros
def resumen_fichero(
    fichero: FicheroTabular, columnas_suma: list[str]
) -> tuple[int, dict[str, Decimal], list[str]]:
    """Recuento de filas y sumas de control de un fichero de origen.

    ``columnas_suma`` usa nombres SQL normalizados. Devuelve también los avisos de
    valores no numéricos (que no se suman).
    """
    por_sql = {nombre_sql(c): c for c in fichero.columnas}
    sumas = {c: Decimal(0) for c in columnas_suma}
    avisos: list[str] = []
    n = 0
    for fila in fichero.filas():
        n += 1
        for col in columnas_suma:
            original = por_sql.get(col)
            valor = fila.get(original) if original else None
            if valor is None:
                continue
            try:
                sumas[col] += Decimal(valor)
            except InvalidOperation:
                if len(avisos) < 5:
                    avisos.append(f"Fila {n}: valor no numérico en {col}: {valor!r}")
    return n, sumas, avisos


def validar_carga(
    conn: psycopg.Connection,
    esquema: str,
    tabla: str,
    ficheros: list[FicheroTabular],
    clave: list[str] | None = None,
    columnas_suma: list[str] | None = None,
) -> list[Comprobacion]:
    """Compara la tabla con sus ficheros de origen (todas las filas de la tabla)."""
    columnas_suma = columnas_suma or []
    total_origen = 0
    sumas_origen = {c: Decimal(0) for c in columnas_suma}
    avisos: list[str] = []
    for fichero in ficheros:
        n, sumas, avisos_f = resumen_fichero(fichero, columnas_suma)
        total_origen += n
        avisos += avisos_f
        for c, v in sumas.items():
            sumas_origen[c] += v

    total_bd = conn.execute(
        sql.SQL("SELECT count(*) FROM {}").format(ident(esquema, tabla))
    ).fetchone()[0]  # type: ignore[index]
    resultado = [
        Comprobacion(
            nombre=f"recuento {tabla} frente al origen",
            ok=total_bd == total_origen,
            esperado=total_origen,
            obtenido=total_bd,
        )
    ]
    for columna in columnas_suma:
        suma_bd = conn.execute(
            sql.SQL("SELECT coalesce(sum({c}), 0)::numeric FROM {t}").format(
                c=sql.Identifier(columna), t=ident(esquema, tabla)
            )
        ).fetchone()[0]  # type: ignore[index]
        esperado = sumas_origen[columna]
        resultado.append(
            Comprobacion(
                nombre=f"suma de control {tabla}.{columna}",
                ok=_casi_iguales(Decimal(suma_bd), esperado),
                esperado=esperado,
                obtenido=Decimal(suma_bd),
                detalle="; ".join(avisos),
            )
        )
    if clave:
        resultado.append(comprobar_unicidad(conn, esquema, tabla, clave))
    return resultado


def _casi_iguales(a: Decimal, b: Decimal) -> bool:
    """Igualdad exacta para numeric; tolerancia relativa mínima por si hay float."""
    if a == b:
        return True
    escala = max(abs(a), abs(b), Decimal(1))
    return abs(a - b) / escala < Decimal("1e-9")
