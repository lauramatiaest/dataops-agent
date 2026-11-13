# DataOps Agent

**Agente de IA extensible mediante skills y MCP que, desde Claude Code, construye
modelos de datos analíticos en PostgreSQL y dashboards en Metabase a partir de ficheros
CSV/JSON, con barreras de seguridad y auditoría.**

Prototipo del Trabajo de Fin de Grado (Ingeniería Informática, Universidad Europea,
2026-27). Objetivo TRL 4: prototipo de extremo a extremo en laboratorio, con datos
sintéticos.

> **Estado (13/11/2026):** prototipo completo y probado de extremo a extremo en
> Windows 11 con Docker Desktop: PostgreSQL 16 y Metabase reales, uso interactivo desde
> Claude Code (incluida la aprobación humana de lo destructivo) y la ampliación de
> diagnóstico y reparación de entregas defectuosas. Batería de tests contra PostgreSQL
> real en verde (también en CI). **Pendiente:** ejecutar la evaluación A/B con
> `claude -p`. Detalle en [Estado y limitaciones](#estado-y-limitaciones).

---

## Qué es

Un analista de BI le pide a Claude Code, en lenguaje natural, cosas como *"carga estos
ficheros, relaciónalos y hazme un dashboard de ventas mensuales sin cancelados"*. El
agente lo hace, pero **no con acceso libre a la base de datos**, sino a través de un
servidor MCP propio que solo ofrece operaciones seguras:

- **Lee y perfila** ficheros (tipos, nulos, cardinalidad, candidatas a clave).
- **Planifica antes de escribir:** cada cambio es un *plan* con el SQL exacto, su
  impacto y sus bloqueos, que la persona ve antes de aplicarlo.
- **No inventa relaciones:** comprueba huérfanos antes de crear una clave foránea y
  pregunta si los nombres no aportan evidencia.
- **Pide aprobación humana** para todo lo destructivo (DROP, TRUNCATE, DELETE).
- **Valida de forma determinista** cada carga (recuentos, sumas de control, unicidad).
- **Audita** cada llamada (quién, qué, cuándo, resultado, plan) en una tabla de solo
  anexado y en JSONL.
- **Publica** preguntas y dashboards en Metabase mediante un adaptador intercambiable.
- **Documenta** el modelo: descripciones de negocio guardadas como comentarios en
  PostgreSQL (con plan), linaje desde la auditoría y diccionario de datos en Markdown
  con diagrama entidad-relación (`docs/modelo/diccionario.md`).

El comportamiento del agente (cuándo perfilar, cuándo preguntar, cómo validar) vive en
`CLAUDE.md` y en **skills** (`.claude/skills/*/SKILL.md`), de modo que se puede ampliar
con nuevas capacidades sin tocar el núcleo; la skill `diagnostico-reparacion` es el
ejemplo: detecta duplicados, columnas renombradas y cambios de unidad en una entrega y
la repara **al cargarla**, sin modificar el fichero, con un plan revisable y auditado
(`planificar_carga_transformada`).

Coste: **solo la suscripción de Claude.** El producto no llama a la API de Claude ni a
ningún servicio de pago; todo lo demás es open source y local.

## Arquitectura

```mermaid
flowchart LR
    U([Analista]) -->|lenguaje natural| CC

    subgraph CC [Claude Code · suscripción]
        direction TB
        M[CLAUDE.md<br/>reglas del agente]
        S[Skills<br/>ingesta · modelado · transformación<br/>reporting · validación · diagnóstico]
        P[Permisos .claude/settings.json<br/>ask: aplicar_plan_destructivo]
    end

    CC <-->|MCP stdio| MCP

    subgraph MCP [Servidor MCP dataops · Python 3.12]
        direction TB
        H[Herramientas<br/>lectura · planificar_* · aplicar_plan · BI]
        V[Validador SQL<br/>sqlglot]
        PL[Planes<br/>planificar → aplicar]
        VA[Validaciones<br/>deterministas]
        AU[Auditoría<br/>JSONL + tabla]
        BI[BIAdapter]
        H --> V & PL & VA & AU
        H --> BI
    end

    subgraph DC [Docker Compose · local]
        PG[(PostgreSQL 16<br/>esquema por proyecto<br/>rol lector / rol escritor)]
        MB[Metabase OSS<br/>v0.64.2.8]
    end

    F[/CSV · JSON<br/>datos/]/ --> H
    PL -->|rol escritor<br/>una transacción| PG
    H -->|rol lector<br/>solo lectura| PG
    BI -->|API REST| MB
    MB -->|rol lector| PG
```

### Barreras de seguridad (defensa en profundidad)

| Capa | Qué impide | Dónde |
|---|---|---|
| Reglas del agente | Escribir sin plan, rodear bloqueos, inventar relaciones | `CLAUDE.md`, skills |
| Permisos del cliente | Aplicar algo destructivo sin que la persona lo apruebe; usar psql o leer `.env` | `.claude/settings.json` |
| Validador SQL | Varias sentencias, sentencias fuera de lista, salir del esquema, catálogo, funciones peligrosas | `seguridad/validador_sql.py` |
| Patrón de planes | Ejecutar algo distinto de lo revisado, aplicar dos veces, planes caducados o bloqueados | `planes.py`, `aplicador.py` |
| Transacción y verificación | Cargas parciales, ficheros cambiados entre planificar y aplicar | `aplicador.py` |
| Rutas de ficheros | Leer fuera de `datos/` (incluidos enlaces simbólicos) | `ficheros.py` |
| Privilegios de PostgreSQL | Lector sin escritura; escritor solo en su esquema; auditoría sin UPDATE/DELETE | `sql/roles.sql`, `sql/proyecto.sql` |
| Sesión de PostgreSQL | Consultas eternas (`statement_timeout`), `search_path` cambiado | `conexion.py` |
| Auditoría | Acciones sin rastro | `auditoria.py` |

Decisiones justificadas en [docs/arquitectura.md](docs/arquitectura.md).

## Requisitos

- **Docker** con Docker Compose v2 (Docker Desktop en Windows/macOS).
- **[uv](https://docs.astral.sh/uv/)** (gestiona Python 3.12 y las dependencias).
- **Claude Code** con sesión iniciada con tu suscripción (`claude` y después `/login`).
- Git. Unos 3 GB libres para las imágenes.

## Instalación paso a paso

```bash
# 1. Código y configuración
git clone <url-de-tu-repo> dataops-agent && cd dataops-agent
cp .env.example .env          # cambia las contraseñas (solo [A-Za-z0-9_-.])

# 2. PostgreSQL + Metabase
docker compose up -d
docker compose ps             # espera a que ambos estén "healthy" (Metabase tarda 1-3 min)

# 3. Python y dependencias
uv sync --extra dev

# 4. Datos sintéticos (semilla fija)
uv run python datos/generar_datos.py

# 5. Metabase: cuenta de administración y conexión a PostgreSQL con el rol lector
uv run python scripts/configurar_metabase.py

# 6. Tests (unitarios; con DATAOPS_TEST_DSN también los de integración)
uv run pytest
export DATAOPS_TEST_DSN="host=127.0.0.1 port=5432 user=dataops_admin password=cambia_admin_123 dbname=dataops"
uv run pytest -v
```

Los tests de integración crean roles `dataops_*_test` y el esquema `proyecto_test` en
la misma base de datos; no tocan `proyecto_demo`.

### En Windows (PowerShell)

Probado en Windows 11 con Docker Desktop y Windows PowerShell 5.1. Diferencias con los
comandos anteriores:

| Bash | PowerShell |
|---|---|
| `cmd1 && cmd2` | `cmd1; cmd2` (PowerShell 5.1 no admite `&&`) |
| `cp .env.example .env` | `Copy-Item .env.example .env` |
| `export VAR=valor` | `$env:VAR="valor"` (solo dura mientras la ventana está abierta) |
| `grep texto` | `Select-String texto` |

DSN de los tests leyendo usuario y contraseña del propio `.env`:

```powershell
$pw = (Select-String -Path .env -Pattern '^POSTGRES_PASSWORD=').Line.Split('=',2)[1]
$us = (Select-String -Path .env -Pattern '^POSTGRES_USER=').Line.Split('=',2)[1]
$env:DATAOPS_TEST_DSN="host=127.0.0.1 port=5432 user=$us password=$pw dbname=dataops"
uv run pytest
```

Problemas conocidos y solución:

- **Usa `127.0.0.1`, no `localhost`.** En Windows `localhost` se resuelve primero a IPv6
  (`::1`) y Docker solo publica los puertos en `127.0.0.1`: cada conexión espera a que
  caduque el intento por IPv6 (minutos por llamada). Los valores por defecto ya usan
  `127.0.0.1`.
- **Puerto 5432 ocupado o reservado** (`ports are not available ... forbidden by its
  access permissions`): pon otro en `DATAOPS_PG_PUERTO` (p. ej. `5433`) y úsalo también
  en el DSN. Para ver los rangos reservados:
  `netsh interface ipv4 show excludedportrange protocol=tcp`.
- **Credenciales cambiadas después del primer arranque:** el script de inicialización
  solo se ejecuta con el volumen vacío; `docker compose down -v` y vuelve a levantar.

## Conectarlo a Claude Code

El servidor MCP es una **aplicación independiente**: corre como servicio `mcp` de
`docker compose` y Claude Code se conecta a ella por HTTP (`http://127.0.0.1:8080/mcp`),
con un token en cada petición.

- `.mcp.json` declara el servidor `dataops` por HTTP con la cabecera
  `Authorization: Bearer ${DATAOPS_MCP_TOKEN}`.
- `CLAUDE.md` y `.claude/skills/` definen el comportamiento del agente.
- `.claude/settings.json` define los permisos.

**1. Token (una sola vez).** Genera uno y guárdalo como variable de entorno de tu
usuario: la leen tanto `docker compose` (para el servidor) como Claude Code (para la
cabecera de `.mcp.json`).

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# Linux/macOS: añade a ~/.bashrc o ~/.zshrc
export DATAOPS_MCP_TOKEN="<token>"
# Windows (PowerShell): persistente para el usuario; abre una terminal NUEVA después
setx DATAOPS_MCP_TOKEN "<token>"
```

**2. Levantar el servidor** (junto a PostgreSQL y Metabase):

```bash
docker compose up -d --build
docker compose logs mcp     # "Uvicorn running on http://0.0.0.0:8080"
```

Sin token, el servicio `mcp` falla al arrancar y `docker compose logs mcp` explica cómo
generarlo.

**3. Abrir Claude Code** en la raíz del repositorio:

```bash
claude
```

1. Claude Code pregunta si confías en el servidor MCP del proyecto: acepta `dataops`.
2. Escribe `/mcp` y comprueba que `dataops` aparece conectado con sus herramientas.
3. Prueba: *"Lista los ficheros de datos y perfila demo/pedidos.csv"*.

**Alternativa sin Docker (stdio):** `.mcp.stdio.json` lanza el servidor como proceso
hijo de Claude Code (`claude --mcp-config .mcp.stdio.json --strict-mcp-config`). Es el
transporte que usan los tests y la evaluación, que arranca un servidor limpio por tarea.

Tras cambiar código del servidor: `docker compose up -d --build mcp`.

**Usa el modo de permisos normal** (sin *auto mode* en la barra inferior; se cambia con
Shift+Tab). El proyecto lo fija por defecto (`defaultMode` en `.claude/settings.json`),
pero si la persona cambia de modo durante la sesión, las reglas `ask` pueden dejar de
pedir aprobación como se describe aquí.

Permisos: las herramientas de lectura y planificación están permitidas;
`aplicar_plan` y las de BI piden permiso la primera vez (puedes permitirlas para la
sesión); `aplicar_plan_destructivo` **pide aprobación siempre**.

## Guion de la demo (5 minutos)

1. *"Carga los cuatro ficheros de `datos/demo` con su clave primaria y valida las
   cargas."* → perfiles, planes con SQL, validación con recuentos y sumas.
2. *"Construye el modelo relacional."* → crea `pedidos→clientes` y
   `lineas_pedido→pedidos`, y **pregunta** por `ref_producto`↔`sku` con la evidencia.
3. *"Crea un dashboard con las ventas mensuales y las ventas por categoría."* →
   vistas `v_*`, preguntas y dashboard en Metabase.
4. *"Excluye los pedidos cancelados."* → se redefine la vista y el dashboard cambia.
5. *"Borra la tabla clientes."* → plan destructivo bloqueado por dependientes y
   aprobación requerida; se enseña la auditoría.

Guion completo con tiempos, frases y plan B: [docs/demo.md](docs/demo.md).

## Evaluación

Banco de 20 tareas con resultado conocido y comparación **A** (Claude Code con SQL
directo, sin barreras) frente a **B** (Claude Code + skills + MCP propio), lanzada con
`claude -p`. Preparada y probada con un Claude simulado; **pendiente de ejecutar**.
Ver [eval/README.md](eval/README.md).

## Estructura del repositorio

```
├── CLAUDE.md                     # reglas del agente
├── .claude/skills/<skill>/SKILL.md
├── .claude/settings.json         # permisos (allow / ask / deny)
├── .mcp.json                     # servidor MCP del proyecto (HTTP + token)
├── .mcp.stdio.json               # alternativa: servidor como proceso hijo (stdio)
├── Dockerfile                    # imagen del servidor MCP (servicio mcp)
├── docker-compose.yml            # PostgreSQL 16 + Metabase v0.64.2.8
├── docker/postgres/init/         # inicialización (roles, auditoría, esquema)
├── src/dataops_agent/
│   ├── servidor.py               # herramientas MCP (capa fina + auditoría)
│   ├── seguridad/validador_sql.py
│   ├── planes.py · planificador.py · aplicador.py
│   ├── perfilado.py · ficheros.py · validaciones.py · catalogo.py
│   ├── auditoria.py · conexion.py · config.py · admin.py
│   ├── bi/base.py (BIAdapter) · bi/metabase.py (MetabaseAdapter)
│   └── sql/roles.sql · sql/proyecto.sql
├── datos/generar_datos.py        # datos sintéticos + variante estropeada
├── scripts/configurar_metabase.py
├── eval/                         # tareas YAML, comparador, lanzador A/B
├── tests/                        # pytest (unitarios + integración)
└── docs/arquitectura.md · docs/demo.md
```

## Estado y limitaciones

**Probado en Windows 11** (Docker Desktop, Python 3.12, Claude Code 2.x): `docker compose
up`, Metabase real (configuración, preguntas y dashboards creados por el agente), uso
interactivo desde Claude Code con una batería manual de pruebas (construir el modelo,
barreras, aprobación humana de lo destructivo, auditoría y reparación de una entrega
defectuosa) y CI en verde.

**Probado** (en Linux, Python 3.13 y PostgreSQL 16.15 nativo): validador SQL, planes,
planificadores, aplicador, auditoría (JSONL y tabla, solo anexado), privilegios de los
roles, validaciones, generador de datos (la carga real cuadra al céntimo con el
manifiesto), servidor MCP por cliente en proceso y por stdio, adaptador de Metabase
contra un simulador de su API, comparador y lanzador de la evaluación con un Claude
simulado.

**Sin probar:** la evaluación A/B con `claude -p` real.

**Limitaciones conocidas:**
- El validador es análisis estático: es la primera barrera, no la única (los
  privilegios de PostgreSQL son la que no se puede saltar).
- Las reglas `deny` de Bash de Claude Code no son una frontera de seguridad: con Bash
  permitido, un comando distinto podría saltárselas. Por eso Bash no está permitido por
  defecto y Claude Code pide aprobación para cada comando.
- Los planes viven en memoria del proceso del servidor MCP (si se reinicia, se
  replanifica).
- El contenido de los ficheros llega al modelo (perfiles, ejemplos): un CSV con
  instrucciones maliciosas en sus celdas es un riesgo de *prompt injection* que mitigan
  las barreras, pero no elimina.
- La reparación de entregas solo admite tres transformaciones (renombrar, dividir por
  10/100/1000 y quitar duplicados exactos); los cambios de formato se piden al origen.
- La auditoría del servidor no registra los rechazos que la persona hace en Claude Code
  (ocurren antes de que la llamada llegue al servidor).

## Uso de IA en el desarrollo

Este prototipo se ha desarrollado con ayuda de **Claude Code** (Anthropic), que ha
generado gran parte del código y la documentación bajo la dirección de la autora. Cada
commit lo declara con el trailer `Co-Authored-By: Claude`. El uso de IA en el TFG
está sujeto a la autorización del tutor según el plan de trabajo de la asignatura.

## Licencia

MIT. Ver [LICENSE](LICENSE).
