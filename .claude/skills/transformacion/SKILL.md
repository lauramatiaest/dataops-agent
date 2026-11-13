---
name: transformacion
description: Crea y modifica vistas analíticas (v_*) en PostgreSQL con SELECT validados, planificar→aplicar. Úsala para calcular métricas (ventas, importes, recuentos por periodo o categoría), aplicar reglas de negocio como "excluye cancelados" o preparar datos para un dashboard.
---

# Transformación con vistas analíticas

Las transformaciones se expresan como **vistas** (`v_*`), no como tablas copiadas: la
lógica queda en un solo sitio, es revisable y el dashboard cambia en cuanto cambia la
vista.

## Procedimiento

1. Aclara la métrica: qué se mide, con qué granularidad (día, mes, categoría) y qué
   filtros de negocio aplican. Si la persona no lo dice, usa las convenciones de
   CLAUDE.md y dilo explícitamente ("asumo importe neto con descuento").
   **No apliques filtros de negocio que la persona no haya pedido** (excluir estados,
   periodos, segmentos...). Si los datos tienen valores que podrían cambiar la cifra
   (por ejemplo, pedidos `cancelado` o `devuelto`), calcula sin filtrar, menciona
   cuántas filas e importe representan y pregunta si se deben excluir.
2. Escribe un único `SELECT` sobre tablas del proyecto. Solo JOINs sobre relaciones
   existentes o comprobadas (skill `modelado`).
3. Pruébalo con `consultar_solo_lectura` (con `max_filas` pequeño) y revisa que los
   números tienen sentido: compáralos con un total de control cuando puedas.
4. `planificar_vista(nombre="v_...", consulta=...)`. El plan devuelve las columnas y el
   número de filas: enséñalos y aplica con `aplicar_plan`.

## Cambiar una vista existente ("excluye cancelados")

1. `planificar_vista(..., reemplazar=true)` con la nueva consulta.
2. Si conservas las mismas columnas en el mismo orden, es un `CREATE OR REPLACE VIEW`
   no destructivo: los dashboards se actualizan solos.
3. Si cambian las columnas, el plan será **destructivo** (DROP + CREATE) y puede quedar
   bloqueado por dependientes. Prefiere mantener las columnas; si no es posible,
   explica el impacto y pide aprobación.
4. Tras aplicar, muestra el antes y el después de la cifra principal.

## Plantilla de referencia

Estructura de ejemplo, sin filtros de negocio: añade un `WHERE` solo si la persona
lo ha pedido.

```sql
SELECT date_trunc('month', p.fecha_pedido)::date AS mes,
       count(DISTINCT p.pedido_id)                AS pedidos,
       round(sum(l.cantidad * l.precio_unitario * (1 - l.descuento_pct / 100.0)), 2) AS importe
FROM pedidos p
JOIN lineas_pedido l ON l.pedido_id = p.pedido_id
GROUP BY 1
```

No uses `SELECT *` en vistas para dashboards: nombra las columnas.
