"""Transformaciones declarativas y acotadas para reparar una entrega al cargarla.

Principio: **el fichero entregado no se modifica nunca**. La transformación se declara
en el plan de carga (forma parte de su huella SHA-256 y de la auditoría) y se aplica
igual en los tres momentos del ciclo:

* al planificar, para calcular el impacto y detectar bloqueos sobre los datos ya
  transformados;
* al aplicar, mientras se envían las filas con ``COPY``;
* al validar, para comparar la tabla con lo que el origen *debería* haber sido.

Solo se admiten tres operaciones, las que cubren los defectos habituales de una
entrega (y los inyectados en ``datos/estropeado``):

* ``renombrar``: columna del fichero → nombre esperado (p. ej. ``precio`` →
  ``precio_unitario``).
* ``dividir``: cambio de unidad por una potencia de 10 (p. ej. céntimos → euros).
* ``deduplicar``: quitar filas **exactamente** iguales (todas las columnas).

No hay expresiones libres a propósito: cualquier otra corrección se pide al origen o
se resuelve con una vista de contención.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from dataops_agent.config import validar_identificador
from dataops_agent.ficheros import FicheroTabular

DIVISORES_PERMITIDOS = (10, 100, 1000)


class ErrorTransformacion(ValueError):
    """La transformación pedida no es válida para este fichero."""


@dataclass(frozen=True)
class Transformacion:
    """Transformación declarativa e inmutable (forma parte de la huella del plan)."""

    renombrar: tuple[tuple[str, str], ...] = ()  # (columna_en_fichero, nombre_nuevo)
    dividir: tuple[tuple[str, int], ...] = ()  # (columna_en_fichero, divisor)
    deduplicar: bool = False

    @classmethod
    def crear(
        cls,
        renombrar: Mapping[str, str] | None = None,
        dividir: Mapping[str, int] | None = None,
        deduplicar: bool = False,
    ) -> Transformacion:
        for nuevo in (renombrar or {}).values():
            validar_identificador(nuevo, "columna")
        for columna, divisor in (dividir or {}).items():
            if divisor not in DIVISORES_PERMITIDOS:
                raise ErrorTransformacion(
                    f"Divisor no permitido para {columna}: {divisor}. "
                    f"Usa uno de {list(DIVISORES_PERMITIDOS)}."
                )
        t = cls(
            renombrar=tuple(sorted((renombrar or {}).items())),
            dividir=tuple(sorted((dividir or {}).items())),
            deduplicar=deduplicar,
        )
        if t.vacia:
            raise ErrorTransformacion(
                "No se ha pedido ninguna transformación: usa planificar_carga."
            )
        return t

    @property
    def vacia(self) -> bool:
        return not (self.renombrar or self.dividir or self.deduplicar)

    def comprobar(self, fichero: FicheroTabular) -> None:
        """Las columnas citadas deben existir en el fichero; los nombres nuevos, no chocar."""
        columnas = set(fichero.columnas)
        citadas = [c for c, _ in self.renombrar] + [c for c, _ in self.dividir]
        faltan = sorted(c for c in citadas if c not in columnas)
        if faltan:
            raise ErrorTransformacion(
                f"Columnas inexistentes en {fichero.ruta.name}: {faltan}. "
                f"Columnas del fichero: {list(fichero.columnas)}."
            )
        nuevos = [n for _, n in self.renombrar]
        renombradas = {c for c, _ in self.renombrar}
        choques = sorted(n for n in nuevos if n in columnas and n not in renombradas)
        if choques or len(set(nuevos)) != len(nuevos):
            raise ErrorTransformacion(
                f"El renombrado produciría columnas repetidas: {choques or nuevos}."
            )


class OrigenTransformado:
    """Vista transformada de un :class:`FicheroTabular`, con su misma interfaz
    (``ruta``, ``formato``, ``columnas`` y ``filas()``). El fichero no se toca."""

    def __init__(self, fichero: FicheroTabular, transformacion: Transformacion) -> None:
        transformacion.comprobar(fichero)
        self.fichero = fichero
        self.transformacion = transformacion
        self.ruta = fichero.ruta
        self.formato = fichero.formato
        self._nombre = dict(transformacion.renombrar)
        self._divisor = dict(transformacion.dividir)
        self.columnas = tuple(self._nombre.get(c, c) for c in fichero.columnas)

    def filas(self) -> Iterator[dict[str, str | None]]:
        vistas: set[tuple[str | None, ...]] = set()
        for fila in self.fichero.filas():
            if self.transformacion.deduplicar:
                firma = tuple(fila.get(c) for c in self.fichero.columnas)
                if firma in vistas:
                    continue
                vistas.add(firma)
            yield {
                self._nombre.get(c, c): self._valor(c, fila.get(c))
                for c in self.fichero.columnas
            }

    def _valor(self, columna: str, valor: str | None) -> str | None:
        divisor = self._divisor.get(columna)
        if divisor is None or valor is None:
            return valor
        try:
            return format(Decimal(valor) / divisor, "f")
        except InvalidOperation:
            return valor  # no numérico: lo detectará la comprobación de tipos

    def efecto(self) -> dict[str, Any]:
        """Efecto cuantificado de la transformación, para enseñarlo en el plan."""
        originales = 0
        vistas: set[tuple[str | None, ...]] = set()
        duplicadas = 0
        rangos: dict[str, list[Decimal]] = {}
        for fila in self.fichero.filas():
            originales += 1
            firma = tuple(fila.get(c) for c in self.fichero.columnas)
            if firma in vistas:
                duplicadas += 1
            vistas.add(firma)
            for columna in self._divisor:
                try:
                    valor = Decimal(fila.get(columna) or "")
                except InvalidOperation:
                    continue
                r = rangos.setdefault(columna, [valor, valor])
                r[0], r[1] = min(r[0], valor), max(r[1], valor)

        descripcion: list[str] = []
        if self.transformacion.deduplicar:
            descripcion.append(
                f"Quitar {duplicadas} filas duplicadas exactas "
                f"({originales} → {originales - duplicadas} filas)."
            )
        for viejo, nuevo in self.transformacion.renombrar:
            descripcion.append(f"Renombrar la columna '{viejo}' a '{nuevo}'.")
        for columna, divisor in self.transformacion.dividir:
            if columna in rangos:
                minimo, maximo = rangos[columna]
                descripcion.append(
                    f"Dividir '{columna}' entre {divisor}: rango {minimo}–{maximo} → "
                    f"{minimo / divisor}–{maximo / divisor}."
                )
            else:
                descripcion.append(f"Dividir '{columna}' entre {divisor}.")
        return {
            "filas_en_fichero": originales,
            "duplicadas_exactas": duplicadas,
            "filas_tras_transformar": originales - duplicadas
            if self.transformacion.deduplicar
            else originales,
            "descripcion": descripcion,
        }
