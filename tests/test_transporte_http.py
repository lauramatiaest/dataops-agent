"""Transporte HTTP del servidor MCP: token obligatorio y protección DNS rebinding.

Se prueba con un servidor MCP mínimo y un cliente MCP real (no hace falta PostgreSQL).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server.mcpserver import MCPServer

from dataops_agent.transporte_http import ErrorConfiguracionHTTP, aplicacion, comprobar_token

TOKEN = "t" * 32


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def url():
    servidor = MCPServer(name="prueba")

    @servidor.tool()
    def eco(texto: str) -> str:
        return texto.upper()

    puerto = _puerto_libre()
    http = uvicorn.Server(
        uvicorn.Config(aplicacion(servidor, TOKEN), host="127.0.0.1", port=puerto,
                       log_level="warning")
    )
    hilo = threading.Thread(target=http.run, daemon=True)
    hilo.start()
    for _ in range(50):
        if http.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{puerto}/mcp"
    http.should_exit = True
    hilo.join(timeout=5)


async def _estado(url: str, cabeceras: dict[str, str]) -> int:
    async with create_mcp_http_client(headers=cabeceras) as cliente:
        respuesta = await cliente.post(url, json={})
        return respuesta.status_code


@pytest.mark.parametrize(
    ("cabeceras", "esperado"),
    [
        ({}, 401),
        ({"Authorization": "Bearer otro-token-que-no-es-el-bueno"}, 401),
        ({"Authorization": f"Bearer {TOKEN}", "Host": "atacante.example"}, 421),
    ],
)
def test_rechaza_sin_token_o_con_host_ajeno(url: str, cabeceras, esperado: int) -> None:
    assert asyncio.run(_estado(url, cabeceras)) == esperado


def test_cliente_mcp_con_token(url: str) -> None:
    async def usar() -> tuple[list[str], str]:
        cabecera = {"Authorization": f"Bearer {TOKEN}"}
        async with (
            create_mcp_http_client(headers=cabecera) as hc,
            streamable_http_client(url, http_client=hc) as flujos,
            ClientSession(flujos[0], flujos[1]) as sesion,
        ):
            await sesion.initialize()
            herramientas = await sesion.list_tools()
            resultado = await sesion.call_tool("eco", {"texto": "hola"})
            return [h.name for h in herramientas.tools], resultado.content[0].text

    nombres, texto = asyncio.run(usar())
    assert nombres == ["eco"]
    assert texto == "HOLA"


@pytest.mark.parametrize("token", [None, "", "corto"])
def test_token_obligatorio_y_largo(token) -> None:
    with pytest.raises(ErrorConfiguracionHTTP, match="DATAOPS_MCP_TOKEN"):
        comprobar_token(token)
