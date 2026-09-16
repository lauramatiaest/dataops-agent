"""Tests del registro de auditoría (JSONL + escritor de BD simulado)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from dataops_agent.auditoria import EventoAuditoria, RegistroAuditoria, Resultado


def _evento(**cambios: object) -> EventoAuditoria:
    datos: dict[str, object] = {
        "herramienta": "aplicar_plan",
        "operacion": "CREATE TABLE",
        "resultado": Resultado.OK,
        "sql": "CREATE TABLE x (i integer)",
        "plan_id": "plan-123",
    }
    datos.update(cambios)
    return EventoAuditoria(**datos)  # type: ignore[arg-type]


def test_escribe_jsonl_con_quien_que_cuando(tmp_path: Path) -> None:
    registro = RegistroAuditoria(tmp_path / "a.jsonl", actor="laura", esquema="proyecto_demo")
    registro.registrar(_evento())
    [linea] = registro.leer_jsonl()
    assert linea["actor"] == "laura"  # quién
    assert linea["operacion"] == "CREATE TABLE"  # qué
    assert linea["instante"]  # cuándo
    assert linea["resultado"] == "ok"  # resultado
    assert linea["plan_id"] == "plan-123"
    assert linea["esquema"] == "proyecto_demo"
    assert linea["guardado_en_bd"] is False  # no había escritor de BD


def test_llama_al_escritor_de_bd(tmp_path: Path) -> None:
    recibidos: list[EventoAuditoria] = []
    registro = RegistroAuditoria(tmp_path / "a.jsonl", "x", "p", escritor_bd=recibidos.append)
    registro.registrar(_evento(destructiva=True, resultado=Resultado.PENDIENTE_CONFIRMACION))
    assert len(recibidos) == 1 and recibidos[0].destructiva
    assert registro.leer_jsonl()[0]["guardado_en_bd"] is True


def test_fallo_de_bd_no_rompe_y_queda_en_jsonl(tmp_path: Path, capsys) -> None:
    def escritor_roto(_: EventoAuditoria) -> None:
        raise ConnectionError("BD caída")

    registro = RegistroAuditoria(tmp_path / "a.jsonl", "x", "p", escritor_bd=escritor_roto)
    registro.registrar(_evento())
    assert registro.leer_jsonl()[0]["guardado_en_bd"] is False
    salida = capsys.readouterr()
    assert salida.out == ""  # nada en stdout (canal MCP)
    assert "BD caída" in salida.err


def test_detalle_con_tipos_no_json(tmp_path: Path) -> None:
    registro = RegistroAuditoria(tmp_path / "a.jsonl", "x", "p")
    registro.registrar(_evento(detalle={"fecha": date(2026, 10, 18)}))
    assert registro.leer_jsonl()[0]["detalle"]["fecha"] == "2026-10-18"


def test_anexa_sin_sobrescribir(tmp_path: Path) -> None:
    registro = RegistroAuditoria(tmp_path / "sub" / "a.jsonl", "x", "p")
    for _ in range(3):
        registro.registrar(_evento())
    eventos = registro.leer_jsonl()
    assert len(eventos) == 3
    assert len({e["id"] for e in eventos}) == 3
