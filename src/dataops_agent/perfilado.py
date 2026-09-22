"""Perfilado de ficheros: tipos inferidos, nulos, cardinalidad y candidatas a clave.

El perfil es la *evidencia* sobre la que el agente decide el modelo. Por eso es
determinista (mismo fichero → mismo perfil) y no adivina: si una columna mezcla
formatos, se infiere ``text`` y se avisa.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from dataops_agent.ficheros import FicheroTabular, nombre_sql

# Patrones de inferencia, de más a menos específico. Se usa punto decimal: un CSV con
# coma decimal (1.234,56) se infiere como text y se avisa, en vez de adivinar.
_ENTERO = re.compile(r"^-?(0|[1-9]\d*)$")
_DECIMAL = re.compile(r"^-?(0|[1-9]\d*)\.\d+$")
_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FECHA_HORA = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")
_FECHA_HORA_TZ = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$"
)
_BOOLEANOS = {"true", "false", "t", "f"}
_COMA_DECIMAL = re.compile(r"^-?\d{1,3}(\.\d{3})*,\d+$|^-?\d+,\d+$")
_LIMITE_INT32 = 2_147_483_647


@dataclass
class PerfilColumna:
    nombre: str  # tal cual viene en el fichero
    nombre_sql: str  # normalizado para usar como columna
    tipo_inferido: str  # integer, bigint, numeric, boolean, date, timestamp, timestamptz, text
    nulos: int
    pct_nulos: float
    distintos: int
    candidata_clave: bool
    ejemplos: list[str]
    minimo: str | None = None
    maximo: str | None = None
    advertencias: list[str] = field(default_factory=list)


@dataclass
class PerfilFichero:
    fichero: str
    formato: str
    filas: int
    filas_duplicadas: int
    columnas: list[PerfilColumna]
    candidatas_clave: list[str]
    advertencias: list[str]

    def como_dict(self) -> dict[str, Any]:
        return asdict(self)


def inferir_tipo(valores: list[str]) -> str:
    """Tipo PostgreSQL más específico compatible con TODOS los valores no nulos."""
    if not valores:
        return "text"
    # Códigos con ceros a la izquierda ("00123") son identificadores, no números.
    if any(len(v) > 1 and v.startswith("0") and v[1].isdigit() for v in valores):
        return "text"
    if all(_ENTERO.match(v) for v in valores):
        return "bigint" if any(abs(int(v)) > _LIMITE_INT32 for v in valores) else "integer"
    if all(_ENTERO.match(v) or _DECIMAL.match(v) for v in valores):
        return "numeric"
    if all(v.lower() in _BOOLEANOS for v in valores):
        return "boolean"
    if all(_FECHA.match(v) for v in valores):
        return "date"
    if all(_FECHA_HORA.match(v) for v in valores):
        return "timestamp"
    if all(_FECHA_HORA.match(v) or _FECHA_HORA_TZ.match(v) for v in valores):
        return "timestamptz"
    return "text"


def perfilar(fichero: FicheroTabular, max_ejemplos: int = 3) -> PerfilFichero:
    """Recorre el fichero una vez y calcula el perfil de cada columna."""
    valores: dict[str, list[str]] = {c: [] for c in fichero.columnas}
    nulos: Counter[str] = Counter()
    huellas_filas: Counter[tuple[str | None, ...]] = Counter()
    n_filas = 0

    for fila in fichero.filas():
        n_filas += 1
        huellas_filas[tuple(fila[c] for c in fichero.columnas)] += 1
        for columna in fichero.columnas:
            valor = fila[columna]
            if valor is None:
                nulos[columna] += 1
            else:
                valores[columna].append(valor.strip())

    advertencias: list[str] = []
    nombres_sql = [nombre_sql(c) for c in fichero.columnas]
    if len(set(nombres_sql)) != len(nombres_sql):
        advertencias.append(
            "Al normalizar los nombres de columna hay colisiones; renómbralas en origen."
        )
    duplicadas = sum(n - 1 for n in huellas_filas.values() if n > 1)
    if duplicadas:
        advertencias.append(f"Hay {duplicadas} filas exactamente duplicadas.")

    columnas = [
        _perfilar_columna(c, nombre_sql(c), valores[c], nulos[c], n_filas, max_ejemplos)
        for c in fichero.columnas
    ]
    candidatas = [c.nombre_sql for c in columnas if c.candidata_clave]
    return PerfilFichero(
        fichero=fichero.ruta.name,
        formato=fichero.formato,
        filas=n_filas,
        filas_duplicadas=duplicadas,
        columnas=columnas,
        candidatas_clave=candidatas,
        advertencias=advertencias,
    )


def _perfilar_columna(
    nombre: str, nombre_normalizado: str, valores: list[str], nulos: int, n_filas: int,
    max_ejemplos: int,
) -> PerfilColumna:
    tipo = inferir_tipo(valores)
    distintos = len(set(valores))
    advertencias: list[str] = []
    if nombre != nombre_normalizado:
        advertencias.append(f"Se cargará como '{nombre_normalizado}'.")
    if tipo == "text" and valores and all(_COMA_DECIMAL.match(v) for v in valores[:200]):
        advertencias.append("Parece numérico con coma decimal; se deja como text.")

    # Candidata a clave: sin nulos, todos distintos y de un tipo razonable para clave.
    candidata = (
        n_filas > 0
        and nulos == 0
        and distintos == n_filas
        and tipo in {"integer", "bigint", "text"}
    )
    minimo = maximo = None
    if valores and tipo in {"integer", "bigint", "numeric"}:
        numeros = [_a_decimal(v) for v in valores]
        minimo, maximo = str(min(numeros)), str(max(numeros))
    elif valores and tipo in {"date", "timestamp", "timestamptz"}:
        minimo, maximo = min(valores), max(valores)  # ISO 8601: el orden de texto vale

    ejemplos = list(dict.fromkeys(valores))[:max_ejemplos]
    return PerfilColumna(
        nombre=nombre,
        nombre_sql=nombre_normalizado,
        tipo_inferido=tipo,
        nulos=nulos,
        pct_nulos=round(100 * nulos / n_filas, 2) if n_filas else 0.0,
        distintos=distintos,
        candidata_clave=candidata,
        ejemplos=ejemplos,
        minimo=minimo,
        maximo=maximo,
        advertencias=advertencias,
    )


def _a_decimal(valor: str) -> Decimal:
    try:
        return Decimal(valor)
    except InvalidOperation:  # no debería ocurrir tras la inferencia
        return Decimal(0)
