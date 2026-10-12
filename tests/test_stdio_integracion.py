"""El servidor arranca por stdio como lo haría Claude Code (subproceso real)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from dataops_agent.config import Config

RAIZ = Path(__file__).resolve().parent.parent


def test_arranque_por_stdio(cfg: Config, tmp_path: Path) -> None:
    (cfg.dir_datos / "a.csv").write_text("id\n1\n", encoding="utf-8")
    entorno = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(tmp_path),  # sin .env: todo llega por variables
        "PYTHONPATH": os.pathsep.join([str(RAIZ / "src"), os.environ.get("PYTHONPATH", "")]),
        "DATAOPS_PG_HOST": cfg.pg_host,
        "DATAOPS_PG_PUERTO": str(cfg.pg_puerto),
        "DATAOPS_PG_BD": cfg.pg_bd,
        "DATAOPS_ESQUEMA": cfg.esquema,
        "DATAOPS_LECTOR_USUARIO": cfg.lector_usuario,
        "DATAOPS_LECTOR_PASSWORD": cfg.lector_password,
        "DATAOPS_ESCRITOR_USUARIO": cfg.escritor_usuario,
        "DATAOPS_ESCRITOR_PASSWORD": cfg.escritor_password,
        "DATAOPS_DIR_DATOS": str(cfg.dir_datos),
    }
    parametros = StdioServerParameters(
        command=sys.executable, args=["-m", "dataops_agent.servidor"], env=entorno
    )

    async def principal() -> tuple[set[str], str]:
        async with Client(parametros) as cliente:
            herramientas = {t.name for t in (await cliente.list_tools()).tools}
            resultado = await cliente.call_tool("perfilar_fichero", {"ruta": "a.csv"})
            return herramientas, str(resultado.content)

    herramientas, contenido = asyncio.run(principal())
    assert "aplicar_plan_destructivo" in herramientas
    assert "candidatas_clave" in contenido
    assert (tmp_path / "auditoria" / "auditoria.jsonl").exists()
