"""Lectura segura de ficheros de entrada (CSV y JSON).

Seguridad: el servidor solo lee ficheros dentro de ``DATAOPS_DIR_DATOS``. Se resuelve
la ruta real (siguiendo enlaces simbólicos) y se rechaza cualquier ruta que salga del
directorio, como ``../../etc/passwd``.

Todas las funciones devuelven los valores como texto (o ``None`` si están vacíos):
la conversión de tipos la hace PostgreSQL en la carga (COPY en formato texto), y el
perfilado infiere los tipos a partir de ese texto.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXTENSIONES = {".csv": "csv", ".json": "json"}
TAMANO_MAXIMO = 200 * 1024 * 1024  # 200 MB: suficiente para el ámbito del prototipo


class ErrorFichero(ValueError):
    """El fichero no se puede usar (ruta, formato o contenido no válidos)."""


@dataclass(frozen=True)
class FicheroTabular:
    """Un fichero de entrada ya validado."""

    ruta: Path
    formato: str  # "csv" | "json"
    columnas: tuple[str, ...]  # nombres originales, en orden

    def filas(self) -> Iterator[dict[str, str | None]]:
        """Itera las filas como diccionarios {columna_original: texto o None}."""
        if self.formato == "csv":
            yield from _filas_csv(self.ruta)
        else:
            for objeto in _objetos_json(self.ruta):
                yield {c: _a_texto(objeto.get(c)) for c in self.columnas}


def huella(ruta: Path) -> str:
    """SHA-256 del contenido del fichero (detecta cambios entre planificar y aplicar)."""
    resumen = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            resumen.update(bloque)
    return resumen.hexdigest()


def resolver_ruta(dir_datos: Path, ruta: str) -> Path:
    """Convierte ``ruta`` (relativa a dir_datos o absoluta) en una ruta segura."""
    base = dir_datos.resolve()
    candidata = Path(ruta)
    if not candidata.is_absolute():
        candidata = base / candidata
    real = candidata.resolve()
    if not real.is_relative_to(base):
        raise ErrorFichero(f"Ruta fuera del directorio de datos permitido ({base}): {ruta}")
    if not real.is_file():
        raise ErrorFichero(f"No existe el fichero: {ruta}")
    if real.suffix.lower() not in EXTENSIONES:
        raise ErrorFichero(
            f"Formato no admitido: {real.suffix}. Usa {', '.join(sorted(EXTENSIONES))}."
        )
    if real.stat().st_size > TAMANO_MAXIMO:
        raise ErrorFichero("Fichero demasiado grande para el prototipo (máx. 200 MB).")
    return real


def abrir(dir_datos: Path, ruta: str) -> FicheroTabular:
    """Valida la ruta y detecta formato y columnas."""
    real = resolver_ruta(dir_datos, ruta)
    formato = EXTENSIONES[real.suffix.lower()]
    if formato == "csv":
        with real.open(encoding="utf-8-sig", newline="") as f:
            lector = csv.reader(f, dialect=_dialecto(real))
            cabecera = next(lector, None)
        if not cabecera:
            raise ErrorFichero("El CSV está vacío o no tiene cabecera.")
        columnas = tuple(c.strip() for c in cabecera)
    else:
        columnas_vistas: dict[str, None] = {}
        for objeto in _objetos_json(real):
            columnas_vistas.update(dict.fromkeys(objeto))
        columnas = tuple(columnas_vistas)
    if len(set(columnas)) != len(columnas):
        raise ErrorFichero("Hay columnas con el mismo nombre en la cabecera.")
    if not columnas or any(not c for c in columnas):
        raise ErrorFichero("Hay columnas sin nombre.")
    return FicheroTabular(real, formato, columnas)


def nombre_sql(nombre: str) -> str:
    """Normaliza un nombre de columna a identificador SQL (snake_case sin tildes).

    Ejemplos: "Precio Unitario (€)" -> "precio_unitario", "Año" -> "ano", "1er" -> "c_1er".
    """
    sin_tildes = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    limpio = re.sub(r"[^a-z0-9]+", "_", sin_tildes.lower()).strip("_")
    if not limpio:
        limpio = "columna"
    if limpio[0].isdigit():
        limpio = f"c_{limpio}"
    return limpio[:63]


# ----------------------------------------------------------------- internos
def _dialecto(ruta: Path) -> type[csv.Dialect] | csv.Dialect:
    """Detecta el separador (coma o punto y coma) mirando el inicio del fichero."""
    with ruta.open(encoding="utf-8-sig", newline="") as f:
        muestra = f.read(64 * 1024)
    try:
        return csv.Sniffer().sniff(muestra, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def _filas_csv(ruta: Path) -> Iterator[dict[str, str | None]]:
    with ruta.open(encoding="utf-8-sig", newline="") as f:
        lector = csv.reader(f, dialect=_dialecto(ruta))
        cabecera = [c.strip() for c in next(lector)]
        for numero, valores in enumerate(lector, start=2):
            if not valores:  # línea en blanco
                continue
            if len(valores) != len(cabecera):
                raise ErrorFichero(
                    f"Línea {numero}: {len(valores)} valores, se esperaban {len(cabecera)}."
                )
            yield {
                c: (v if v.strip() != "" else None)
                for c, v in zip(cabecera, valores, strict=True)
            }


def _objetos_json(ruta: Path) -> Iterator[dict[str, Any]]:
    with ruta.open(encoding="utf-8-sig") as f:
        datos = json.load(f)
    if isinstance(datos, dict):
        # Forma habitual de las API: {"datos": [...]} o {"items": [...]}: se toma la
        # primera lista de objetos que aparezca.
        listas = [v for v in datos.values() if isinstance(v, list)]
        if not listas:
            raise ErrorFichero("El JSON debe ser una lista de objetos.")
        datos = listas[0]
    if not isinstance(datos, list):
        raise ErrorFichero("El JSON debe ser una lista de objetos.")
    for numero, objeto in enumerate(datos, start=1):
        yield _exigir_objeto(objeto, numero)


def _exigir_objeto(valor: Any, numero: int) -> dict[str, Any]:
    if not isinstance(valor, dict):
        raise ErrorFichero(f"Elemento {numero}: se esperaba un objeto JSON.")
    return valor


def _a_texto(valor: Any) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, bool):
        return "true" if valor else "false"
    if isinstance(valor, dict | list):
        return json.dumps(valor, ensure_ascii=False)  # anidado: se guarda como texto JSON
    texto = str(valor)
    return texto if texto.strip() != "" else None
