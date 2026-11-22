"""Documentación del modelo de datos: descripción, linaje y diccionario de datos.

Tres piezas, todas deterministas (el agente aporta las descripciones de negocio, el
servidor aporta los hechos):

* :func:`describir_modelo` reúne del catálogo de PostgreSQL tablas, vistas, columnas,
  tipos, claves, relaciones, la definición de cada vista y de qué objetos lee, los
  comentarios ya existentes y el **linaje de cargas** reconstruido desde la auditoría
  (qué fichero y qué plan cargaron cada tabla, con qué transformación).
* :func:`sql_comentario` construye ``COMMENT ON`` con identificadores validados y el
  texto como literal: el texto del agente nunca se interpreta como SQL, por eso estos
  pasos no pasan por el validador (igual que el ``COPY`` de las cargas).
* :func:`diccionario_markdown` genera el diccionario de datos en Markdown con un
  diagrama entidad-relación en Mermaid.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg import sql

from dataops_agent import catalogo
from dataops_agent.config import validar_identificador

if TYPE_CHECKING:
    import psycopg

    from dataops_agent.planes import Paso

LONGITUD_MAXIMA = 500
_CREATE = re.compile(r"CREATE TABLE\s+(\w+)\.(\w+)\s*\(", re.IGNORECASE)
_COPY = re.compile(r"COPY\s+(\w+)\.(\w+)\s*\(.*?\)\s*FROM STDIN\s+--\s*datos de\s+([^\s;]+)")


# ===================================================================== catálogo
def comentario_objeto(conn: psycopg.Connection, esquema: str, objeto: str) -> str | None:
    fila = conn.execute(
        "SELECT obj_description(to_regclass(%s), 'pg_class')", (f"{esquema}.{objeto}",)
    ).fetchone()
    return fila[0] if fila else None


def comentarios_columnas(conn: psycopg.Connection, esquema: str, objeto: str) -> dict[str, str]:
    filas = conn.execute(
        """
        SELECT a.attname, col_description(a.attrelid, a.attnum)
        FROM pg_attribute a
        WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
          AND col_description(a.attrelid, a.attnum) IS NOT NULL
        """,
        (f"{esquema}.{objeto}",),
    ).fetchall()
    return {nombre: texto for nombre, texto in filas}


def lee_de(conn: psycopg.Connection, esquema: str, vista: str) -> list[str]:
    """Tablas o vistas del esquema de las que lee una vista (por pg_depend)."""
    filas = conn.execute(
        """
        SELECT DISTINCT c.relname
        FROM pg_rewrite r
        JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass
        JOIN pg_class c ON c.oid = d.refobjid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE r.ev_class = to_regclass(%s) AND c.oid <> r.ev_class AND n.nspname = %s
        ORDER BY 1
        """,
        (f"{esquema}.{vista}", esquema),
    ).fetchall()
    return [f[0] for f in filas]


def definicion_vista(conn: psycopg.Connection, esquema: str, vista: str) -> str | None:
    fila = conn.execute(
        "SELECT pg_get_viewdef(to_regclass(%s), true)", (f"{esquema}.{vista}",)
    ).fetchone()
    return fila[0].strip() if fila and fila[0] else None


# ======================================================================= linaje
def linaje_cargas(eventos: Iterable[Mapping[str, Any]], esquema: str) -> dict[str, list[dict]]:
    """Cargas aplicadas por tabla, reconstruidas desde la auditoría.

    Un plan de carga queda auditado al planificarse (con su SQL ``COPY ... -- datos de
    <fichero>`` y, si la hubo, la transformación) y otra vez al aplicarse (mismo
    ``plan_id``). Solo cuentan los planes aplicados con éxito y, si la auditoría registra
    la creación de la tabla, solo las cargas posteriores a su **última** creación: la
    auditoría es acumulativa y una tabla borrada y vuelta a crear (o un proyecto
    reiniciado) no hereda el linaje de la anterior con el mismo nombre.
    """
    eventos = list(eventos)
    aplicados = {
        e.get("plan_id"): e.get("instante")
        for e in eventos
        if str(e.get("operacion", "")).startswith("APLICAR")
        and e.get("resultado") == "ok"
        and e.get("plan_id")
    }
    creada: dict[str, str] = {}
    for e in eventos:
        instante = aplicados.get(e.get("plan_id"))
        if instante is None:
            continue
        for esq, tabla in _CREATE.findall(e.get("sql") or ""):
            if esq == esquema and instante > creada.get(tabla, ""):
                creada[tabla] = instante
    linaje: dict[str, list[dict]] = {}
    for e in eventos:
        plan_id = e.get("plan_id")
        if plan_id not in aplicados or not str(e.get("operacion", "")).startswith("PLAN CARGA"):
            continue
        for coincidencia in _COPY.finditer(e.get("sql") or ""):
            esq, tabla, fichero = coincidencia.groups()
            if esq != esquema or aplicados[plan_id] < creada.get(tabla, ""):
                continue
            detalle = e.get("detalle") or {}
            transformacion = {
                k: detalle[k]
                for k in ("renombrar", "dividir", "quitar_duplicados_exactos")
                if detalle.get(k)
            }
            linaje.setdefault(tabla, []).append({
                "fichero": fichero,
                "plan_id": plan_id,
                "aplicado": aplicados[plan_id],
                **({"transformacion": transformacion} if transformacion else {}),
            })
    return linaje


# ================================================================ modelo completo
def describir_modelo(
    conn: psycopg.Connection,
    esquema: str,
    eventos_auditoria: Iterable[Mapping[str, Any]] = (),
    uso_bi: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Descripción completa y factual del modelo del esquema."""
    base = catalogo.describir_esquema(conn, esquema)
    linaje = linaje_cargas(eventos_auditoria, esquema)
    uso = dict(uso_bi or {})
    objetos = []
    for obj in base["objetos"]:
        nombre = obj["nombre"]
        comentarios = comentarios_columnas(conn, esquema, nombre)
        entrada = {
            **obj,
            "descripcion": comentario_objeto(conn, esquema, nombre),
            "columnas": [
                {**c, "descripcion": comentarios.get(c["nombre"])} for c in obj["columnas"]
            ],
        }
        if obj["tipo"] == "vista":
            entrada["lee_de"] = lee_de(conn, esquema, nombre)
            entrada["definicion"] = definicion_vista(conn, esquema, nombre)
        else:
            entrada["cargas"] = linaje.get(nombre, [])
        if nombre in uso:
            entrada["uso_bi"] = uso[nombre]
        objetos.append(entrada)

    leido_por: dict[str, list[str]] = {}
    for obj in objetos:
        for origen in obj.get("lee_de", []):
            leido_por.setdefault(origen, []).append(obj["nombre"])
    sin_descripcion = []
    for obj in objetos:
        obj["leido_por"] = sorted(leido_por.get(obj["nombre"], []))
        if not obj["descripcion"]:
            sin_descripcion.append(obj["nombre"])
        sin_descripcion += [
            f"{obj['nombre']}.{c['nombre']}" for c in obj["columnas"] if not c["descripcion"]
        ]
    return {
        "esquema": esquema,
        "objetos": objetos,
        "pendiente_de_documentar": sin_descripcion,
        "bi_disponible": uso_bi is not None,
    }


# ================================================================== comentarios
def normalizar_texto(texto: str, donde: str) -> str:
    limpio = " ".join(str(texto).split())
    if not limpio:
        raise ValueError(f"La descripción de {donde} está vacía.")
    if len(limpio) > LONGITUD_MAXIMA:
        raise ValueError(
            f"La descripción de {donde} tiene {len(limpio)} caracteres "
            f"(máximo {LONGITUD_MAXIMA})."
        )
    return limpio


def sql_comentario(conn: psycopg.Connection, esquema: str, paso: Paso) -> sql.Composed:
    """``COMMENT ON`` seguro: identificadores validados y texto como literal."""
    objeto = validar_identificador(paso.tabla or "", "objeto")
    tipo = catalogo.tipo_objeto(conn, esquema, objeto)
    if tipo is None:
        raise ValueError(f"No existe {esquema}.{objeto}.")
    if paso.columna:
        columna = validar_identificador(paso.columna, "columna")
        destino = sql.SQL("COLUMN {}.{}.{}").format(
            sql.Identifier(esquema), sql.Identifier(objeto), sql.Identifier(columna)
        )
    else:
        destino = sql.SQL("{} {}").format(
            sql.SQL("VIEW" if tipo == "vista" else "TABLE"), catalogo.ident(esquema, objeto)
        )
    return sql.SQL("COMMENT ON {} IS {}").format(destino, sql.Literal(paso.texto))


def sql_legible(esquema: str, objeto: str, columna: str | None, texto: str, tipo: str) -> str:
    """Versión legible del ``COMMENT ON`` para enseñarla en el plan (no se ejecuta)."""
    escapado = texto.replace("'", "''")
    if columna:
        return f"COMMENT ON COLUMN {esquema}.{objeto}.{columna} IS '{escapado}'"
    clase = "VIEW" if tipo == "vista" else "TABLE"
    return f"COMMENT ON {clase} {esquema}.{objeto} IS '{escapado}'"


# =================================================================== diccionario
def _md(texto: Any) -> str:
    return str(texto).replace("|", "\\|").replace("\n", " ") if texto else ""


def diccionario_markdown(modelo: Mapping[str, Any]) -> str:
    """Diccionario de datos en Markdown, con diagrama entidad-relación en Mermaid."""
    esquema = modelo["esquema"]
    objetos = modelo["objetos"]
    tablas = [o for o in objetos if o["tipo"] == "tabla"]
    vistas = [o for o in objetos if o["tipo"] == "vista"]
    hoy = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lineas = [
        f"# Diccionario de datos — `{esquema}`",
        "",
        f"Generado automáticamente por DataOps Agent (`generar_diccionario`) el {hoy}.",
        "Los hechos (tipos, claves, filas, linaje) salen del catálogo de PostgreSQL y de la",
        "auditoría; las descripciones, de los comentarios del modelo (`COMMENT ON`).",
        "",
        f"**{len(tablas)} tablas · {len(vistas)} vistas**",
        "",
        "## Diagrama entidad-relación",
        "",
        "```mermaid",
        "erDiagram",
    ]
    for t in tablas:
        pk = set(t.get("clave_primaria") or [])
        fk = {c for f in t.get("claves_foraneas", []) for c in f["columnas"]}
        lineas.append(f"    {t['nombre']} {{")
        for c in t["columnas"]:
            marca = " PK" if c["nombre"] in pk else (" FK" if c["nombre"] in fk else "")
            tipo = re.sub(r"[^A-Za-z0-9_]", "_", c["tipo"])
            lineas.append(f"        {tipo} {c['nombre']}{marca}")
        lineas.append("    }")
    for t in tablas:
        for f in t.get("claves_foraneas", []):
            etiqueta = ", ".join(f["columnas"])
            lineas.append(f'    {f["tabla_referenciada"]} ||--o{{ {t["nombre"]} : "{etiqueta}"')
    lineas += ["```", ""]

    lineas += ["## Tablas", ""]
    for t in tablas:
        lineas += [f"### `{t['nombre']}`", ""]
        lineas += [t["descripcion"] or "_Sin descripción._", ""]
        lineas.append(f"- **Filas:** {t['filas']:,}".replace(",", "."))
        if t.get("clave_primaria"):
            lineas.append(f"- **Clave primaria:** {', '.join(t['clave_primaria'])}")
        for f in t.get("claves_foraneas", []):
            lineas.append(
                f"- **Relación:** {', '.join(f['columnas'])} → "
                f"`{f['tabla_referenciada']}`({', '.join(f['columnas_referenciadas'])})"
            )
        for carga in t.get("cargas", []):
            extra = " (con transformación)" if carga.get("transformacion") else ""
            lineas.append(
                f"- **Origen:** `{carga['fichero']}` — plan `{carga['plan_id']}`{extra}"
            )
        if t.get("leido_por"):
            lineas.append(f"- **La usan las vistas:** {', '.join(t['leido_por'])}")
        lineas += _uso_bi(t)
        lineas += ["", "| Columna | Tipo | Nulos | Descripción |", "|---|---|---|---|"]
        for c in t["columnas"]:
            lineas.append(
                f"| `{c['nombre']}` | {c['tipo']} | {'sí' if c['nulable'] else 'no'} "
                f"| {_md(c['descripcion'])} |"
            )
        lineas.append("")

    if vistas:
        lineas += ["## Vistas analíticas", ""]
    for v in vistas:
        lineas += [f"### `{v['nombre']}`", ""]
        lineas += [v["descripcion"] or "_Sin descripción._", ""]
        lineas.append(f"- **Filas:** {v['filas']:,}".replace(",", "."))
        if v.get("lee_de"):
            lineas.append(f"- **Lee de:** {', '.join(v['lee_de'])}")
        lineas += _uso_bi(v)
        lineas += ["", "| Columna | Tipo | Descripción |", "|---|---|---|"]
        for c in v["columnas"]:
            lineas.append(f"| `{c['nombre']}` | {c['tipo']} | {_md(c['descripcion'])} |")
        if v.get("definicion"):
            lineas += ["", "<details><summary>Definición SQL</summary>", "", "```sql",
                       v["definicion"], "```", "", "</details>"]
        lineas.append("")

    pendientes = modelo.get("pendiente_de_documentar") or []
    if pendientes:
        lineas += ["## Pendiente de documentar", "",
                   ", ".join(f"`{p}`" for p in pendientes), ""]
    return "\n".join(lineas)


def _uso_bi(objeto: Mapping[str, Any]) -> list[str]:
    uso = objeto.get("uso_bi") or {}
    lineas = []
    if uso.get("preguntas"):
        lineas.append(
            "- **Preguntas de BI:** " + ", ".join(p["nombre"] for p in uso["preguntas"])
        )
    if uso.get("dashboards"):
        lineas.append("- **Dashboards:** " + ", ".join(d["nombre"] for d in uso["dashboards"]))
    return lineas
