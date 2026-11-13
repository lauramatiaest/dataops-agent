"""Configuración del servidor, leída de variables de entorno.

Se usa una dataclass inmutable para que la configuración no cambie durante la
ejecución y sea fácil de construir en los tests sin tocar el entorno real.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Identificadores SQL admitidos por todo el proyecto: minúsculas, dígitos y guion bajo.
# Restringirlos evita problemas de comillas y de mayúsculas en PostgreSQL.
PATRON_IDENTIFICADOR = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class ErrorConfiguracion(ValueError):
    """La configuración no es válida."""


def validar_identificador(valor: str, que: str = "identificador") -> str:
    """Devuelve `valor` si es un identificador SQL seguro; si no, lanza ValueError."""
    if not PATRON_IDENTIFICADOR.match(valor):
        raise ValueError(
            f"{que} no válido: {valor!r}. Usa minúsculas, dígitos y '_' "
            "(máx. 63 caracteres, sin empezar por dígito)."
        )
    return valor


@dataclass(frozen=True)
class Config:
    """Parámetros de ejecución del servidor MCP."""

    pg_host: str = "127.0.0.1"
    pg_puerto: int = 5432
    pg_bd: str = "dataops"
    esquema: str = "proyecto_demo"
    lector_usuario: str = "dataops_lector"
    lector_password: str = ""
    escritor_usuario: str = "dataops_escritor"
    escritor_password: str = ""
    dir_datos: Path = Path("datos")
    dir_auditoria: Path = Path("auditoria")
    timeout_ms: int = 15_000
    max_filas: int = 200
    caducidad_plan_min: int = 60
    actor: str = "claude-code"
    metabase_url: str = "http://127.0.0.1:3000"
    # URL para los enlaces que ve la persona; difiere de metabase_url cuando el servidor
    # MCP corre en Docker y habla con Metabase por la red interna (http://metabase:3000).
    metabase_url_publica: str = ""
    metabase_usuario: str = ""
    metabase_password: str = ""
    metabase_api_key: str = ""
    metabase_bd_nombre: str = "DataOps (solo lectura)"

    def __post_init__(self) -> None:
        validar_identificador(self.esquema, "esquema")
        if self.esquema.startswith("pg_") or self.esquema in {"public", "dataops_meta"}:
            raise ErrorConfiguracion(f"El esquema de proyecto no puede ser {self.esquema!r}.")
        if self.timeout_ms <= 0 or self.max_filas <= 0:
            raise ErrorConfiguracion("timeout_ms y max_filas deben ser positivos.")

    def dsn(self, rol: str) -> str:
        """Cadena de conexión para el rol 'lector' o 'escritor'."""
        if rol == "lector":
            usuario, password = self.lector_usuario, self.lector_password
        elif rol == "escritor":
            usuario, password = self.escritor_usuario, self.escritor_password
        else:
            raise ValueError(f"Rol desconocido: {rol}")
        # psycopg acepta la forma clave=valor; se usa en lugar de URL para no
        # tener que escapar caracteres especiales de la contraseña.
        partes = {
            "host": self.pg_host,
            "port": str(self.pg_puerto),
            "dbname": self.pg_bd,
            "user": usuario,
            "password": password,
            "application_name": f"dataops-agent-{rol}",
        }
        return " ".join(f"{k}='{_escapar_dsn(v)}'" for k, v in partes.items())

    @classmethod
    def desde_entorno(cls, entorno: Mapping[str, str] | None = None) -> Config:
        """Construye la configuración a partir de variables DATAOPS_* y METABASE_*."""
        e = os.environ if entorno is None else entorno

        def obtener(nombre: str, defecto: str) -> str:
            return e.get(nombre, defecto)

        return cls(
            pg_host=obtener("DATAOPS_PG_HOST", "127.0.0.1"),
            pg_puerto=int(obtener("DATAOPS_PG_PUERTO", "5432")),
            pg_bd=obtener("DATAOPS_PG_BD", "dataops"),
            esquema=obtener("DATAOPS_ESQUEMA", "proyecto_demo"),
            lector_usuario=obtener("DATAOPS_LECTOR_USUARIO", "dataops_lector"),
            lector_password=obtener("DATAOPS_LECTOR_PASSWORD", ""),
            escritor_usuario=obtener("DATAOPS_ESCRITOR_USUARIO", "dataops_escritor"),
            escritor_password=obtener("DATAOPS_ESCRITOR_PASSWORD", ""),
            dir_datos=Path(obtener("DATAOPS_DIR_DATOS", "datos")).resolve(),
            dir_auditoria=Path(obtener("DATAOPS_DIR_AUDITORIA", "auditoria")).resolve(),
            timeout_ms=int(obtener("DATAOPS_TIMEOUT_MS", "15000")),
            max_filas=int(obtener("DATAOPS_MAX_FILAS", "200")),
            caducidad_plan_min=int(obtener("DATAOPS_CADUCIDAD_PLAN_MIN", "60")),
            actor=obtener("DATAOPS_ACTOR", "claude-code"),
            metabase_url=obtener("METABASE_URL", "http://127.0.0.1:3000").rstrip("/"),
            metabase_url_publica=obtener("METABASE_URL_PUBLICA", "").rstrip("/"),
            metabase_usuario=obtener("METABASE_USUARIO", ""),
            metabase_password=obtener("METABASE_PASSWORD", ""),
            metabase_api_key=obtener("METABASE_API_KEY", ""),
            metabase_bd_nombre=obtener("METABASE_BD_NOMBRE", "DataOps (solo lectura)"),
        )


def cargar_dotenv(ruta: Path) -> None:
    """Carga un fichero .env sencillo (CLAVE=valor) sin sobrescribir el entorno.

    Evita una dependencia extra: solo admite líneas CLAVE=valor y comentarios con #.
    """
    if not ruta.is_file():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def _escapar_dsn(valor: str) -> str:
    """Escapa comillas simples y barras para el formato clave='valor' de libpq."""
    return valor.replace("\\", "\\\\").replace("'", "\\'")
