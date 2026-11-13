"""Transporte Streamable HTTP del servidor MCP, protegido con un token.

Con HTTP el servidor deja de ser un proceso hijo de Claude Code y pasa a ser una
**aplicación independiente** (un servicio más de ``docker compose``) a la que el cliente
se conecta por URL (``http://127.0.0.1:8080/mcp``). Al abrir un puerto, cualquier
programa del equipo podría llamarlo, así que se exige un token:

* cabecera ``Authorization: Bearer <DATAOPS_MCP_TOKEN>`` en cada petición, comparada
  en tiempo constante; sin token configurado el servidor **no arranca** en HTTP;
* protección contra *DNS rebinding*: solo se aceptan cabeceras ``Host``/``Origin`` de
  ``127.0.0.1``/``localhost`` (el puerto se publica solo en ``127.0.0.1``).

El transporte stdio sigue disponible (``DATAOPS_MCP_TRANSPORTE=stdio``, el valor por
defecto): lo usan los tests y la evaluación, que arranca un servidor limpio por tarea.
"""

from __future__ import annotations

import hmac
import json
from typing import TYPE_CHECKING

from mcp.server.transport_security import TransportSecuritySettings

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer
    from starlette.types import ASGIApp, Receive, Scope, Send

LONGITUD_MINIMA_TOKEN = 24
RUTA = "/mcp"
SEGURIDAD = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*"],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
)


class ErrorConfiguracionHTTP(RuntimeError):
    """El transporte HTTP no puede arrancar con esta configuración."""


def comprobar_token(token: str | None) -> str:
    if not token or len(token) < LONGITUD_MINIMA_TOKEN:
        raise ErrorConfiguracionHTTP(
            "DATAOPS_MCP_TOKEN es obligatorio para servir por HTTP y debe tener al menos "
            f"{LONGITUD_MINIMA_TOKEN} caracteres. Genera uno con: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return token


class TokenPortador:
    """Middleware ASGI: rechaza con 401 las peticiones HTTP sin el token correcto."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._esperado = f"Bearer {comprobar_token(token)}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            recibido = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(recibido, self._esperado):
                cuerpo = json.dumps({"error": "Token ausente o incorrecto."}).encode()
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                })
                await send({"type": "http.response.body", "body": cuerpo})
                return
        await self.app(scope, receive, send)


def aplicacion(servidor: MCPServer, token: str) -> ASGIApp:
    """Aplicación ASGI del servidor MCP por HTTP, protegida con el token."""
    comprobar_token(token)
    app = servidor.streamable_http_app(streamable_http_path=RUTA, transport_security=SEGURIDAD)
    return TokenPortador(app, token)


def servir(servidor: MCPServer, token: str, host: str, puerto: int) -> None:
    """Arranca el servidor HTTP (bloqueante)."""
    import uvicorn

    uvicorn.run(aplicacion(servidor, token), host=host, port=puerto, log_level="info")
