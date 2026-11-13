"""Transformaciones declarativas de reparación (sin base de datos)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataops_agent.ficheros import abrir
from dataops_agent.transformaciones import (
    ErrorTransformacion,
    OrigenTransformado,
    Transformacion,
)

ROTO = "linea_id,pedido_id,precio\n1,10,2825\n2,10,49486\n2,10,49486\n3,11,abc\n"


@pytest.fixture
def fichero(tmp_path: Path):
    (tmp_path / "roto.csv").write_text(ROTO, encoding="utf-8")
    return abrir(tmp_path, "roto.csv")


def test_renombra_divide_y_deduplica_sin_tocar_el_fichero(fichero, tmp_path: Path) -> None:
    t = Transformacion.crear({"precio": "precio_unitario"}, {"precio": 100}, deduplicar=True)
    origen = OrigenTransformado(fichero, t)
    assert origen.columnas == ("linea_id", "pedido_id", "precio_unitario")
    filas = list(origen.filas())
    assert [f["precio_unitario"] for f in filas] == ["28.25", "494.86", "abc"]
    assert len(filas) == 3
    # El fichero entregado no cambia.
    assert (tmp_path / "roto.csv").read_text(encoding="utf-8") == ROTO


def test_efecto_cuantificado(fichero) -> None:
    efecto = OrigenTransformado(
        fichero, Transformacion.crear(dividir={"precio": 100}, deduplicar=True)
    ).efecto()
    assert efecto["filas_en_fichero"] == 4
    assert efecto["duplicadas_exactas"] == 1
    assert efecto["filas_tras_transformar"] == 3
    assert any("2825–49486" in d and "28.25–494.86" in d for d in efecto["descripcion"])


@pytest.mark.parametrize(
    ("argumentos", "mensaje"),
    [
        ({}, "ninguna transformación"),
        ({"dividir": {"precio": 7}}, "Divisor no permitido"),
        ({"renombrar": {"precio": "Mal-Nombre"}}, "no válido"),
    ],
)
def test_transformaciones_invalidas(argumentos, mensaje) -> None:
    with pytest.raises(ValueError, match=mensaje):
        Transformacion.crear(**argumentos)


def test_columnas_inexistentes_o_choques(fichero) -> None:
    with pytest.raises(ErrorTransformacion, match="inexistentes"):
        OrigenTransformado(fichero, Transformacion.crear({"no_existe": "x"}))
    with pytest.raises(ErrorTransformacion, match="repetidas"):
        OrigenTransformado(fichero, Transformacion.crear({"precio": "pedido_id"}))


def test_es_inmutable_y_serializable() -> None:
    t = Transformacion.crear({"b": "x", "a": "y"}, deduplicar=True)
    assert t.renombrar == (("a", "y"), ("b", "x"))  # orden estable → huella estable
    assert hash(t) == hash(Transformacion.crear({"a": "y", "b": "x"}, deduplicar=True))
