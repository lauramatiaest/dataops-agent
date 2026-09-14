---
name: validacion
description: Validación determinista de datos tras cargas y cambios de modelo (recuentos frente al origen, sumas de control, unicidad de claves y huérfanos). Úsala después de cada carga, antes de dar por bueno un modelo y cuando la persona pregunte si los datos son correctos.
---

# Validación determinista

La validación no la "opina" el modelo: la calculan las herramientas y se informa tal
cual. Una validación que falla detiene el trabajo hasta que la persona decida.

## Tras cada carga

`validar_carga(tabla, ficheros=[...], clave=[...], columnas_suma=[...], relaciones=[...])`

- `ficheros`: TODOS los ficheros cargados en esa tabla (la comparación es con la tabla
  entera).
- `clave`: la clave primaria (comprueba repetidos y nulos).
- `columnas_suma`: columnas numéricas con significado de negocio (cantidades, precios,
  importes). La suma se compara exactamente con la del fichero.
- `relaciones`: `[[columna_hija, tabla_padre, columna_padre], ...]` para comprobar
  huérfanos si la tabla es hija.

Informa en una tabla corta: comprobación, esperado, obtenido, ✔/✘.

## Validar el modelo completo

1. `listar_esquema`: todas las tablas tienen clave primaria; relaciones esperadas creadas.
2. Para cada relación SIN clave foránea que se use en vistas: `comprobar_relacion`.
3. Totales de control con `consultar_solo_lectura`, por ejemplo la suma del importe
   de las líneas frente a la de la vista de ventas (deben coincidir salvo filtros
   explícitos como los cancelados).

## Si algo no cuadra

- Recuento menor que el origen → filas perdidas; mayor → duplicados o carga repetida.
- Suma ×100 o ÷100 → cambio de unidad (céntimos/euros).
- Huérfanos → referencia rota o datos de otra entrega.
Describe la diferencia con cifras y pasa a la skill `diagnostico-reparacion` si son
datos que antes cargaban bien. No borres ni corrijas nada sin plan y sin aprobación.
