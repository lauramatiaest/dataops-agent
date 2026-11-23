---
name: analitica-avanzada
description: Análisis de clientes y productos como vistas analíticas - segmentación de clientes por recencia y frecuencia (RFM simplificado), clasificación ABC / Pareto de productos y tendencias (variación interanual y media móvil). Úsala cuando la persona pida segmentar clientes, saber qué productos concentran las ventas, comparar con el año anterior o "un análisis más profundo" que un total por mes.
---

# Analítica avanzada

Tres análisis clásicos de BI expresados como vistas `v_*` con el flujo habitual
(`consultar_solo_lectura` → `planificar_vista` → `aplicar_plan`). No hay herramientas
especiales: la potencia está en el SQL, que aquí ya viene probado.

| Pregunta de negocio | Vista | Una fila por |
|---|---|---|
| ¿Quiénes son mis mejores clientes y cuáles estoy perdiendo? | `v_rfm_clientes` | cliente con compras |
| ¿Qué productos concentran las ventas? | `v_abc_productos` | producto (también los que no venden) |
| ¿Crecemos frente al año pasado? | `v_ventas_tendencia` | mes |

## 1. Antes de escribir SQL: decisiones que confirma la persona

Pregunta (o, si ya está decidido en la conversación, recuérdalo) y no sigas sin
respuesta:

- **Qué cuenta como compra.** Las plantillas excluyen `estado = 'cancelado'` y
  conservan devueltos y enviados. Es una regla de negocio: si la persona no la ha
  confirmado, enséñale cuántos pedidos e importe hay por estado
  (`consultar_solo_lectura`) y pregúntale. Si decide otra cosa, cambia el `WHERE` en
  todas las vistas para que sean coherentes entre sí.
- **Fecha de corte.** La recencia se mide hasta la **última fecha de pedido de los
  datos**, no hasta hoy: con datos históricos, "hoy" haría que todos los clientes
  parecieran perdidos y la vista cambiaría cada día sin que cambien los datos. Dilo
  al presentar los resultados.
- **Importe**: el de la convención de CLAUDE.md (por línea, con descuento, redondeado).

## 2. Adaptar la plantilla al modelo

1. `listar_esquema`: confirma tablas, columnas y claves foráneas. Las plantillas usan
   `clientes`, `pedidos(pedido_id, cliente_id, fecha_pedido, estado)`,
   `lineas_pedido(pedido_id, ref_producto, cantidad, precio_unitario, descuento_pct)`
   y `productos(sku, nombre, categoria)`. Si los nombres son otros, sustitúyelos; si
   falta una relación que el JOIN necesita, para y usa la skill `modelado`.
2. Prueba el SELECT con `consultar_solo_lectura` (`max_filas` pequeño) y haz los
   **controles** de cada análisis (abajo). Si un control no cuadra, no crees la vista:
   explica la diferencia.
3. `planificar_vista(nombre=..., consulta=...)`, enseña columnas y filas del plan y
   `aplicar_plan`.
4. Ofrece el dashboard (skill `reporting`, plantillas "Clientes" y "Producto").

## 3. Segmentación de clientes (RFM simplificado)

Para cada cliente con compras: **recencia** (días desde su última compra hasta la
fecha de corte), **frecuencia** (pedidos distintos) e **importe**. El segmento usa
dos umbrales fijos y fáciles de explicar: **90 días** de recencia y **5 pedidos**. Son
el punto de partida: enséñaselos a la persona y cámbialos si lo pide (o si un
segmento sale vacío o con casi todos los clientes; la mediana de pedidos por cliente,
con `consultar_solo_lectura`, es una buena referencia).

```sql
WITH ventas AS (
    SELECT p.cliente_id, p.pedido_id, p.fecha_pedido,
           round(l.cantidad * l.precio_unitario * (1 - l.descuento_pct / 100.0), 2) AS importe
    FROM pedidos p
    JOIN lineas_pedido l ON l.pedido_id = p.pedido_id
    WHERE p.estado <> 'cancelado'
),
corte AS (
    SELECT max(fecha_pedido) AS fecha_corte FROM ventas
),
por_cliente AS (
    SELECT v.cliente_id,
           max(c.fecha_corte) - max(v.fecha_pedido) AS recencia_dias,
           count(DISTINCT v.pedido_id)              AS frecuencia,
           round(sum(v.importe), 2)                 AS importe
    FROM ventas v
    CROSS JOIN corte c
    GROUP BY v.cliente_id
)
SELECT cliente_id, recencia_dias, frecuencia, importe,
       CASE
           WHEN recencia_dias <= 90 AND frecuencia >= 5 THEN 'Fieles'
           WHEN recencia_dias <= 90                     THEN 'Ocasionales'
           WHEN frecuencia >= 5                         THEN 'En riesgo'
           ELSE 'Inactivos'
       END AS segmento_rfm
FROM por_cliente
```

| Segmento | Regla | Lectura |
|---|---|---|
| Fieles | compra en los últimos 90 días y 5 o más pedidos | base estable: cuidarlos |
| Ocasionales | compra en los últimos 90 días y menos de 5 pedidos | recientes: animarlos a repetir |
| En riesgo | más de 90 días sin comprar y 5 o más pedidos | eran buenos: campaña de reactivación |
| Inactivos | más de 90 días sin comprar y menos de 5 pedidos | |

**Controles:** suma de `importe` = importe total con la misma regla de compra; número
de filas = clientes distintos con al menos una compra (los clientes sin compras no
aparecen: dilo y da cuántos son).

## 4. ABC / Pareto de productos

Ordena los productos por importe y acumula el porcentaje. Clase **A**: los que llevan
el acumulado hasta el 80 % (incluido el que lo cruza); **B**: hasta el 95 %; **C**: el
resto, incluidos los productos sin ventas (importe 0, gracias al `LEFT JOIN`). Los
umbrales 80/95 son los habituales; si la persona quiere otros, cámbialos y dilo.

```sql
WITH ventas AS (
    SELECT l.ref_producto, l.cantidad,
           round(l.cantidad * l.precio_unitario * (1 - l.descuento_pct / 100.0), 2) AS importe
    FROM lineas_pedido l
    JOIN pedidos p ON p.pedido_id = l.pedido_id
    WHERE p.estado <> 'cancelado'
),
por_producto AS (
    SELECT pr.sku, pr.nombre, pr.categoria,
           coalesce(sum(v.cantidad), 0) AS unidades,
           coalesce(sum(v.importe), 0)  AS importe
    FROM productos pr
    LEFT JOIN ventas v ON v.ref_producto = pr.sku
    GROUP BY pr.sku, pr.nombre, pr.categoria
),
acumulado AS (
    SELECT sku, nombre, categoria, unidades, importe,
           row_number() OVER (ORDER BY importe DESC, sku) AS posicion,
           100 * importe / nullif(sum(importe) OVER (), 0) AS pct,
           100 * sum(importe) OVER (ORDER BY importe DESC, sku ROWS UNBOUNDED PRECEDING)
               / nullif(sum(importe) OVER (), 0)           AS pct_acum
    FROM por_producto
)
SELECT posicion, sku, nombre, categoria, unidades,
       round(importe, 2)  AS importe,
       round(pct, 2)      AS pct_importe,
       round(pct_acum, 2) AS pct_acumulado,
       CASE
           WHEN pct_acum - pct < 80 THEN 'A'
           WHEN pct_acum - pct < 95 THEN 'B'
           ELSE 'C'
       END AS clase_abc
FROM acumulado
```

**Controles:** número de filas = productos del catálogo; suma de `importe` = importe
total con la misma regla; el `pct_acumulado` del último producto es 100.

## 5. Tendencias: interanual y media móvil

Sobre la vista mensual de ventas que ya exista (aquí `v_ventas_mensuales` con `mes` e
`importe`), sin repetir su lógica:

```sql
SELECT a.mes, a.importe,
       round(avg(a.importe) OVER (ORDER BY a.mes ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 2)
           AS media_movil_3m,
       b.importe AS importe_hace_un_anio,
       round(100 * (a.importe / nullif(b.importe, 0) - 1), 2) AS variacion_interanual_pct
FROM v_ventas_mensuales a
LEFT JOIN v_ventas_mensuales b ON b.mes = (a.mes - interval '1 year')::date
```

La variación interanual cruza por fecha (`mes - 1 año`), así que no se descuadra si
falta un mes; queda vacía en los 12 primeros meses. La media móvil usa las 3 filas
anteriores: si faltan meses, avísalo.

**Controles:** mismas filas que la vista mensual; comprueba un mes a mano.

## 6. Presentar resultados

- Da cifras que salgan de `consultar_solo_lectura` sobre la vista recién creada (p. ej.
  clientes y porcentaje de importe por segmento; productos y porcentaje de importe por
  clase), no de memoria ni del gráfico.
- Explica la lectura de negocio en una frase por análisis y una acción posible
  ("los clientes en riesgo concentran X % del importe: candidatos a una campaña de
  reactivación").
- Advierte de los límites cuando apliquen: los umbrales de la segmentación son una
  regla de negocio, no una verdad estadística; sin 13 meses no hay variación
  interanual. Con datos sintéticos, los patrones reflejan el generador, no un negocio
  real.
