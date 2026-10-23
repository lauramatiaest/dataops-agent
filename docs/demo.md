# Guion de la demo (5 minutos)

Objetivo: enseñar en directo que el agente **construye** (ficheros → modelo →
dashboard) y que **se controla** (pregunta en vez de inventar, pide aprobación para lo
destructivo y deja rastro).

## Preparación (antes de la sesión)

```bash
docker compose up -d && docker compose ps           # todo "healthy"
uv run python datos/generar_datos.py
uv run python scripts/configurar_metabase.py
uv run python -m dataops_agent.admin reiniciar-proyecto \
  --dsn-admin "host=localhost port=5432 user=dataops_admin password=<la de .env> dbname=dataops"
```

- Borra en Metabase los dashboards y preguntas de ensayos anteriores.
- Abre dos ventanas: terminal con `claude` en la raíz del proyecto y el navegador en
  `http://localhost:3000` con la sesión iniciada.
- Ten `/mcp` comprobado (servidor `dataops` conectado).
- **Plan B:** graba un ensayo completo en vídeo y ten preparado un estado ya cargado
  (ejecuta los pasos 1-2 en otro esquema) por si algo falla en directo.

## Guion

| Tiempo | Qué dices / escribes | Qué debe pasar | Qué señalas |
|---|---|---|---|
| 0:00-0:30 | Contexto: "Un analista quiere un dashboard a partir de ficheros. El agente es Claude Code; lo que he construido es lo que le permite hacerlo de forma segura." | — | Diagrama del README |
| 0:30-1:45 | *"Carga los cuatro ficheros de datos/demo en tablas con su clave primaria y valida las cargas."* | Perfila, propone claves, enseña planes con SQL; Claude Code pide permiso para `aplicar_plan`; tabla de validación (recuento, suma, unicidad) | El plan antes de aplicar; la validación determinista |
| 1:45-2:45 | *"Construye el modelo relacional."* | Crea `pedidos→clientes` y `lineas_pedido→pedidos` sin huérfanos; **pregunta** si `ref_producto` es la referencia a `productos.sku` (100 % de coincidencia, nombres distintos) | "No la inventa: aporta la evidencia y pregunta". Responde: *"Sí, es la referencia al producto."* |
| 2:45-3:45 | *"Crea un dashboard con las ventas mensuales y las ventas por categoría."* | Vistas `v_ventas_mensuales` y `v_ventas_categoria`, preguntas y dashboard; enlace | Abre el dashboard en el navegador |
| 3:45-4:15 | *"Excluye los pedidos cancelados."* | Redefine la vista (no destructivo, mismas columnas); enseña la cifra antes y después | Recarga el dashboard: la cifra baja |
| 4:15-4:45 | *"Borra la tabla clientes."* | Plan destructivo: dependientes (FK de pedidos) → bloqueado, sin CASCADE; si se intenta aplicar, Claude Code pide aprobación → **deniega** | Barreras en capas |
| 4:45-5:00 | *"Enséñame la auditoría de esta sesión."* | `ver_auditoria`: cada acción con plan_id, resultado y marca de destructiva | Trazabilidad; cierre |

## Ampliación (si da tiempo o en preguntas): datos estropeados

*"Carga la entrega de octubre: datos/estropeado/lineas_pedido_2026_10.csv."*

Esperado: el plan de carga queda bloqueado ("la columna 'precio' no existe… ¿quizá se
renombró 'precio_unitario'?"); con la skill `diagnostico-reparacion` el agente detecta
también los duplicados y que los precios están en céntimos, y propone un arreglo
verificado. Es un borrador: ensáyalo antes de enseñarlo.

## Si algo falla

| Síntoma | Causa probable | Arreglo rápido |
|---|---|---|
| `/mcp` no muestra `dataops` | No se aprobó el servidor o falta `uv` | `claude mcp reset-project-choices` y reabrir; comprobar `uv --version` |
| `estado_bi` sin base de datos | No se ejecutó `configurar_metabase.py` | Ejecutarlo |
| El dashboard no ve una vista nueva | Metabase no ha sincronizado | *"Sincroniza el BI"* (`sincronizar_bi`) y esperar unos segundos |
| Error de conexión a PostgreSQL | Contenedor parado o contraseñas cambiadas tras crear el volumen | `docker compose ps`; si cambiaste `.env` después del primer arranque: `docker compose down -v` y volver a empezar |
