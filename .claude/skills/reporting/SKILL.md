---
name: reporting
description: Crea preguntas y dashboards en la herramienta de BI (Metabase) sobre vistas analíticas del proyecto - KPI, secciones, gráficos con formato y plantillas de dashboard (resumen de ventas, clientes, producto). Úsala cuando la persona pida un dashboard, un gráfico, un informe visual o publicar métricas.
---

# Reporting: preguntas y dashboards

## Antes de empezar

1. `estado_bi`. Si no está disponible o no tiene `base_datos_id`, para y explica cómo
   arrancarlo (`docker compose up -d` y `python scripts/configurar_metabase.py`).
2. Asegúrate de que existen vistas `v_*` para cada métrica (skill `transformacion`; para
   RFM, ABC o cohortes, skill `analitica-avanzada`). Las preguntas leen de vistas para
   que un cambio de lógica no obligue a rehacerlas.
3. Si acabas de crear vistas, `sincronizar_bi`.
4. Antes de diseñar, consulta con `consultar_solo_lectura` las columnas y el rango de
   fechas de cada vista: los ajustes citan columnas por su nombre exacto.

## Elegir la visualización

| Qué quieres mostrar | `visualizacion` | Ajustes típicos |
|---|---|---|
| Una cifra (ventas totales, nº de pedidos) | `numero` | `series: [col]`, `formatos` |
| Una cifra y su variación frente al periodo anterior | `tendencia` | SQL con fecha y cifra ordenado por fecha; `eje_x`, `series` |
| Una cifra frente a una meta que da la persona | `progreso` | `objetivo` (obligatorio) |
| Evolución en el tiempo | `lineas` o `area` | `eje_x: mes`, `series`, `titulo_y` |
| Dos magnitudes en el tiempo (importe y pedidos) | `combinado` | la primera serie va en barras y las demás en línea |
| Comparar categorías | `barras` / `barras_horizontales` (nombres largos o top N) | `mostrar_valores` |
| Composición por categoría y periodo (importe por mes y canal) | `barras` o `area` | `eje_x: mes`, `desglose: canal`, `apilado: true` |
| Reparto de un total (≤ 6 categorías) | `tarta` | `eje_x`, `series` |
| Pasos sucesivos (visitas → pedidos) | `embudo` | |
| Aportación de cada parte a un total | `cascada` | |
| Relación entre dos medidas por elemento | `dispersion` | `eje_x`, `series` |
| Detalle o ranking con varias columnas | `tabla` | `formatos` |

Ajustes (`ajustes` en `crear_pregunta` y `actualizar_pregunta`): `eje_x`, `desglose`, `series`,
`titulo_x`, `titulo_y`, `apilado`, `mostrar_valores`, `formatos` y `objetivo`.
- `formatos`: `{columna: moneda | porcentaje | entero | decimal}`. Usa `moneda` (euros)
  **solo si la moneda está confirmada** en la documentación o por la persona; si no,
  `decimal` y la palabra "Importe" en el título. `porcentaje` añade " %" y espera
  valores 0-100, que es como los calculan las vistas del proyecto.
- `tendencia` compara el último periodo con el anterior: si el último mes está
  incompleto (compruébalo con la fecha máxima), exclúyelo en el SQL y dilo en la
  descripción. Si no, la variación engaña.
- No inventes objetivos para `progreso`: pídeselos a la persona.

## Diseñar el dashboard

Un dashboard responde a una pregunta de negocio y se lee de arriba abajo:

1. **Texto de cabecera** (opcional, una o dos frases): qué responde el panel y las
   reglas de negocio que aplican sus vistas (p. ej. "excluye pedidos cancelados").
   **Sin cifras en los textos**: cambian con los datos y el texto no se actualiza.
2. **Fila de KPI**: de 2 a 4 tarjetas `numero`/`tendencia`/`progreso`, tamaño `kpi`.
3. **Secciones** (`{"seccion": "..."}`), cada una con 2-4 gráficos relacionados:
   `medio` para gráficos que van de dos en dos, `tercio` para tres pequeños y `ancho`
   para series temporales largas y tablas.
4. Como mucho unas 10-12 preguntas. Si sale más, propone dos dashboards.

Crea primero todas las preguntas y después el dashboard con `elementos`, en orden de
lectura:

```json
crear_dashboard(nombre="Resumen de ventas", descripcion="...", elementos=[
  {"texto": "Ventas de la tienda online. Las cifras excluyen pedidos cancelados."},
  {"pregunta_id": 101, "tamano": "kpi"}, {"pregunta_id": 102, "tamano": "kpi"},
  {"pregunta_id": 103, "tamano": "kpi"}, {"pregunta_id": 104, "tamano": "kpi"},
  {"seccion": "Evolución"},
  {"pregunta_id": 105, "tamano": "ancho"},
  {"seccion": "Producto"},
  {"pregunta_id": 106, "tamano": "medio"}, {"pregunta_id": 107, "tamano": "medio"}
])
```

`pregunta_ids=[...]` sigue valiendo para algo rápido: KPI pequeños, tablas a todo el
ancho y el resto a media anchura, sin secciones.

## Plantillas

Adáptalas a las vistas que existan; si falta una vista, créala antes (o quita esa
pieza y dilo). Nombres de pregunta en lenguaje de negocio.

**Resumen de ventas** (sobre `v_ventas_mensuales`, `v_ventas_por_categoria` y, si
existe, una vista por canal):
- KPI: ventas totales (`numero`), pedidos (`numero`, `entero`), ticket medio
  (`numero`: `sum(importe) / sum(pedidos)`), ventas del último mes completo frente
  al anterior (`tendencia`).
- Sección "Evolución": importe y pedidos por mes (`combinado`, `ancho`).
- Sección "Producto": importe por categoría (`barras_horizontales`, `medio`) y
  reparto por categoría (`tarta`, `medio`).
- Sección "Canal" (si hay vista): importe por mes desglosado por canal (`barras`,
  `desglose: canal`, `apilado: true`, `ancho`). Sin `desglose`, las filas de cada canal
  se pintan como una sola serie y el canal no se ve.

**Clientes** (requiere `v_rfm_clientes` y `v_cohortes_clientes`, skill
`analitica-avanzada`):
- KPI: clientes con compras (`numero`), clientes del segmento "Campeones"
  (`numero`) y porcentaje de clientes que repiten (`numero`, `porcentaje`).
- Sección "Segmentos RFM": clientes por segmento (`barras_horizontales`, `medio`) e
  importe por segmento (`barras_horizontales`, `medio`).
- Sección "Retención": retención por cohorte y meses desde la primera compra
  (`tabla`, `ancho`) y una curva media (`lineas`, `medio`).
- Sección "Mejores clientes": top 20 por importe con su segmento (`tabla`, `ancho`).

**Producto** (requiere `v_abc_productos`):
- KPI: productos clase A (`numero`) y su porcentaje del importe (`numero`, `porcentaje`).
- Sección "Pareto": importe por producto en barras y porcentaje acumulado en línea
  (`combinado`, `ancho`; `series: ["importe", "pct_acumulado"]`).
- Sección "Clases ABC": productos e importe por clase (`barras`, `medio`, ×2) y
  detalle de la clase A (`tabla`, `ancho`).

## Entrega

Da el enlace (`url`) y una frase por sección con lo que muestra y la cifra clave,
sacada de `consultar_solo_lectura`, no del gráfico.

## Cambios posteriores

- Cambio de lógica de negocio → modifica la **vista** (skill `transformacion`); el
  dashboard se actualiza solo.
- Cambio de presentación → `actualizar_pregunta` (visualización o `ajustes`; los
  ajustes nuevos sustituyen a los anteriores, así que envíalos completos).
- Nuevo contenido → `crear_pregunta` + `anadir_a_dashboard(dashboard_id,
  elementos=[...])`; se coloca debajo y conserva lo que había.

## Límites

- Las preguntas pasan el mismo validador que las consultas: un solo SELECT, sin salir
  del esquema del proyecto.
- No hay filtros interactivos de dashboard: si la persona los pide, explícalo y ofrece
  preguntas separadas por la dimensión que quiera (p. ej. una por canal).
- No prometas números que no hayas comprobado con `consultar_solo_lectura`.
