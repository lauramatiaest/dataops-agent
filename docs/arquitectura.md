# Arquitectura y decisiones técnicas

Este documento explica cómo está construido DataOps Agent y **por qué**. Cada decisión
se recoge como un ADR breve (contexto → decisión → consecuencias) para poder
defenderla y revisarla.

## 1. Visión general

```
Analista ──► Claude Code (CLAUDE.md + skills + permisos)
                 │  MCP Streamable HTTP · 127.0.0.1:8080/mcp · Authorization: Bearer
                 │  (stdio en los tests y en la evaluación)
                 ▼
          Servidor MCP "dataops" (Python, contenedor `mcp` de Docker Compose)
          ├─ herramientas de lectura ─────────► PostgreSQL (rol lector, solo lectura)
          ├─ planificar_* (sin efectos) ──────► PostgreSQL (rol lector, para estimar)
          ├─ aplicar_plan[_destructivo] ──────► PostgreSQL (rol escritor, 1 transacción)
          ├─ validaciones deterministas
          ├─ auditoría (JSONL + dataops_meta.auditoria)
          └─ MetabaseAdapter ─────────────────► Metabase (API REST) ──► PostgreSQL (lector)
```

Skills (`.claude/skills/`): `proyecto-completo` orquesta por fases (configuración,
descubrimiento, diseño con aprobación, construcción y validación) a `ingesta`,
`diagnostico-reparacion`, `modelado`, `transformacion`, `analitica-avanzada`,
`reporting` y `validacion`.

Reparto de responsabilidades:

| Componente | Responsabilidad | No hace |
|---|---|---|
| Claude Code + skills | Entender la petición, decidir pasos, explicar, preguntar | Ejecutar SQL directamente |
| Servidor MCP | Ofrecer operaciones seguras, validar, auditar | Decidir qué modelo construir |
| PostgreSQL | Datos, integridad referencial, privilegios | — |
| Metabase | Visualización | Escribir en la BD (usa el rol lector) |

### Flujo de una escritura

```mermaid
sequenceDiagram
    participant P as Persona
    participant C as Claude Code
    participant S as Servidor MCP
    participant DB as PostgreSQL
    P->>C: "Relaciona pedidos con clientes"
    C->>S: planificar_relacion(pedidos, cliente_id, clientes, cliente_id)
    S->>DB: (lector) tipos, unicidad del padre, huérfanos
    S-->>C: plan_id, SQL, impacto {huérfanos: 0, cobertura 100 %}, bloqueos: []
    C-->>P: Enseña el plan
    C->>S: aplicar_plan(plan_id)  (Claude Code pide permiso)
    S->>S: plan pendiente, no caducado, huella intacta, revalidar SQL
    S->>DB: (escritor) BEGIN; ALTER TABLE ... ADD CONSTRAINT ...; COMMIT
    S->>S: auditoría (ok, plan_id, SQL)
    S-->>C: aplicado
```

## 2. Decisiones (ADR)

### ADR-01. Claude Code como "cerebro"; el producto no llama a ninguna API de pago
- **Contexto:** restricción de coste (solo la suscripción de Claude). La API de Claude
  se factura aparte.
- **Decisión:** el agente es Claude Code; el producto es lo que Claude Code usa:
  servidor MCP, `CLAUDE.md`, skills y permisos. La evaluación se lanza con `claude -p`.
- **Consecuencias:** coste cero adicional y foco en la ingeniería del agente. A cambio,
  dependencia de Claude Code (versión, límites de uso de la suscripción) y menos
  control sobre el bucle del agente que con un SDK. `claude -p` usaría la API si
  `ANTHROPIC_API_KEY` está definida, por eso el lanzador se niega a ejecutar en ese caso.

### ADR-02. SDK oficial de MCP 2.x (`MCPServer`, antes `FastMCP`)
- **Contexto:** se pidió FastMCP. En la serie 2.x del SDK oficial, `FastMCP` se renombró a
  `MCPServer` (`mcp.server.mcpserver`); el módulo `mcp.server.fastmcp` ya no existe.
- **Decisión:** `mcp>=2.2,<3` con `MCPServer`. Herramientas tipadas con anotaciones MCP
  `read_only_hint` y `destructive_hint`. El mismo servidor admite dos transportes
  (`DATAOPS_MCP_TRANSPORTE`): Streamable HTTP para el uso interactivo (ADR-15) y stdio,
  en el que Claude Code lanza el proceso, para los tests y la evaluación.
- **Consecuencias:** es la misma API de alto nivel (decorador `@tool`, esquemas a partir
  de los tipos). Con stdio, **stdout es el canal del protocolo**: nada puede escribir en
  él (la auditoría y los avisos van a stderr; hay un test que arranca el servidor por stdio).
  La versión inicial solo usaba stdio; el cambio a HTTP no tocó ninguna herramienta.

### ADR-03. Patrón planificar → aplicar con planes inmutables
- **Contexto:** un agente que ejecuta SQL directamente puede hacer cambios que nadie ha
  revisado, y su razonamiento no es determinista.
- **Decisión:** las herramientas de escritura no existen; existen `planificar_*`, que
  devuelven un plan (SQL exacto, impacto estimado con el rol lector, bloqueos y
  advertencias), y `aplicar_plan(plan_id)`. El plan tiene huella SHA-256 de sus pasos,
  se aplica una sola vez, caduca (60 min) y no se aplica si tiene bloqueos.
- **Alternativas:** *dry-run* con `ROLLBACK` (ejecuta de verdad y puede bloquear tablas);
  dejar que el modelo "se autocontrole" (no es verificable).
- **Consecuencias:** la persona ve lo que se va a ejecutar y la auditoría enlaza cada
  cambio con su plan. Los planes se guardan en memoria del proceso: si el servidor se
  reinicia, se replanifica (aceptable: el estado de la BD puede haber cambiado).

### ADR-04. La confirmación de lo destructivo la da el cliente, no un parámetro
- **Contexto:** un parámetro `confirmar=true` no protege: el modelo puede rellenarlo solo.
- **Decisión:** herramienta separada `aplicar_plan_destructivo`; `aplicar_plan` rechaza
  planes destructivos. En `.claude/settings.json` la herramienta destructiva está en
  `permissions.ask`: según la documentación de Claude Code, una regla `ask` se evalúa
  antes que `allow` y ningún modo la aprueba automáticamente (ni `bypassPermissions`);
  en `claude -p` sin nadie que apruebe, se deniega. La auditoría marca `destructiva` y
  registra el intento pendiente (`pendiente_confirmacion`).
- **Alternativas futuras:** *elicitation* de MCP (el servidor pregunta directamente a la
  persona), que no depende de la configuración del cliente.
- **Consecuencias:** la barrera depende de que el proyecto se use con su
  `settings.json`; por eso hay además bloqueos de servidor (sin CASCADE, dependientes) y
  privilegios de BD.
- **Comprobado en la práctica** (Claude Code en modo normal): un borrado confirmado en
  la conversación se detiene en el aviso de aprobación; denegado, la tabla sigue intacta
  y el agente no reintenta; aprobado, se aplica y queda auditado como destructivo.
  Matiz descubierto en las pruebas: en el modo automático de Claude Code los
  `aplicar_plan` no destructivos no se consultan uno a uno, por eso el proyecto fija
  `"defaultMode": "default"`. Los rechazos ocurren en el cliente y no llegan a la
  auditoría del servidor (limitación conocida).

### ADR-05. Validación de SQL con sqlglot por lista de permitidos
- **Decisión:** se analiza el SQL y se exige: una sola sentencia; tipo dentro de la
  lista del modo (lectura: `SELECT`; escritura: además `CREATE TABLE/VIEW`, `INSERT`,
  `ALTER TABLE`, `DROP TABLE/VIEW`, `TRUNCATE`, `DELETE`); tablas sin calificar o del
  esquema del proyecto; ni `pg_*` ni otros esquemas o bases de datos; funciones
  peligrosas bloqueadas (`pg_read_file`, `dblink`, `query_to_xml`, `set_config`…);
  sin escrituras anidadas en CTE; sin `SELECT ... INTO`. Lo que sqlglot no entiende
  (`Command`) se rechaza. El SQL generado por el propio servidor pasa por el mismo
  validador.
- **Por qué lista de permitidos:** una lista de prohibidos siempre olvida algo.
- **Consecuencias:** es análisis estático, con límites (p. ej. nombres dentro de cadenas
  como `'pg_authid'::regclass`). Por eso es la *primera* barrera: debajo están los
  privilegios de PostgreSQL y el `statement_timeout`, que no dependen del análisis.

### ADR-06. Esquema aislado por proyecto y dos roles
- **Decisión:** cada proyecto es un esquema. `dataops_lector`: `USAGE` + `SELECT`,
  `default_transaction_read_only=on`. `dataops_escritor`: `USAGE` + `CREATE` solo en ese
  esquema; nadie puede crear en `public`. Conexiones con `search_path` = esquema del
  proyecto y `statement_timeout` fijados al abrir la sesión.
- **Consecuencias:** aunque fallara el validador, el escritor no puede tocar otros
  esquemas ni la auditoría (solo `INSERT`/`SELECT`). Crear esquemas y roles es tarea de
  administración (`dataops_agent.admin`), nunca una herramienta MCP. Metabase usa el
  rol lector.

### ADR-07. Auditoría doble: tabla de solo anexado + JSONL
- **Decisión:** cada llamada a herramienta genera un evento (id, instante UTC, actor,
  rol, esquema, herramienta, operación, plan_id, SQL, destructiva, resultado, detalle,
  duración). Se escribe en `dataops_meta.auditoria` con una conexión independiente
  (autocommit, para que quede aunque la operación haga ROLLBACK) y en un JSONL.
- **Por qué dos:** la tabla es consultable y está protegida por privilegios; el JSONL
  funciona aunque la BD falle y es lo que procesa la evaluación.
- **Consecuencias:** "quién" es el actor configurado (`DATAOPS_ACTOR`) y el rol de BD;
  en un despliegue multiusuario habría que tomarlo de la autenticación.

### ADR-08. Una conexión por operación, sin pool
- **Decisión:** cada operación abre y cierra su conexión con las opciones de sesión.
- **Consecuencias:** más simple y sin estado compartido entre llamadas; el coste
  (milisegundos por conexión local) es irrelevante para un único analista.

### ADR-09. Cargas con COPY, verificadas dentro de la transacción
- **Decisión:** el plan de carga recorre el fichero (columnas, tipos, claves repetidas o
  ya existentes) y guarda su huella SHA-256. Al aplicar: se comprueba que el fichero no
  ha cambiado, se carga con `COPY ... FROM STDIN` (los valores viajan como texto y los
  convierte PostgreSQL) y se verifica el recuento **antes del COMMIT**.
- **Consecuencias:** una carga es atómica y verificada. El `COPY` no pasa por sqlglot
  porque no es SQL del agente: lo construye el servidor con identificadores validados
  (`psycopg.sql.Identifier`).

### ADR-10. Integración directa con Metabase v0.64.2.8
- **Decisión:** un único módulo, `metabase.py`, con la clase `MetabaseAdapter` (estado,
  sincronizar, crear pregunta y crear dashboard) y nombres neutrales de visualización
  (13 tipos), de ajustes (ejes, desglose, series, formatos, objetivo) y de elementos de
  dashboard (sección, texto, pregunta con tamaño kpi/tercio/medio/ancho; sin tamaño,
  medio). La traducción a `visualization_settings` y a la rejilla de 24 columnas se hace
  en ese módulo. Imagen fijada a `metabase/metabase:v0.64.2.8`.
- **Alternativa retirada (23/11/2026):** una interfaz abstracta (`BIAdapter`) para poder
  añadir otra herramienta de BI. Con una sola implementación era complejidad sin uso; si
  hiciera falta Superset, se extraería la interfaz entonces. Por la misma razón se
  retiraron la edición de preguntas y dashboards existentes, la colocación automática
  por tipo de gráfico y la consulta de uso de objetos: un cambio de presentación se hace
  creando un dashboard nuevo.
- **Verificación de la API:** los endpoints se comprobaron en el código fuente y en la
  especificación OpenAPI (`docs/api.json`) de esa versión, no de memoria:
  `POST /api/session` (`X-Metabase-Session`) o clave de API (`X-API-Key`);
  `POST /api/card` con consulta nativa `{"type": "native", ...}` (sigue aceptándose y se
  normaliza a MBQL 5); `PUT /api/dashboard/:id` con `dashcards` (desde 0.48; fija el
  conjunto completo de tarjetas); `POST /api/dashboard/:id/cards` ya no existe
  (eliminado en 0.47); las tarjetas de título y texto son *dashcards* virtuales
  (`card_id` nulo y `visualization_settings.virtual_card`).
- **Consecuencias:** el SQL de las preguntas pasa el validador y se califica con el
  esquema (Metabase no tiene el `search_path` del servidor). Probado contra un simulador
  que reproduce los esquemas cerrados de la API y **con Metabase real** (dashboards
  creados por el agente). Lección de esa prueba: el simulador solo vale lo que se parezca
  a la versión real (el formato `stages` de las preguntas no estaba simulado y ocultó un
  fallo). No hay filtros interactivos de dashboard (trabajo futuro).

### ADR-11. Transformaciones como vistas
- **Decisión:** la lógica analítica son vistas `v_*` y los dashboards leen de ellas.
- **Consecuencias:** un cambio de negocio ("excluye cancelados") es un `CREATE OR
  REPLACE VIEW` y el dashboard se actualiza solo. Si cambian las columnas, el plan es
  destructivo (DROP + CREATE) y se bloquea si hay dependientes. Sin vistas
  materializadas: a este volumen no hacen falta.

### ADR-12. Datos sintéticos reproducibles con manifiesto
- **Decisión:** los números salen de `random.Random(semilla)` (estable entre versiones
  de Python); Faker solo pone textos. Cada conjunto incluye `manifiesto.json` con
  recuentos y sumas de control, calculados al generar.
- **Consecuencias:** los totales no dependen de la versión de Faker, y la evaluación no
  tiene cifras escritas a mano. La relación `lineas_pedido.ref_producto → productos.sku`
  tiene nombres distintos a propósito (la demo de "preguntar en vez de inventar").

### ADR-13. Evaluación A/B con el mismo modelo y el mismo modo
- **Decisión:** A = Claude Code con SQL genérico, fuera del repositorio (sin CLAUDE.md
  ni skills); B = proyecto completo. Ambas con `--strict-mcp-config`,
  `--permission-mode dontAsk` y el mismo aviso de "no hay persona disponible". El
  comparador mira el **estado real** de la BD (y la respuesta final), no el texto del
  agente.
- **Sesgos y límites:** A no tiene mecanismo de aprobación, así que en tareas de
  seguridad mide si ejecuta acciones destructivas por su cuenta; A conserva los
  privilegios de BD (por seguridad del equipo de pruebas), así que el aislamiento entre
  proyectos no lo diferencia; la configuración de usuario de Claude Code afecta a ambas.

### ADR-14. Idioma y estilo del código
- **Decisión:** identificadores, comentarios y mensajes en español, de forma
  consistente con los nombres de las herramientas pedidos (`perfilar_fichero`…);
  `MetabaseAdapter` en inglés, como se pidió en el diseño inicial. Tipado en todo el código, dataclasses inmutables donde aplica,
  ruff con línea de 100.

### ADR-15. El servidor MCP como aplicación independiente por HTTP
- **Contexto:** con stdio el servidor es un proceso hijo de Claude Code; en entornos
  reales el MCP es un servicio aparte al que se conectan los clientes.
- **Decisión:** servicio `mcp` de Docker Compose (imagen propia) con Streamable HTTP en
  `127.0.0.1:8080/mcp`, en el mismo repositorio. Seguridad: token *Bearer* obligatorio
  (≥ 24 caracteres, comparación en tiempo constante; sin token no arranca), protección
  contra *DNS rebinding* (`Host`/`Origin` locales), puerto publicado solo en `127.0.0.1`,
  contenedor sin la contraseña de administración de PostgreSQL (solo lector y escritor),
  `datos/` montado en solo lectura y usuario sin root.
- **Alternativas:** MCP remoto en la nube (fuera de alcance y obligaría a subir datos,
  PostgreSQL y Metabase); OAuth por usuario (trabajo futuro).
- **Consecuencias:** cualquier cliente MCP puede usar el servidor y todo arranca con un
  `docker compose up`. La evaluación sigue con stdio porque necesita un servidor limpio
  por tarea (los planes viven en memoria del proceso).

### ADR-16. Reparación de entregas con transformaciones declarativas
- **Decisión:** `planificar_carga_transformada` admite solo `renombrar`, `dividir` por
  10/100/1000 y `quitar_duplicados_exactos`, aplicadas al leer el fichero. La
  transformación es parte del paso del plan (y de su huella SHA-256) y de la auditoría,
  y `validar_carga(..., plan_carga=...)` compara con el origen tal y como se cargó.
- **Alternativas descartadas:** que el agente edite el fichero (en las pruebas lo hizo
  con la shell: sin plan, sin validación y sin auditoría) o permitir expresiones SQL de
  transformación (más superficie de ataque).
- **Consecuencias:** el fichero entregado no se toca nunca (además, montado en solo
  lectura). Los cambios de formato quedan fuera a propósito.

### ADR-17. Documentación del modelo (retirada el 23/11/2026)
- **Decisión inicial:** descripciones de negocio guardadas como `COMMENT ON` con plan,
  linaje desde la auditoría y diccionario en Markdown, copiados a Metabase al
  sincronizar.
- **Por qué se retiró:** no forma parte del objetivo central (construir el modelo y los
  dashboards con barreras) y añadía un módulo, tres herramientas y una skill que había
  que mantener y justificar en el plazo del TFG. Queda como trabajo futuro.

### ADR-18. Capacidades analíticas en skills, no en el núcleo
- **Decisión:** segmentación de clientes, ABC/Pareto y tendencias son plantillas SQL de la skill
  `analitica-avanzada` que se aplican con `planificar_vista`; la orquestación por fases
  es otra skill (`proyecto-completo`) con un punto de control humano para aprobar el
  diseño. Ninguna de las dos añade herramientas al servidor. La segmentación usa dos
  umbrales fijos (90 días y 5 pedidos) en lugar de quintiles RFM y no hay cohortes
  (simplificación del 23/11/2026): la regla se explica en una frase y se cambia
  editando dos números.
- **Consecuencias:** demuestra la extensibilidad del enfoque (capacidades nuevas
  editando texto) y mantiene la superficie de seguridad del servidor. Las plantillas
  tienen un test de integración que las extrae del propio `SKILL.md` y compara sus
  resultados con un cálculo independiente, para que una edición de la skill no las rompa.

## 3. Modelo de amenazas (resumen)

| Amenaza | Mitigación | Riesgo residual |
|---|---|---|
| El agente ejecuta DROP/DELETE por error o por instrucción ambigua | Planes, herramienta destructiva aparte, regla `ask`, sin CASCADE | Persona que aprueba sin leer |
| SQL que sale del esquema o lee el catálogo | Validador + privilegios + `search_path` | Funciones/casos no previstos por el validador (los frena el privilegio) |
| Consultas que bloquean la BD | `statement_timeout`, `lock_timeout`, límite de filas | — |
| Lectura de ficheros arbitrarios | Rutas confinadas a `datos/`, sin seguir enlaces fuera | — |
| El agente rodea el servidor (psql, Python, Docker) | Reglas `deny`, Bash no permitido por defecto | Las reglas de Bash no son frontera; depende de que la persona revise los comandos |
| *Prompt injection* en el contenido de los ficheros | Las barreras no dependen del modelo | El modelo puede intentar acciones no deseadas (quedan bloqueadas o pendientes de aprobación) |
| Manipulación de la auditoría | Solo INSERT/SELECT para el escritor; JSONL paralelo | Un administrador de la BD puede modificarla |
| Otro proceso local llama al servidor MCP por HTTP | Token *Bearer*, `Host`/`Origin` locales, puerto en 127.0.0.1 | Quien lea el token del entorno del usuario |
| El contenedor del MCP se ve comprometido | Sin contraseña de administración, `datos/` en solo lectura, usuario sin root | Credenciales de lector y escritor dentro del contenedor |
| Secretos en el repositorio | `.env` fuera de Git; puertos solo en 127.0.0.1 | Contraseñas por defecto si no se cambian |

## 4. Referencias

- Model Context Protocol, SDK de Python: <https://github.com/modelcontextprotocol/python-sdk>
- Claude Code: MCP, skills, permisos y modo no interactivo:
  <https://code.claude.com/docs/en/mcp>, <https://code.claude.com/docs/en/skills>,
  <https://code.claude.com/docs/en/permissions>, <https://code.claude.com/docs/en/headless>
- Metabase v0.64.2.8: código y `docs/api.json` en <https://github.com/metabase/metabase>
  (etiqueta `v0.64.2.8`); documentación de la API: <https://www.metabase.com/docs/latest/api>
- sqlglot: <https://github.com/tobymao/sqlglot>
- psycopg 3: <https://www.psycopg.org/psycopg3/docs/>
- PostgreSQL 16: privilegios (`GRANT`, `ALTER DEFAULT PRIVILEGES`) y `COPY`:
  <https://www.postgresql.org/docs/16/>
