"""Coherencia del agente: skills bien formadas y referencias a herramientas reales.

Evita un fallo típico: que CLAUDE.md, las skills o los permisos mencionen herramientas
que el servidor no tiene (o con otro nombre), lo que el agente no detectaría.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml
from mcp import Client

from dataops_agent.config import Config
from dataops_agent.servidor import crear_servidor

RAIZ = Path(__file__).resolve().parent.parent
SKILLS = sorted((RAIZ / ".claude" / "skills").glob("*/SKILL.md"))
VERBOS = r"(?:listar|perfilar|consultar|comprobar|validar|ver|planificar|aplicar|estado|" \
         r"sincronizar|crear|actualizar|anadir|trazar)_[a-z_]+"
# Herramientas futuras que el borrador de la ampliación menciona como pendientes.
FUTURAS = {"trazar_linaje", "planificar_transformacion_carga"}


@pytest.fixture(scope="module")
def herramientas(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    cfg = Config(dir_datos=tmp_path_factory.mktemp("d"), dir_auditoria=tmp_path_factory.mktemp("a"))

    async def listar() -> set[str]:
        async with Client(crear_servidor(cfg)) as cliente:
            return {t.name for t in (await cliente.list_tools()).tools}

    return asyncio.run(listar())


def _frontmatter(ruta: Path) -> tuple[dict[str, str], str]:
    texto = ruta.read_text(encoding="utf-8")
    assert texto.startswith("---\n"), f"{ruta} sin frontmatter"
    _, cabecera, cuerpo = texto.split("---\n", 2)
    return yaml.safe_load(cabecera), cuerpo


def test_skills_esperadas() -> None:
    nombres = {p.parent.name for p in SKILLS}
    assert nombres == {"ingesta", "modelado", "transformacion", "reporting", "validacion",
                       "diagnostico-reparacion", "documentacion", "analitica-avanzada"}


@pytest.mark.parametrize("ruta", SKILLS, ids=lambda p: p.parent.name)
def test_skill_bien_formada(ruta: Path) -> None:
    datos, cuerpo = _frontmatter(ruta)
    assert datos["name"] == ruta.parent.name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", datos["name"])
    assert 50 < len(datos["description"]) <= 1024
    assert len(cuerpo.splitlines()) < 500


@pytest.mark.parametrize(
    "ruta", [*SKILLS, RAIZ / "CLAUDE.md"], ids=lambda p: p.parent.name if p.name == "SKILL.md" else p.name
)
def test_menciona_solo_herramientas_existentes(ruta: Path, herramientas: set[str]) -> None:
    mencionadas = set(re.findall(rf"\b{VERBOS}\b", ruta.read_text(encoding="utf-8")))
    desconocidas = mencionadas - herramientas - FUTURAS
    assert not desconocidas, f"{ruta.name} menciona herramientas inexistentes: {desconocidas}"


def test_permisos_apuntan_a_herramientas_reales(herramientas: set[str]) -> None:
    permisos = json.loads((RAIZ / ".claude" / "settings.json").read_text())["permissions"]
    for regla in permisos["allow"] + permisos["ask"]:
        assert regla.removeprefix("mcp__dataops__") in herramientas, regla
    # La única herramienta destructiva exige aprobación y no está auto-permitida:
    assert permisos["ask"] == ["mcp__dataops__aplicar_plan_destructivo"]
    assert "mcp__dataops__aplicar_plan_destructivo" not in permisos["allow"]


def test_mcp_json() -> None:
    """Por defecto, servidor independiente por HTTP con token; stdio como alternativa."""
    http = json.loads((RAIZ / ".mcp.json").read_text())["mcpServers"]["dataops"]
    assert http["type"] == "http"
    assert http["url"].endswith("127.0.0.1:8080/mcp}")
    assert http["headers"]["Authorization"] == "Bearer ${DATAOPS_MCP_TOKEN}"
    stdio = json.loads((RAIZ / ".mcp.stdio.json").read_text())["mcpServers"]["dataops"]
    assert stdio["type"] == "stdio"


def test_contenedor_mcp_minimo_privilegio() -> None:
    """El servicio mcp no recibe la contraseña del administrador y monta datos/ en solo
    lectura; el puerto solo se publica en 127.0.0.1."""
    compose = yaml.safe_load((RAIZ / "docker-compose.yml").read_text(encoding="utf-8"))
    mcp = compose["services"]["mcp"]
    assert "env_file" not in mcp
    assert not any("POSTGRES_PASSWORD" in str(v) for v in mcp["environment"].values())
    assert "./datos:/app/datos:ro" in mcp["volumes"]
    assert all(p.startswith("127.0.0.1:") for p in mcp["ports"])
