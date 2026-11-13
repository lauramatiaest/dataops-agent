---
name: reporting
description: Crea preguntas y dashboards en la herramienta de BI (Metabase) sobre vistas analíticas del proyecto. Úsala cuando la persona pida un dashboard, un gráfico, un informe visual o publicar métricas.
---

# Reporting: preguntas y dashboards

## Antes de empezar

1. `estado_bi`. Si no está disponible o no tiene `base_datos_id`, para y explica cómo
   arrancarlo (`docker compose up -d` y `python scripts/configurar_metabase.py`).
2. Asegúrate de que existen vistas `v_*` para cada métrica (skill `transformacion`).
   Las preguntas leen de vistas para que un cambio de lógica no obligue a rehacerlas.
3. Si acabas de crear vistas, `sincronizar_bi`.

## Crear el dashboard

1. Una pregunta por idea, con `crear_pregunta(nombre, sql, visualizacion, descripcion)`:
   - evolución temporal → `lineas`; comparación entre categorías → `barras` o
     `barras_horizontales`; reparto → `tarta` (pocas categorías); KPI → `numero`;
     detalle → `tabla`.
   - SQL simple sobre la vista: `SELECT mes, importe FROM v_ventas_mensuales ORDER BY mes`.
   - Nombre en lenguaje de negocio ("Ventas mensuales"); si la vista aplica un filtro de negocio,
     indícalo en el nombre o la descripción.
2. `crear_dashboard(nombre, pregunta_ids=[...], descripcion=...)`. Pon primero los KPI.
3. Entrega el enlace (`url`) y una frase por gráfico con lo que muestra y la cifra clave.

## Cambios posteriores

- Cambio de lógica de negocio → modifica la **vista** (skill `transformacion`); el
  dashboard se actualiza solo.
- Cambio de presentación → `actualizar_pregunta`.
- Nuevo gráfico → `crear_pregunta` + `anadir_a_dashboard` (conserva los existentes).

## Límites

- Las preguntas pasan el mismo validador que las consultas: un solo SELECT, sin salir
  del esquema del proyecto.
- No prometas números que no hayas comprobado con `consultar_solo_lectura`.
