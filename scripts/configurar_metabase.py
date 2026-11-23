"""Configuración inicial de Metabase (se ejecuta una vez, tras `docker compose up`).

1. Espera a que Metabase responda en /api/health.
2. Completa el asistente inicial con la cuenta de METABASE_USUARIO/METABASE_PASSWORD.
3. Registra PostgreSQL con el rol LECTOR (Metabase nunca puede escribir).
4. Lanza la sincronización del esquema.

Uso:  python scripts/configurar_metabase.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

from dataops_agent.config import Config, cargar_dotenv
from dataops_agent.metabase import ErrorBI, MetabaseAdapter

RAIZ = Path(__file__).resolve().parent.parent


def esperar_metabase(url: str, segundos: int = 300) -> None:
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        try:
            if httpx.get(f"{url}/api/health", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        print("  Metabase todavía arrancando...", flush=True)
        time.sleep(5)
    sys.exit(f"Metabase no respondió en {segundos} s en {url}")


def main() -> None:
    cargar_dotenv(RAIZ / ".env")
    cfg = Config.desde_entorno()
    # Dentro de la red de Docker, Metabase llega a PostgreSQL por el nombre del servicio.
    host_pg = os.environ.get("METABASE_PG_HOST", "postgres")
    print(f"1/4 Esperando a Metabase en {cfg.metabase_url}")
    esperar_metabase(cfg.metabase_url)

    mb = MetabaseAdapter(cfg)
    print("2/4 Asistente inicial")
    hecho = mb.configurar_admin(cfg.metabase_usuario, cfg.metabase_password)
    print("    completado ahora" if hecho else "    ya estaba completado")

    print(f"3/4 Registrando PostgreSQL ({host_pg}:5432, rol {cfg.lector_usuario})")
    try:
        bd_id = mb.registrar_base_datos(host_pg, 5432)
    except ErrorBI as error:
        sys.exit(f"    Error: {error}")
    print(f"    base de datos id={bd_id} ('{cfg.metabase_bd_nombre}')")

    print("4/4 Sincronizando esquema (en segundo plano en Metabase)")
    mb.sincronizar()

    print("Extra: formato regional (español, coma decimal y punto de miles)")
    try:
        mb.configurar_regional()
        print("    aplicado")
    except ErrorBI as error:  # no es imprescindible: el resto ya está configurado
        print(f"    no se pudo aplicar ({error}); se puede cambiar en Admin → Settings")
    print(f"Listo. Abre {cfg.metabase_url} e inicia sesión con {cfg.metabase_usuario}.")


if __name__ == "__main__":
    main()
