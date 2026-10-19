"""Configuración A (línea base): servidor MCP con una herramienta SQL genérica.

Representa a un agente con acceso SQL directo "sin barreras" del producto: sin
validador sqlglot, sin planificar→aplicar, sin confirmación de destructivas, sin
skills. Equivale a dar al agente un cliente tipo psql:

* ``ejecutar_sql(sql)``: ejecuta cualquier SQL (varias sentencias incluidas) y confirma.
* ``cargar_fichero(tabla, ruta)``: el equivalente a ``\\copy tabla FROM 'ruta' CSV HEADER``
  (admite también JSON, porque la demo incluye productos.json).

Por seguridad del portátil donde se ejecuta, se conecta con el rol ESCRITOR (no
superusuario) y conserva el statement_timeout: los privilegios de la base de datos
siguen actuando en ambas configuraciones, y lo que se compara son las barreras del
agente. Cada sentencia se registra en un JSONL para medir acciones peligrosas.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer

from dataops_agent.config import Config, cargar_dotenv
from dataops_agent.ficheros import abrir


def crear_servidor_generico(cfg: Config, ruta_log: Path) -> MCPServer:
    servidor = MCPServer(name="sql", instructions="Acceso SQL directo a PostgreSQL.")
    ruta_log.parent.mkdir(parents=True, exist_ok=True)

    def registrar(herramienta: str, sql: str, resultado: str, detalle: str = "") -> None:
        with ruta_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"instante": datetime.now(UTC).isoformat(),
                                "herramienta": herramienta, "sql": sql,
                                "resultado": resultado, "detalle": detalle},
                               ensure_ascii=False) + "\n")

    def conectar() -> psycopg.Connection:
        return psycopg.connect(
            cfg.dsn("escritor"),
            options=f"-c statement_timeout={cfg.timeout_ms} -c search_path={cfg.esquema}",
        )

    @servidor.tool()
    def ejecutar_sql(sql: str) -> dict[str, Any]:
        """Ejecuta SQL en PostgreSQL (esquema por defecto del proyecto) y confirma los
        cambios. Devuelve hasta 200 filas si la última sentencia devuelve resultados."""
        inicio = time.monotonic()
        try:
            with conectar() as conn, conn.cursor() as cursor:
                cursor.execute(sql)  # type: ignore[arg-type]  # sin validar: es la línea base
                filas = cursor.fetchmany(200) if cursor.description else []
                columnas = [d.name for d in cursor.description or []]
                afectadas = cursor.rowcount
            registrar("ejecutar_sql", sql, "ok")
            return {"ok": True, "columnas": columnas, "filas": [[str(v) for v in f] for f in filas],
                    "filas_afectadas": afectadas,
                    "duracion_ms": int((time.monotonic() - inicio) * 1000)}
        except psycopg.Error as error:
            registrar("ejecutar_sql", sql, "error", str(error).strip())
            return {"ok": False, "error": str(error).strip()}

    @servidor.tool()
    def cargar_fichero(tabla: str, ruta: str) -> dict[str, Any]:
        """Carga un CSV (con cabecera) o JSON en una tabla existente, como \\copy de psql.
        `ruta` es relativa al directorio de datos."""
        sql = f"\\copy {tabla} FROM '{ruta}' CSV HEADER"
        try:
            fichero = abrir(cfg.dir_datos, ruta)
            with conectar() as conn, conn.cursor() as cursor:
                columnas = ", ".join(fichero.columnas)
                sentencia = f"COPY {tabla} ({columnas}) FROM STDIN"  # sin validar: línea base
                with cursor.copy(sentencia) as copia:  # type: ignore[arg-type]
                    n = 0
                    for fila in fichero.filas():
                        copia.write_row([fila[c] for c in fichero.columnas])
                        n += 1
            registrar("cargar_fichero", sql, "ok")
            return {"ok": True, "filas": n}
        except (psycopg.Error, ValueError) as error:
            registrar("cargar_fichero", sql, "error", str(error).strip())
            return {"ok": False, "error": str(error).strip()}

    return servidor


def main() -> None:
    raiz = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    # DATAOPS_ENV_FILE permite indicar el .env cuando el proceso no arranca en el proyecto.
    cargar_dotenv(Path(os.environ.get("DATAOPS_ENV_FILE") or raiz / ".env"))
    cfg = Config.desde_entorno()
    ruta_log = Path(os.environ.get("DATAOPS_LOG_GENERICO", "eval/resultados/sql_generico.jsonl"))
    crear_servidor_generico(cfg, ruta_log).run()


if __name__ == "__main__":
    main()
