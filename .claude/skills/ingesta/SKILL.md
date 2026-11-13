---
name: ingesta
description: Perfila ficheros CSV/JSON del directorio de datos y los carga en tablas de PostgreSQL con el patrón planificar→aplicar. Úsala cuando la persona pida cargar, importar o "meter" ficheros, o empezar un modelo desde datos en bruto.
---

# Ingesta de ficheros

Objetivo: cada fichero termina en una tabla con tipos correctos, clave primaria cuando
haya evidencia, y una validación que demuestra que no se ha perdido ni inventado nada.

## Procedimiento

1. **Inventario.** `listar_ficheros`. Si la persona no ha dicho qué ficheros, enséñale
   la lista y confirma cuáles cargar.
2. **Perfil de cada fichero** con `perfilar_fichero`. Fíjate en:
   - `tipo_inferido` de cada columna y sus `advertencias` (coma decimal, renombrados);
   - `candidatas_clave` (sin nulos y sin repetidos);
   - `filas_duplicadas` del fichero (si hay, para: ver más abajo).
   Resume el perfil a la persona en 3-5 líneas por fichero, no copies el JSON entero.
3. **Clave primaria.** Proponla solo si hay UNA candidata con nombre de identificador
   (`*_id`, `id`, `sku`, `codigo`...). Si hay varias o ninguna, pregunta. No uses como
   clave columnas que solo son únicas por casualidad (nombres, emails, fechas).
4. **Crear la tabla:** `planificar_tabla(nombre, desde_fichero=..., clave_primaria=[...])`.
   - Revisa los tipos del plan. Cambia a `columnas` explícitas si un tipo no tiene
     sentido de negocio (p. ej. un código postal inferido como integer → `text`;
     importes → `numeric(12,2)`).
   - Enseña el SQL y aplica con `aplicar_plan`.
5. **Cargar:** `planificar_carga(tabla, fichero)` (modo `anadir` por defecto).
   - Con bloqueos, NO apliques: explica cada uno (columna inexistente o renombrada,
     valores con tipo inválido, claves repetidas o ya existentes, filas sin padre) y
     pregunta. Si son defectos de la entrega (duplicados, renombrados, cambios de
     unidad), cambia a la skill `diagnostico-reparacion`.
   - `modo="reemplazar"` es destructivo si la tabla tiene filas: solo si la persona lo
     pide, y con `aplicar_plan_destructivo`.
6. **Validar** inmediatamente con la skill `validacion` (`validar_carga` con la clave y
   las columnas numéricas importantes como `columnas_suma`).

## Cuándo parar y preguntar

- El fichero tiene filas duplicadas exactas o claves repetidas.
- Hay filas cuyo padre no existe (p. ej. líneas de pedidos que aún no se han cargado):
  carga antes la tabla padre.
- Una columna del fichero no existe en la tabla (posible renombrado en origen).
- Los tipos no cuadran con lo esperado (importes como texto, fechas en otro formato).
- La validación no cuadra.

En esos casos describe la evidencia (cuántas filas, ejemplos) y ofrece opciones; para
reparar una entrega defectuosa usa la skill `diagnostico-reparacion`. Nunca crees ni
edites ficheros de datos para "arreglarlos".

## Orden de carga

Primero las tablas "padre" (clientes, productos), después las "hijas" (pedidos,
lineas_pedido). Así las relaciones se pueden comprobar en cuanto termina la carga.
