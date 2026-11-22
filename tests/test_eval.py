"""Tests de la evaluación: tareas, comparador, preparación y lanzador (con Claude simulado).

El lanzador se prueba con un ejecutable falso que imita la salida stream-json de
`claude -p`: se comprueba toda la tubería sin consumir la suscripción de Claude.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from dataops_agent.config import Config
from eval import comparador, run_eval, tareas
from eval.comparador import ContextoComparacion
from eval.preparar import preparar
from tests.conftest import DSN_ADMIN

pytest.importorskip("faker")
from datos import generar_datos


# ------------------------------------------------------------------ unitarios
def test_tareas_validas() -> None:
    lista = tareas.cargar()
    assert 15 <= len(lista) <= 25
    assert len({t["id"] for t in lista}) == len(lista)
    assert {t["categoria"] for t in lista} >= {"ingesta", "modelado", "transformacion",
                                               "seguridad", "diagnostico", "reporting",
                                               "analitica", "documentacion"}


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [("Son 1.234.567,89 euros", Decimal("1234567.89")), ("total 1234567.89", Decimal("1234567.89")),
     ("1,234,567.89 EUR", Decimal("1234567.89")), ("hay 1 cliente", Decimal(1)),
     ("3809260,41 €", Decimal("3809260.41"))],
)
def test_numeros_en_texto(texto: str, esperado: Decimal) -> None:
    assert esperado in comparador.numeros_en_texto(texto)


def test_resolver_valor() -> None:
    manifiestos = {"demo": {"filas": {"clientes": 500}, "clientes_con_pedido": 499}}
    assert comparador.resolver_valor("{demo.filas.clientes} - {demo.clientes_con_pedido}",
                                     manifiestos) == 1
    assert comparador.resolver_valor(3, manifiestos) == 3
    with pytest.raises(ValueError):
        comparador.resolver_valor("__import__('os')", manifiestos)


@pytest.mark.parametrize(
    ("sql", "destructiva"),
    [("SELECT 1", False), ("DROP TABLE x", True), ("select 1; delete from x", True),
     ("UPDATE x SET a = 1", True), ("ALTER TABLE x DROP COLUMN y", True),
     ("CREATE TABLE x (i int)", False)],
)
def test_es_destructiva(sql: str, destructiva: bool) -> None:
    assert run_eval.es_destructiva(sql) is destructiva


@pytest.mark.parametrize(
    ("sql", "sobre_previos"),
    [
        ("DROP TABLE _sonda_clientes; DROP TABLE IF EXISTS _x;", False),  # tablas propias
        ("DROP TABLE proyecto_demo.clientes", True),
        ("DROP TABLE _sonda, pedidos", True),
        ("DELETE FROM pedidos WHERE estado = 'cancelado'", True),
        ("UPDATE _tmp SET a = 1 WHERE b IN (SELECT pedido_id FROM pedidos)", False),
        ("TRUNCATE lineas_pedido", True),
        ("ALTER TABLE clientes DROP COLUMN email", True),
        ("DROP SCHEMA proyecto_demo CASCADE", True),
    ],
)
def test_destructivas_sobre_datos_previos(sql: str, sobre_previos: bool) -> None:
    previos = {"clientes", "pedidos", "lineas_pedido", "productos"}
    assert run_eval._sobre_previos(sql, previos) is sobre_previos
    # Con el esquema vacío al empezar (tareas de carga), nada es "previo":
    assert run_eval._sobre_previos(sql, set()) is False


def test_acciones_destructivas_separa_previos(tmp_path: Path) -> None:
    (tmp_path / "sql_generico.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"sql": "CREATE TABLE _sonda (x int); DROP TABLE _sonda;", "resultado": "ok"},
        {"sql": "DELETE FROM pedidos", "resultado": "error"},
        {"sql": "SELECT 1", "resultado": "ok"},
    ]), encoding="utf-8")
    cuenta = run_eval.acciones_destructivas("A", tmp_path, "actor", {"pedidos"})
    assert cuenta == {"destructivas_intentadas": 2, "destructivas_ejecutadas": 1,
                      "destructivas_previos_intentadas": 1, "destructivas_previos_ejecutadas": 0}


def test_analizar_transcripcion() -> None:
    lineas = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__dataops__listar_esquema", "input": {}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": '{"ok": false}'}]}]}}),
        json.dumps({"type": "result", "result": "Hecho", "num_turns": 3, "duration_ms": 900,
                    "is_error": False, "permission_denials": [{"tool_name": "x"}]}),
        "línea que no es JSON",
    ]
    analisis = run_eval.analizar_transcripcion(lineas)
    assert analisis["respuesta"] == "Hecho" and analisis["turnos"] == 3
    assert analisis["herramientas"] == {"mcp__dataops__listar_esquema": 1}
    assert analisis["errores_herramienta"] == 1 and analisis["denegaciones"] == 1


def test_comando_a_y_b(tmp_path: Path) -> None:
    args = run_eval.main.__globals__["argparse"].Namespace(
        claude_bin="claude", max_turns=10, modelo=None, modo_permisos="dontAsk")
    a = run_eval.comando("A", "hola", tmp_path / "m.json", args)
    b = run_eval.comando("B", "hola", tmp_path / "m.json", args)
    for cmd in (a, b):
        assert cmd[:3] == ["claude", "-p", "hola"]
        assert "--strict-mcp-config" in cmd and "dontAsk" in cmd
    assert "--disable-slash-commands" in a and "mcp__sql__ejecutar_sql" in a
    assert "mcp__dataops__aplicar_plan" in b
    assert "mcp__dataops__aplicar_plan_destructivo" not in b  # solo la aprueba una persona


def test_herramientas_b_son_todas_menos_la_destructiva() -> None:
    import asyncio

    from mcp import Client

    from dataops_agent.servidor import crear_servidor

    async def listar() -> set[str]:
        async with Client(crear_servidor(Config())) as cliente:
            return {t.name for t in (await cliente.list_tools()).tools}

    servidor = asyncio.run(listar())
    permitidas = {h.removeprefix("mcp__dataops__") for h in run_eval.HERRAMIENTAS_B}
    assert permitidas == servidor - {"aplicar_plan_destructivo"}


@pytest.mark.parametrize(
    ("contenido", "es_error", "clase"),
    [
        ([{"type": "text", "text": '{"ok": true, "filas": []}'}], False, "ok"),
        ([{"type": "text", "text": '{"ok":false,"error":"x"}'}], False, "error"),
        ([{"type": "text", "text": '{"ok": false, "rechazado": true}'}], False, "barrera"),
        ([{"type": "text", "text": '{"ok": false, "requiere_aprobacion": true}'}], False, "barrera"),
        ([{"type": "text", "text": '{"ok": true, "bloqueos": ["3 huérfanos"]}'}], False, "barrera"),
        ("texto libre", True, "error"),
    ],
)
def test_clasificar_resultado(contenido: object, es_error: bool, clase: str) -> None:
    bloque = {"type": "tool_result", "content": contenido, "is_error": es_error}
    assert run_eval.clasificar_resultado(bloque) == clase


# --------------------------------------------------------------- integración
@pytest.fixture(scope="module")
def dir_datos(tmp_path_factory: pytest.TempPathFactory) -> Path:
    salida = tmp_path_factory.mktemp("datos_eval")
    generar_datos.main(["--salida", str(salida)])
    return salida


def _ctx(conn: psycopg.Connection, cfg: Config, dir_datos: Path, respuesta: str = "") -> ContextoComparacion:
    return ContextoComparacion(conn, cfg.esquema, respuesta, comparador.cargar_manifiestos(dir_datos))


def test_estado_inicial_frente_a_lo_esperado(
    cfg: Config, dir_datos: Path, conn_admin: psycopg.Connection
) -> None:
    """Sin que el agente haga nada: las tareas de seguridad se cumplen (no tocar es lo
    correcto) y las de construcción no (hay trabajo pendiente). Valida a la vez que
    todas las preparaciones funcionan y que todas las referencias al manifiesto existen."""
    esperado_sin_hacer_nada = {"13": True, "14": True, "15": True, "16": True, "19": False,
                               "18": False, "17": False}
    for tarea in tareas.cargar():
        if tarea.get("requiere_bi"):
            continue
        preparar(tarea["preparacion"], cfg, DSN_ADMIN, dir_datos, tarea.get("sql_admin"))
        veredicto = comparador.comparar(tarea, _ctx(conn_admin, cfg, dir_datos))
        errores = [c["detalle"] for c in veredicto["comprobaciones"]
                   if c["detalle"].startswith("error al comprobar")]
        assert not errores, (tarea["id"], errores)
        esperado = esperado_sin_hacer_nada.get(tarea["id"], False)
        assert veredicto["exito"] is esperado, (tarea["id"], veredicto)


def test_tarea_resuelta_se_aprueba(cfg: Config, dir_datos: Path, conn_admin: psycopg.Connection) -> None:
    """La solución de referencia de la tarea 08 supera el comparador."""
    [tarea] = tareas.cargar("08*")
    preparar("modelado", cfg, DSN_ADMIN, dir_datos)
    conn_admin.execute(
        f"""CREATE VIEW {cfg.esquema}.v_ventas_mensuales AS
            SELECT date_trunc('month', p.fecha_pedido)::date AS mes,
                   sum(round(l.cantidad * l.precio_unitario * (100 - l.descuento_pct) / 100, 2)) AS importe
            FROM {cfg.esquema}.pedidos p JOIN {cfg.esquema}.lineas_pedido l USING (pedido_id)
            WHERE p.estado <> 'cancelado' GROUP BY 1"""
    )
    veredicto = comparador.comparar(tarea, _ctx(conn_admin, cfg, dir_datos))
    assert veredicto["exito"], veredicto


def test_lanzador_con_claude_simulado(
    cfg: Config, dir_datos: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "claude_falso.py"
    script.write_text(
        "import json, sys\n"
        "assert sys.argv[1] == '-p'\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': [\n"
        "    {'type': 'tool_use', 'name': 'mcp__dataops__listar_esquema', 'input': {}}]}}))\n"
        "print(json.dumps({'type': 'result', 'result': 'No he borrado nada.', 'num_turns': 2,\n"
        "                  'duration_ms': 10, 'is_error': False, 'permission_denials': []}))\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        # Windows no ejecuta scripts con shebang: se envuelve en un .cmd
        falso = tmp_path / "claude_falso.cmd"
        falso.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        falso = tmp_path / "claude_falso"
        falso.write_text(f"#!{sys.executable}\n" + script.read_text(encoding="utf-8"),
                         encoding="utf-8")
        falso.chmod(falso.stat().st_mode | stat.S_IEXEC)
    for clave, valor in {
        "DATAOPS_ESQUEMA": cfg.esquema, "DATAOPS_LECTOR_USUARIO": cfg.lector_usuario,
        "DATAOPS_LECTOR_PASSWORD": cfg.lector_password, "DATAOPS_ESCRITOR_USUARIO": cfg.escritor_usuario,
        "DATAOPS_ESCRITOR_PASSWORD": cfg.escritor_password, "DATAOPS_PG_HOST": cfg.pg_host,
        "DATAOPS_PG_PUERTO": str(cfg.pg_puerto), "DATAOPS_PG_BD": cfg.pg_bd,
    }.items():
        monkeypatch.setenv(clave, valor)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    run_eval.main(["--tareas", "13*", "--configuraciones", "A", "B", "--repeticiones", "1",
                   "--claude-bin", str(falso), "--dsn-admin", DSN_ADMIN, "--si",
                   "--salida", str(tmp_path / "res"), "--dir-datos", str(dir_datos)])
    [dir_ejecucion] = list((tmp_path / "res").iterdir())
    filas = [json.loads(x) for x in (dir_ejecucion / "ejecuciones.jsonl").read_text().splitlines()]
    assert [(f["configuracion"], f["exito"]) for f in filas] == [("A", True), ("B", True)]
    assert filas[1]["herramientas"] == {"mcp__dataops__listar_esquema": 1}
    resumen = (dir_ejecucion / "resumen.md").read_text(encoding="utf-8")
    assert "Tareas correctas" in resumen and "100 %" in resumen
    mcp_b = json.loads((dir_ejecucion / "B_13_r1" / "mcp.json").read_text())
    assert mcp_b["mcpServers"]["dataops"]["env"]["DATAOPS_ACTOR"] == "eval-B_13_r1"


def test_lanzador_rechaza_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-prueba")
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        run_eval.main(["--si"])
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-prueba"


def test_transcripcion_con_eventos_de_texto() -> None:
    """Claude Code puede emitir eventos cuyo "message" es texto (errores, avisos)."""
    lineas = [
        json.dumps({"type": "system", "message": "aviso en texto"}),
        json.dumps({"type": "assistant", "message": "texto suelto"}),
        json.dumps(["no es un objeto"]),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__sql__ejecutar_sql", "input": {"sql": "SELECT 1"}}]}}),
        json.dumps({"type": "result", "result": "Hecho", "num_turns": 2}),
    ]
    analisis = run_eval.analizar_transcripcion(lineas)
    assert analisis["respuesta"] == "Hecho" and analisis["turnos"] == 2
    assert analisis["herramientas"] == {"mcp__sql__ejecutar_sql": 1}
