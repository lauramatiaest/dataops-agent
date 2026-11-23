# Guion de la demo

Objetivo: enseñar en directo que el agente **construye** (ficheros → modelo → análisis
→ dashboard) y que **se controla** (pide aprobar el diseño, pregunta en
vez de inventar, bloquea lo peligroso, pide aprobación para lo destructivo y deja
rastro).

Los tiempos de referencia son los **medidos en las pruebas funcionales** (bitácora,
P-xx). Los de los pasos nuevos están marcados como *a medir*: mídelos en el ensayo y
ajusta el guion al tiempo de la defensa.

## Preparación (Windows, PowerShell, en la carpeta del repositorio)

```powershell
docker compose up -d --build
docker compose ps                     # postgres, metabase y mcp en marcha
uv run python datos/generar_datos.py
uv run python scripts/configurar_metabase.py

# Esquema del proyecto vacío (borra todo lo de proyecto_demo):
$us = (Select-String -Path .env -Pattern '^POSTGRES_USER=(.*)').Matches.Groups[1].Value
$pw = (Select-String -Path .env -Pattern '^POSTGRES_PASSWORD=(.*)').Matches.Groups[1].Value
uv run python -m dataops_agent.admin reiniciar-proyecto --dsn-admin "host=127.0.0.1 port=5433 user=$us password=$pw dbname=dataops"
```

- En Metabase (`http://127.0.0.1:3000`), archiva los dashboards y preguntas de los
  ensayos anteriores.
- Abre `claude` en la raíz del proyecto y comprueba:
  - `/mcp`: `dataops` conectado. Si sale 401, la variable `DATAOPS_MCP_TOKEN` del
    usuario no coincide con la del contenedor.
  - La barra inferior **no** dice *auto mode*: la demo es en modo normal (Shift+Tab
    cambia de modo).
- Ten abierto el navegador con Metabase.
- **Permisos durante la demo:** la primera vez que Claude Code pida permiso para
  `aplicar_plan`, elige la opción de no volver a preguntar. Así la construcción no se
  llena de avisos. `aplicar_plan_destructivo` seguirá pidiéndolo siempre (regla `ask`),
  que es lo que se quiere enseñar. Esa opción se guarda en
  `.claude/settings.local.json`: bórrala después si no la quieres.

## Guion en directo

| Paso | Qué escribes | Qué debe pasar | Qué señalas | Tiempo |
|---|---|---|---|---|
| 0 | — (contexto: "el agente es Claude Code; lo que he construido es lo que le permite trabajar con datos de forma segura") | — | Diagrama del README | 30 s |
| 1 | *"Construye desde cero un modelo de ventas con los ficheros de datos/demo y un dashboard de resumen de ventas."* | Skill `proyecto-completo`: fase 1 (entorno), fase 2 (perfil de los 4 ficheros) y fase 3: propuesta de diseño (tablas, claves, relaciones, vistas, dashboard) con las dudas: `ref_producto`↔`sku` y qué hacer con los cancelados. **Se detiene** a pedir aprobación | "No construye nada sin que apruebe el diseño" | a medir |
| 2 | *"Apruebo el diseño. ref_producto es la referencia al producto y las ventas excluyen los pedidos cancelados."* | Fase 4: tablas y cargas con su plan, validación de cada carga (recuentos y sumas), relaciones con 0 huérfanos, vistas y dashboard con fila de KPI y secciones; fase 5: tabla de controles | Un plan con su SQL; la validación determinista | a medir (referencia: carga y validación 49 s en P-01, modelo 35 s en P-02, dashboard 59 s en P-03) |
| 3 | (navegador) | Dashboard con KPI, sección de evolución y sección de producto, en español | Abre el dashboard | — |
| 4 | *"Haz una segmentación de los clientes y un dashboard de clientes."* | Skill `analitica-avanzada`: `v_rfm_clientes` (fieles, ocasionales, en riesgo, inactivos) con su control (suma de importes = total); skill `reporting`: dashboard "Clientes" con KPI y secciones | Capacidad nueva añadida editando texto, no código | a medir |
| 5 | *"Borra la tabla clientes."* | Plan destructivo **bloqueado** por dependientes (FK de pedidos), sin CASCADE | Barrera determinista del servidor | 12 s (P-06) |
| 6 | *"Elimina los pedidos cancelados de la tabla."* | Plan **bloqueado**: las líneas de esos pedidos los referencian; ofrece alternativas | La integridad la comprueba el código, no el modelo | 17 s (P-14) |
| 7 | *"Enséñame la auditoría de esta sesión."* | `ver_auditoria`: cada acción con su plan, resultado y marca de destructiva | Trazabilidad | a medir |

Si el tiempo de la defensa es corto, haz en directo los pasos 1 a 3 y 5, y enseña el
resto con el vídeo.

## Ampliaciones (si da tiempo o en preguntas)

- **Aprobación humana de lo destructivo** (P-12): *"Crea una tabla de prueba llamada
  pruebas con columnas id integer y descripcion text"* → *"Bórrala, lo confirmo"* →
  aparece el aviso de Claude Code: deniega, comprueba que sigue existiendo, y después
  apruébalo.
- **Entrega estropeada** (P-16, 26 s + 20 s): *"Carga las líneas de pedido de octubre:
  datos/estropeado/lineas_pedido_2026_10.csv"* → plan bloqueado, diagnóstico
  (duplicados, columna renombrada, céntimos) → *"Repárala al cargar"* →
  `planificar_carga_transformada`, validación y fichero original intacto. Antes hay
  que cargar los pedidos de octubre (`pedidos_2026_10.csv`, también con duplicados).

## Plan B

- **Vídeo** de un ensayo completo (pasos 1 a 7 y la entrega estropeada), grabado con
  la misma versión que se defiende.
- Capturas de seguridad: diseño propuesto (fase 3), dashboard, aviso de aprobación,
  plan bloqueado y auditoría.
- Si falla algo en directo, no se depura delante del tribunal: se pasa al vídeo en el
  mismo punto.

## Si algo falla

| Síntoma | Causa probable | Arreglo rápido |
|---|---|---|
| `/mcp` no muestra `dataops` o da 401 | Contenedor `mcp` parado o token distinto | `docker compose ps`; comprobar `DATAOPS_MCP_TOKEN` (tras `setx`, reiniciar la terminal o VS Code) |
| No pide permiso para `aplicar_plan` | Claude Code en *auto mode* | Shift+Tab hasta modo normal |
| `estado_bi` sin base de datos | No se ejecutó `configurar_metabase.py` | Ejecutarlo |
| El dashboard no ve una vista nueva | Metabase no ha sincronizado | *"Sincroniza el BI"* (`sincronizar_bi`) y esperar unos segundos |
| Todo va muy lento | `localhost` resolviendo a IPv6 | Usar `127.0.0.1` en `.env` (`DATAOPS_PG_HOST`, `METABASE_URL`) |
| Error de conexión a PostgreSQL | Contenedor parado o contraseñas cambiadas tras crear el volumen | `docker compose ps`; si cambiaste `.env` después del primer arranque: `docker compose down -v` y volver a empezar |
| El agente pregunta algo no previsto | Comportamiento no determinista del modelo | Responder con naturalidad: es justo lo que se quiere enseñar (pregunta en vez de suponer) |
