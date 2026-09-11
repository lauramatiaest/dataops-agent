---
name: modelado
description: Define claves primarias y relaciones (claves foráneas) entre tablas ya cargadas, siempre con evidencia de integridad y sin inventar relaciones. Úsala al relacionar tablas, construir un modelo en estrella o cuando falte una relación para responder una pregunta.
---

# Modelado: claves y relaciones con evidencia

Principio: **una relación es una afirmación sobre los datos, y se demuestra.** Nunca
crees una clave foránea porque "parece lógica"; tampoco hagas JOIN en una vista sobre
columnas cuya relación no se ha comprobado.

## Procedimiento

1. `listar_esquema` para ver tablas, columnas, claves primarias y foráneas existentes.
2. Para cada tabla sin clave primaria, decide con la skill `ingesta` (punto 3) y usa
   `planificar_clave`. Si el plan dice que hay repetidos o nulos, no la crees: informa.
3. **Relaciones candidatas.** Busca pares hija.columna → padre.columna_clave con:
   - **Evidencia de nombre/significado:** mismo nombre (`pedidos.cliente_id` →
     `clientes.cliente_id`) o nombre que indica claramente la entidad.
   - **Evidencia de datos:** `comprobar_relacion` con `huerfanos = 0` y tipos
     compatibles.
4. Decide según esta tabla:

| Nombre coherente | Datos (huérfanos) | Acción |
|---|---|---|
| Sí | 0 | Planifica con `planificar_relacion`, enseña la evidencia y aplica. |
| Sí | > 0 | NO la crees. Enseña nº de huérfanos y muestra; pregunta si excluirlos, corregir origen o dejar la relación sin restricción. |
| No o dudoso | 0 | **Pregunta.** Ejemplo: "`lineas_pedido.ref_producto` coincide con `productos.sku` en el 100 % de los valores, pero los nombres no lo dicen. ¿Es la referencia al producto?" |
| No | > 0 o sin comprobar | No es una relación. No la propongas. |

5. **Si falta una relación** para lo que pide la persona (p. ej. ventas por categoría y
   no hay forma demostrada de unir líneas con productos), dilo explícitamente y
   pregunta qué columna las une. No construyas el JOIN suponiendo.
6. Tras aplicar, `listar_esquema` y resume el modelo (tablas, claves, relaciones).

## Notas técnicas

- `planificar_relacion` vuelve a comprobar huérfanos, tipos y unicidad del padre; si
  bloquea, la causa está en el plan (`bloqueos` e `impacto.evidencia`).
- Los valores nulos en la columna hija no son huérfanos (una línea sin producto es un
  dato incompleto, no una referencia rota); menciónalos si son muchos.
- Un modelo en estrella típico aquí: hechos = `lineas_pedido` (con `pedidos`),
  dimensiones = `clientes`, `productos`, fecha.
