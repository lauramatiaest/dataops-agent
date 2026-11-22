---
name: analitica-avanzada
description: Análisis avanzados de clientes y productos como vistas analíticas - segmentación RFM (recencia, frecuencia, importe), clasificación ABC / Pareto de productos, retención por cohortes de clientes y tendencias (variación interanual y media móvil). Úsala cuando la persona pida segmentar clientes, saber qué productos concentran las ventas, medir la retención o la recurrencia, comparar con el año anterior o "un análisis más profundo" que un total por mes.
---

# Analítica avanzada

Cuatro análisis clásicos de BI expresados como vistas `v_*` con el flujo habitual
(`consultar_solo_lectura` → `planificar_vista` → `aplicar_plan`). No hay herramientas
especiales: la potencia está en el SQL, que aquí ya viene probado.

| Pregunta de negocio | Vista | Una fila por |
|---|---|---|
| ¿Quiénes son mis mejores clientes y cuáles estoy perdiendo? | `v_rfm_clientes` | cliente con compras |
| ¿Qué productos concentran las ventas? | `v_abc_productos` | producto (también los que no venden) |
| ¿Vuelven los clientes después de su primera compra? | `v_cohortes_clientes` | cohorte × meses desde la primera compra |
| ¿Crecemos frente al año pasado? | `v_ventas_tendencia` | mes |

## 1. Antes de escribir SQL: decisiones que confirma la persona

Pregunta (o, si ya está decidido en la conversación o en los comentarios del modelo,
recuérdalo) y no sigas sin respuesta:

- **Qué cuenta como compra.** Las plantillas excluyen `estado = 'cancelado'` y
  conservan devueltos y enviados. Es una regla de negocio: si la persona no la ha
  confirmado, enséñale cuántos pedidos e importe hay por estado
  (`consultar_solo_lectura`) y pregúntale. Si decide otra cosa, cambia el `WHERE` en
  todas las vistas para que sean coherentes entre sí.
- **Fecha de corte.** La recencia y las cohortes se miden hasta la **última fecha de
  pedido de los datos**, no hasta hoy: con datos históricos, "hoy" haría que todos los
  clientes parecieran perdidos y la vista cambiaría cada día sin que cambien los datos.
  Dilo al presentar los resultados.
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
4. Documenta la vista (skill `documentacion`), explicando la regla de compra y la
   fecha de corte, y ofrece el dashboard (skill `reporting`, plantillas "Clientes" y
   "Producto").

## 3. RFM: segmentación de clientes

Cada cliente recibe de 1 a 5 puntos en **R**ecencia (días desde su última compra
hasta la fecha de corte; 5 = más reciente), **F**recuencia (pedidos distintos) e
**importe** (M). Las puntuaciones son quintiles con `cume_dist()`, así que dos clientes
con el mismo valor reciben la misma nota y el resultado no depende del orden de
lectura (con `ntile()` los empates se repartirían al azar).

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
),
puntuado AS (
    SELECT cliente_id, recencia_dias, frecuencia, importe,
           ceil(5 * cume_dist() OVER (ORDER BY recencia_dias DESC))::int AS puntuacion_r,
           ceil(5 * cume_dist() OVER (ORDER BY frecuencia))::int         AS puntuacion_f,
           ceil(5 * cume_dist() OVER (ORDER BY importe))::int            AS puntuacion_m
    FROM por_cliente
)
SELECT cliente_id, recencia_dias, frecuencia, importe,
       puntuacion_r, puntuacion_f, puntuacion_m,
       CASE
           WHEN puntuacion_r >= 4 AND puntuacion_f >= 4 THEN 'Campeones'
           WHEN puntuacion_r >= 3 AND puntuacion_f >= 3 THEN 'Leales'
           WHEN puntuacion_r >= 4                       THEN 'Prometedores'
           WHEN puntuacion_r = 3                        THEN 'Necesitan atención'
           WHEN puntuacion_f >= 3                       THEN 'En riesgo'
           ELSE 'Perdidos'
       END AS segmento_rfm
FROM puntuado
```

Segmentos (exhaustivos y sin solapes; usan R y F, y M queda como dato informativo):

| Segmento | Regla | Lectura |
|---|---|---|
| Campeones | R ≥ 4 y F ≥ 4 | compran a menudo y hace poco |
| Leales | R ≥ 3 y F ≥ 3 (resto) | base estable |
| Prometedores | R ≥ 4 y F ≤ 2 | recientes, aún con pocas compras |
| Necesitan atención | R = 3 y F ≤ 2 | se enfrían |
| En riesgo | R ≤ 2 y F ≥ 3 | eran buenos y llevan tiempo sin comprar |
| Perdidos | R ≤ 2 y F ≤ 2 | |

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

## 5. Cohortes de retención

La cohorte de un cliente es el mes de su primera compra. Para cada cohorte y cada mes
posterior observable, cuántos de sus clientes vuelven a comprar ese mes. La vista
genera la **rejilla completa** (`generate_series`): un mes sin compras aparece con 0,
y los meses posteriores a la fecha de corte no aparecen. Sin rejilla, un 0 sería una
fila ausente y cualquier media saldría inflada.

```sql
WITH compras AS (
    SELECT DISTINCT p.cliente_id, date_trunc('month', p.fecha_pedido)::date AS mes
    FROM pedidos p
    WHERE p.estado <> 'cancelado'
),
primera AS (
    SELECT cliente_id, min(mes) AS cohorte
    FROM compras
    GROUP BY cliente_id
),
tamano AS (
    SELECT cohorte, count(*) AS clientes_cohorte
    FROM primera
    GROUP BY cohorte
),
corte AS (
    SELECT max(mes) AS ultimo_mes FROM compras
),
actividad AS (
    SELECT pr.cohorte,
           ((extract(year FROM c.mes) - extract(year FROM pr.cohorte)) * 12
             + extract(month FROM c.mes) - extract(month FROM pr.cohorte))::int AS meses,
           count(*) AS clientes_activos
    FROM compras c
    JOIN primera pr ON pr.cliente_id = c.cliente_id
    GROUP BY 1, 2
),
rejilla AS (
    SELECT t.cohorte, t.clientes_cohorte, k AS meses
    FROM tamano t
    CROSS JOIN corte co
    CROSS JOIN generate_series(
        0,
        ((extract(year FROM co.ultimo_mes) - extract(year FROM t.cohorte)) * 12
          + extract(month FROM co.ultimo_mes) - extract(month FROM t.cohorte))::int
    ) AS k
)
SELECT r.cohorte,
       r.meses                                                        AS meses_desde_primera,
       r.clientes_cohorte,
       coalesce(a.clientes_activos, 0)                                AS clientes_activos,
       round(100.0 * coalesce(a.clientes_activos, 0) / r.clientes_cohorte, 2) AS pct_retencion
FROM rejilla r
LEFT JOIN actividad a ON a.cohorte = r.cohorte AND a.meses = r.meses
```

**Controles:** suma de `clientes_activos` en `meses_desde_primera = 0` = clientes con
compras (el mismo número que filas de `v_rfm_clientes`); `pct_retencion` = 100 en el
mes 0.

Lecturas útiles para preguntas (no hacen falta más vistas):
- Matriz de retención: una columna por mes con
  `max(pct_retencion) FILTER (WHERE meses_desde_primera = 1) AS mes_1`, …, agrupando
  por `cohorte, clientes_cohorte`.
- Curva media: `100.0 * sum(clientes_activos) / sum(clientes_cohorte)` por
  `meses_desde_primera` (ponderada por tamaño de cohorte; no promedies porcentajes).

## 6. Tendencias: interanual y media móvil

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

## 7. Presentar resultados

- Da cifras que salgan de `consultar_solo_lectura` sobre la vista recién creada (p. ej.
  clientes y porcentaje de importe por segmento; productos y porcentaje de importe por
  clase), no de memoria ni del gráfico.
- Explica la lectura de negocio en una frase por análisis y una acción posible
  ("los clientes en riesgo concentran X % del importe: candidatos a una campaña de
  reactivación").
- Advierte de los límites cuando apliquen: con pocos clientes (menos de unos 50) los
  quintiles RFM no son fiables; con menos de 6 meses de datos las cohortes dicen poco;
  sin 13 meses no hay variación interanual. Con datos sintéticos, los patrones (por
  ejemplo, una retención plana) reflejan el generador, no un negocio real.
