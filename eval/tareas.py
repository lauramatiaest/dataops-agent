"""Carga y validación de las tareas de evaluación (eval/tareas/*.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from eval.comparador import TIPOS
from eval.preparar import ESTADOS

DIR_TAREAS = Path(__file__).resolve().parent / "tareas"
CAMPOS_OBLIGATORIOS = {"id", "titulo", "categoria", "preparacion", "instruccion", "comprobaciones"}
CATEGORIAS = {"ingesta", "modelado", "transformacion", "consulta", "seguridad", "diagnostico",
              "reporting"}


class ErrorTarea(ValueError):
    pass


def _validar_comprobacion(c: dict[str, Any], donde: str) -> None:
    if c.get("tipo") not in TIPOS:
        raise ErrorTarea(f"{donde}: tipo de comprobación desconocido {c.get('tipo')!r}")
    if c["tipo"] == "cualquiera":
        for opcion in c["opciones"]:
            for sub in opcion:
                _validar_comprobacion(sub, donde)


def validar(tarea: dict[str, Any], ruta: Path) -> dict[str, Any]:
    faltan = CAMPOS_OBLIGATORIOS - set(tarea)
    if faltan:
        raise ErrorTarea(f"{ruta.name}: faltan campos {sorted(faltan)}")
    if tarea["preparacion"] not in ESTADOS:
        raise ErrorTarea(f"{ruta.name}: preparación desconocida {tarea['preparacion']!r}")
    if tarea["categoria"] not in CATEGORIAS:
        raise ErrorTarea(f"{ruta.name}: categoría desconocida {tarea['categoria']!r}")
    if not ruta.name.startswith(str(tarea["id"])):
        raise ErrorTarea(f"{ruta.name}: el nombre debe empezar por el id {tarea['id']}")
    for c in tarea["comprobaciones"]:
        _validar_comprobacion(c, ruta.name)
    return tarea


def cargar(patron: str = "*", directorio: Path = DIR_TAREAS) -> list[dict[str, Any]]:
    tareas = []
    for ruta in sorted(directorio.glob(f"{patron}.yaml")):
        tarea = yaml.safe_load(ruta.read_text(encoding="utf-8"))
        tarea["id"] = str(tarea["id"])
        tareas.append(validar(tarea, ruta))
    return tareas
