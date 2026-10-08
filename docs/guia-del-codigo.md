# Guía del código (para entenderlo y defenderlo)

Esta guía recorre las piezas que más probablemente te pregunten. Para cada una: qué
problema resuelve, cómo lo hace y qué decir si te piden justificarlo. Al final hay
ejercicios para hacer el código tuyo antes de la defensa.

## Mapa mental en una frase

> El modelo **decide**, el servidor **ejecuta solo lo seguro**, la base de datos
> **impone lo que no se puede saltar** y todo **queda registrado**.

Recorrido de una petición: `servidor.py` (herramienta MCP) → `planificador.py` (crea un
`Plan` sin tocar nada) → la persona lo ve → `aplicador.py` (lo ejecuta en una
transacción) → `auditoria.py` (lo registra). El SQL pasa siempre por
`seguridad/validador_sql.py`.

---

## 1. El validador SQL (`src/dataops_agent/seguridad/validador_sql.py`)

**Problema:** el modelo puede generar SQL peligroso (por error o por una instrucción
maliciosa en los datos).

**Cómo:** `sqlglot.parse()` convierte el texto en un árbol (AST) y el validador
recorre ese árbol con cinco reglas:

1. `len(sentencias) == 1` → bloquea `SELECT 1; DROP TABLE x`.
2. `_comprobar_tipo`: lista de **permitidos** por modo. Lo desconocido (`exp.Command`)
   se rechaza.
3. `_comprobar_tablas`: cada `exp.Table` debe ir sin esquema o con el del proyecto;
   se rechazan los `pg_*` sin calificar porque `pg_catalog` está siempre en el
   `search_path` implícitamente (detalle de PostgreSQL que conviene saber explicar).
4. `_comprobar_funciones`: bloquea `pg_*`, `dblink*`, `lo_*`, `query_to_xml`…
   `query_to_xml('select ...')` es interesante: ejecuta SQL que va **dentro de una
   cadena**, así que el validador no lo vería; por eso se prohíbe la función.
5. `_es_destructiva`: marca DROP, TRUNCATE, DELETE y ALTER … DROP.

**Si te preguntan "¿y si sqlglot se equivoca?":** es la primera barrera, no la única.
Debajo están los privilegios de PostgreSQL (`sql/roles.sql`, `sql/proyecto.sql`) y el
`statement_timeout` (`conexion.py`). El test `test_escritor_limitado_a_su_esquema`
demuestra que la BD rechaza `CREATE TABLE public.t` aunque nadie lo valide.

**Por qué lista de permitidos y no de prohibidos:** con prohibidos basta con olvidar
una sentencia (p. ej. `COPY ... TO PROGRAM`) para tener un agujero.

## 2. Planificar → aplicar (`planes.py`, `planificador.py`, `aplicador.py`)

**Problema:** si el agente escribe directamente, nadie revisa lo que hace.

**`Plan` (planes.py):** dataclass con los pasos (`Paso`: SQL o carga), impacto,
bloqueos y `huella` = SHA-256 de los pasos. `AlmacenPlanes.obtener_para_aplicar`
comprueba cinco cosas: existe, está pendiente, no ha caducado, la huella coincide y no
tiene bloqueos. Pregunta típica: *"¿para qué la huella si el plan está en memoria?"* →
garantiza que lo que se ejecuta es exactamente lo que se mostró; si alguien (o un error
de programación) modificara los pasos, no se aplicaría.

**Planificador:** cada método consulta la BD con el rol **lector** para estimar el
impacto. El caso estrella es `relacion()`:
- tipos compatibles (`_familia`),
- la columna padre es PK o UNIQUE (`catalogo.conjuntos_unicos`),
- **huérfanos** con `validaciones.huerfanos()`: una consulta `NOT EXISTS` que cuenta
  filas hijas cuyo valor no está en el padre, más una muestra de valores.
Si hay huérfanos, el plan sale con `bloqueos` y no se puede aplicar: el agente tiene que
preguntar. Así "no inventar relaciones" no depende de que el modelo se porte bien.

**Aplicador:** `with self.gestor.escritura() as conn:` abre UNA transacción; si algo
falla dentro, psycopg hace ROLLBACK al salir del `with` con excepción. En las cargas,
`_cargar` cuenta filas antes y después del `COPY` y lanza `ErrorVerificacion` si no
cuadra **antes del COMMIT**. También comprueba que el fichero no ha cambiado (huella).

**Destructivo:** `aplicar(..., permitir_destructivo=False)` lanza `RequiereConfirmacion`.
Solo la herramienta `aplicar_plan_destructivo` lo permite, y esa herramienta está en
`permissions.ask` de `.claude/settings.json`: la aprobación la pide Claude Code a la
persona, no un parámetro que el modelo pueda rellenar solo (ADR-04).

## 3. Auditoría (`auditoria.py`, `conexion.py`, `sql/roles.sql`)

- `Servicio.auditado()` en `servidor.py` envuelve **todas** las herramientas: mide el
  tiempo, captura los errores esperables y registra un `EventoAuditoria`.
- Se escribe en JSONL y en `dataops_meta.auditoria`, con una conexión **aparte en
  autocommit**: si la operación hace ROLLBACK, el evento queda igualmente.
- El rol escritor tiene `INSERT, SELECT` sobre la tabla y nada más: no puede borrar ni
  modificar el histórico (lo demuestra `test_auditoria_solo_anexado`).
- Un fallo al escribir en BD no rompe la operación y se avisa por **stderr**: stdout es
  el canal del protocolo MCP por stdio.

## 4. El servidor MCP (`servidor.py`)

- `crear_servidor(cfg, bi)` construye un `MCPServer` y registra las herramientas con
  `@servidor.tool(annotations=...)`. Los tipos de los parámetros (`str`, `list[str]`,
  `ColumnaDef` de pydantic) generan el esquema JSON que ve el modelo.
- Las herramientas son finas: validan, delegan y auditan. La lógica está en módulos que
  se prueban sin MCP.
- `main()` carga `.env`, cambia al directorio del proyecto y llama a `run()` (stdio).
- Test clave: `tests/test_servidor_integracion.py` usa un cliente MCP real en proceso y
  reproduce el guion de la demo; `test_stdio_integracion.py` arranca el servidor como
  subproceso, como lo hace Claude Code.

## 5. Adaptador BI (`bi/base.py`, `bi/metabase.py`)

- `BIAdapter` es una clase abstracta (patrón **adaptador**): el servidor no sabe que
  existe Metabase. Superset = otra subclase.
- Detalle que demuestra que se verificó la API: `PUT /api/dashboard/:id` **reemplaza**
  todas las tarjetas, así que `anadir_a_dashboard` primero lee las existentes y las
  reenvía con las nuevas (id negativo = nueva). Si no se hiciera, añadir un gráfico
  borraría los demás.
- El SQL de las preguntas pasa por el validador y por `calificar()`, que añade el
  esquema a las tablas: Metabase ejecuta con su propia conexión, sin nuestro
  `search_path`.

## 6. Validación determinista (`validaciones.py`)

Recuento frente al origen, sumas de control con `Decimal` (no `float`: 0.1 + 0.2 ≠ 0.3),
unicidad y huérfanos. Todo devuelve `Comprobacion(nombre, ok, esperado, obtenido)` para
que el resultado sea explicable. Idea para la defensa: *el modelo no "opina" si la carga
está bien; lo calcula el código y el modelo lo comunica.*

## 7. Skills y CLAUDE.md

- `CLAUDE.md`: reglas generales (siempre en contexto).
- Skills: procedimientos que Claude Code carga cuando la tarea encaja con su
  `description`. La de `modelado` tiene la tabla de decisión "nombre coherente ×
  huérfanos → acción", que es la que produce el "pregunta en vez de inventar".
- `tests/test_agente.py` comprueba que skills, CLAUDE.md y permisos solo mencionan
  herramientas que existen: un error de nombre haría que el agente fallara en silencio.

## 8. Datos y evaluación

- `datos/generar_datos.py`: números con `random.Random(semilla)`, textos con Faker; el
  manifiesto es la verdad de la evaluación (ADR-12).
- `eval/comparador.py` mira el **estado real** de la BD, no lo que dice el agente;
  `resolver_valor` evalúa expresiones del manifiesto con un evaluador seguro basado en
  `ast` (nunca `eval()`).
- `eval/run_eval.py`: prepara, lanza `claude -p`, compara y resume. Probado con un
  Claude falso (`test_lanzador_con_claude_simulado`).

---

## Preguntas probables del tribunal

1. ¿Qué aporta esto frente a darle a Claude Code acceso con psql? → Barreras que no
   dependen del modelo + trazabilidad + medición (evaluación A/B).
2. ¿Qué pasa si el modelo ignora CLAUDE.md? → Las reglas importantes también están en
   código (bloqueos, validador, aplicar sin destructivos) y en la BD (privilegios).
3. ¿Cómo sabes que el validador es correcto? → 60 casos en `test_validador_sql.py` y
   defensa en profundidad; sus límites están documentados.
4. ¿Por qué MCP? → Estándar abierto: el mismo servidor sirve a otros clientes MCP.
5. ¿Cómo has usado la IA en el desarrollo? → Declarado en el README y en cada commit;
   según lo autorizado por el tutor.

## Ejercicios para hacerlo tuyo (recomendado antes de enseñarlo)

1. Quita `"pg_"` de `_PREFIJOS_PROHIBIDOS` en el validador, ejecuta
   `test_validador_sql.py` y mira qué casos dejan de bloquearse. Explica por qué
   enumerar funciones una a una es más frágil que prohibir la familia.
2. Añade una comprobación nueva a `validaciones.py` (p. ej. "fechas fuera de rango") con
   su test y su herramienta MCP.
3. Cambia la caducidad de los planes a 5 minutos con una variable de entorno y escribe
   el test.
4. Escribe tú la herramienta `trazar_linaje` de la skill `diagnostico-reparacion`.
5. Ejecuta la demo completa en tu portátil y anota en `docs/` lo que falle y cómo lo
   arreglas: ese diario es una buena prueba de autoría.
