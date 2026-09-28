"""Tests del patrón planificar → aplicar (modelo de plan y almacén)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from dataops_agent.planes import (
    AlmacenPlanes,
    ErrorPlan,
    EstadoPlan,
    Paso,
    Plan,
    TipoPaso,
    TipoPlan,
)


def _plan(**cambios: object) -> Plan:
    datos: dict[str, object] = {
        "tipo": TipoPlan.CREAR_TABLA,
        "resumen": "Crear tabla clientes",
        "pasos": [Paso(TipoPaso.SQL, "Crear tabla", "CREATE TABLE clientes (id integer)")],
    }
    datos.update(cambios)
    return Plan(**datos)  # type: ignore[arg-type]


@pytest.fixture
def almacen() -> AlmacenPlanes:
    return AlmacenPlanes(timedelta(minutes=60))


def test_plan_expone_sql_e_impacto() -> None:
    datos = _plan(impacto={"objeto_nuevo": "clientes"}).como_dict()
    assert datos["plan_id"].startswith("plan_")
    assert datos["sql"] == "CREATE TABLE clientes (id integer);"
    assert datos["aplicable"] is True
    assert "aplicar_plan(" in datos["siguiente_paso"]


def test_plan_destructivo_indica_herramienta_con_aprobacion() -> None:
    datos = _plan(destructivo=True).como_dict()
    assert "aplicar_plan_destructivo" in datos["siguiente_paso"]


def test_ids_unicos() -> None:
    assert len({_plan().id for _ in range(200)}) == 200


def test_obtener_plan_valido(almacen: AlmacenPlanes) -> None:
    plan = almacen.guardar(_plan())
    assert almacen.obtener_para_aplicar(plan.id) is plan


def test_plan_inexistente(almacen: AlmacenPlanes) -> None:
    with pytest.raises(ErrorPlan, match="No existe"):
        almacen.obtener_para_aplicar("plan_nada")


def test_plan_no_se_aplica_dos_veces(almacen: AlmacenPlanes) -> None:
    plan = almacen.guardar(_plan())
    almacen.marcar(plan.id, EstadoPlan.APLICADO)
    with pytest.raises(ErrorPlan, match="aplicado"):
        almacen.obtener_para_aplicar(plan.id)


def test_plan_caducado(almacen: AlmacenPlanes) -> None:
    plan = almacen.guardar(_plan())
    mas_tarde = datetime.now(UTC) + timedelta(minutes=61)
    with pytest.raises(ErrorPlan, match="caducado"):
        almacen.obtener_para_aplicar(plan.id, ahora=mas_tarde)


def test_plan_alterado_tras_revisarlo(almacen: AlmacenPlanes) -> None:
    plan = almacen.guardar(_plan())
    # Alguien cambia el SQL después de que la persona lo revisara:
    plan.pasos[0] = replace(plan.pasos[0], sql="DROP TABLE clientes")
    with pytest.raises(ErrorPlan, match="alterado"):
        almacen.obtener_para_aplicar(plan.id)


def test_plan_con_bloqueos(almacen: AlmacenPlanes) -> None:
    plan = almacen.guardar(_plan(bloqueos=["3 huérfanos"]))
    assert plan.como_dict()["aplicable"] is False
    with pytest.raises(ErrorPlan, match="huérfanos"):
        almacen.obtener_para_aplicar(plan.id)


def test_pendientes(almacen: AlmacenPlanes) -> None:
    a, b = almacen.guardar(_plan()), almacen.guardar(_plan())
    almacen.marcar(a.id, EstadoPlan.APLICADO)
    assert almacen.pendientes() == [b]
