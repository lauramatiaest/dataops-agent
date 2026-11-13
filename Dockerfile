# Servidor MCP de DataOps Agent como aplicación independiente (transporte HTTP).
# Se construye y arranca con docker compose (servicio "mcp"); ver README.
FROM python:3.12-slim

# uv, misma versión que en desarrollo, para instalar desde uv.lock
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Primero solo las dependencias: esta capa se reutiliza mientras no cambie uv.lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Sin privilegios de root. Los datos se montan en solo lectura; la auditoría, en escritura.
RUN useradd --create-home dataops && mkdir -p /app/datos /app/auditoria \
    && chown dataops /app/auditoria
USER dataops

ENV PATH="/app/.venv/bin:$PATH" \
    DATAOPS_MCP_TRANSPORTE=http \
    DATAOPS_MCP_HOST=0.0.0.0 \
    DATAOPS_MCP_PUERTO=8080 \
    DATAOPS_DIR_DATOS=/app/datos \
    DATAOPS_DIR_AUDITORIA=/app/auditoria
EXPOSE 8080

CMD ["dataops-mcp"]
