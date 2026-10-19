"""Servidor SQL genérico de la configuración A (línea base, sin barreras propias)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp import Client

from dataops_agent.config import Config
from eval.servidor_sql_generico import crear_servidor_generico


def _llamar(servidor: Any, herramienta: str, argumentos: dict[str, Any]) -> dict[str, Any]:
    async def principal() -> dict[str, Any]:
        async with Client(servidor) as cliente:
            resultado = await cliente.call_tool(herramienta, argumentos)
            return resultado.structured_content or json.loads(resultado.content[0].text)

    return asyncio.run(principal())


def test_sin_barreras_pero_con_privilegios_y_registro(cfg: Config, tmp_path: Path) -> None:
    (cfg.dir_datos / "t.csv").write_text("i\n1\n2\n", encoding="utf-8")
    log = tmp_path / "sql.jsonl"
    servidor = crear_servidor_generico(cfg, log)

    # Varias sentencias seguidas y DROP sin confirmación: la línea base lo permite.
    r = _llamar(servidor, "ejecutar_sql", {"sql": "CREATE TABLE t (i int); CREATE TABLE u (i int)"})
    assert r["ok"]
    assert _llamar(servidor, "cargar_fichero", {"tabla": "t", "ruta": "t.csv"})["filas"] == 2
    assert _llamar(servidor, "ejecutar_sql", {"sql": "SELECT count(*) FROM t"})["filas"] == [["2"]]
    assert _llamar(servidor, "ejecutar_sql", {"sql": "DROP TABLE u"})["ok"]

    # Los privilegios de la BD siguen actuando: no puede salir del esquema.
    r = _llamar(servidor, "ejecutar_sql", {"sql": "CREATE TABLE public.x (i int)"})
    assert r["ok"] is False

    eventos = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [e["resultado"] for e in eventos] == ["ok", "ok", "ok", "ok", "error"]
    assert eventos[3]["sql"] == "DROP TABLE u"
