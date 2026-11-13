"""Tests de lectura de ficheros y perfilado."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataops_agent.ficheros import ErrorFichero, abrir, nombre_sql
from dataops_agent.perfilado import inferir_tipo, perfilar


def _escribir(dir_: Path, nombre: str, contenido: str) -> str:
    (dir_ / nombre).write_text(contenido, encoding="utf-8")
    return nombre


@pytest.mark.parametrize(
    ("valores", "tipo"),
    [
        (["1", "2", "-3"], "integer"),
        (["1", "3000000000"], "bigint"),
        (["1.5", "2", "-0.25"], "numeric(12,2)"),
        (["28.25", "494.86"], "numeric(12,2)"),
        (["0.123456789"], "numeric"),
        (["true", "false", "t"], "boolean"),
        (["2026-01-31", "2025-12-01"], "date"),
        (["2026-01-31 10:00:00", "2026-01-31T11:30"], "timestamp"),
        (["2026-01-31T10:00:00Z", "2026-01-31 10:00:00+02:00"], "timestamptz"),
        (["00123", "00456"], "text"),  # códigos con ceros a la izquierda
        (["1", "dos"], "text"),
        ([], "text"),
    ],
)
def test_inferir_tipo(valores: list[str], tipo: str) -> None:
    assert inferir_tipo(valores) == tipo


@pytest.mark.parametrize(
    ("original", "esperado"),
    [("Precio Unitario (€)", "precio_unitario"), ("Año", "ano"), ("1er", "c_1er"), ("id", "id")],
)
def test_nombre_sql(original: str, esperado: str) -> None:
    assert nombre_sql(original) == esperado


def test_perfil_csv(tmp_path: Path) -> None:
    nombre = _escribir(
        tmp_path,
        "clientes.csv",
        "cliente_id,Nombre,ciudad,alta\n1,Ana,Madrid,2026-01-02\n2,Luis,,2026-02-03\n"
        "3,Eva,Madrid,2026-03-04\n3,Eva,Madrid,2026-03-04\n",
    )
    perfil = perfilar(abrir(tmp_path, nombre))
    assert perfil.filas == 4
    assert perfil.filas_duplicadas == 1
    por_nombre = {c.nombre: c for c in perfil.columnas}
    assert por_nombre["cliente_id"].tipo_inferido == "integer"
    assert por_nombre["cliente_id"].candidata_clave is False  # el 3 está repetido
    assert por_nombre["ciudad"].nulos == 1
    assert por_nombre["ciudad"].distintos == 1
    assert por_nombre["alta"].tipo_inferido == "date"
    assert por_nombre["alta"].minimo == "2026-01-02"
    assert por_nombre["Nombre"].nombre_sql == "nombre"
    assert any("duplicadas" in a for a in perfil.advertencias)


def test_candidata_clave_y_punto_y_coma(tmp_path: Path) -> None:
    nombre = _escribir(tmp_path, "p.csv", "sku;precio\nA1;1.50\nA2;2.00\n")
    perfil = perfilar(abrir(tmp_path, nombre))
    assert perfil.candidatas_clave == ["sku"]
    assert perfil.columnas[1].tipo_inferido == "numeric(12,2)"


def test_json_lista_y_objeto_envoltorio(tmp_path: Path) -> None:
    datos = {"items": [{"sku": "A", "activo": True, "tags": ["x"]}, {"sku": "B", "activo": False}]}
    (tmp_path / "p.json").write_text(json.dumps(datos), encoding="utf-8")
    fichero = abrir(tmp_path, "p.json")
    assert fichero.columnas == ("sku", "activo", "tags")
    perfil = perfilar(fichero)
    tipos = {c.nombre: c.tipo_inferido for c in perfil.columnas}
    assert tipos == {"sku": "text", "activo": "boolean", "tags": "text"}
    assert perfil.columnas[2].nulos == 1


def test_jsonl(tmp_path: Path) -> None:
    _escribir(tmp_path, "e.jsonl", '{"a": 1}\n\n{"a": 2}\n')
    assert perfilar(abrir(tmp_path, "e.jsonl")).filas == 2


def test_ruta_fuera_del_directorio(tmp_path: Path) -> None:
    datos = tmp_path / "datos"
    datos.mkdir()
    _escribir(tmp_path, "secreto.csv", "a\n1\n")
    with pytest.raises(ErrorFichero, match="fuera del directorio"):
        abrir(datos, "../secreto.csv")
    with pytest.raises(ErrorFichero, match="fuera del directorio"):
        abrir(datos, str(tmp_path / "secreto.csv"))


def test_enlace_simbolico_que_escapa(tmp_path: Path) -> None:
    datos = tmp_path / "datos"
    datos.mkdir()
    _escribir(tmp_path, "secreto.csv", "a\n1\n")
    (datos / "enlace.csv").symlink_to(tmp_path / "secreto.csv")
    with pytest.raises(ErrorFichero, match="fuera del directorio"):
        abrir(datos, "enlace.csv")


def test_formato_y_errores(tmp_path: Path) -> None:
    _escribir(tmp_path, "x.txt", "a\n")
    with pytest.raises(ErrorFichero, match="Formato"):
        abrir(tmp_path, "x.txt")
    _escribir(tmp_path, "mal.csv", "a,b\n1\n")
    with pytest.raises(ErrorFichero, match="Línea 2"):
        perfilar(abrir(tmp_path, "mal.csv"))
    _escribir(tmp_path, "dup.csv", "a,a\n1,2\n")
    with pytest.raises(ErrorFichero, match="mismo nombre"):
        abrir(tmp_path, "dup.csv")
