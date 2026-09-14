---
name: diagnostico-reparacion
description: BORRADOR (ampliación "bombero"). Diagnostica datos estropeados en una nueva entrega o tras una carga (duplicados, columnas renombradas, cambios de unidad o formato), localiza el origen y propone un arreglo verificado. Úsala cuando una carga se bloquee o una validación falle en datos que antes funcionaban.
---

# Diagnóstico y reparación ("bombero") — BORRADOR

> Estado: borrador de la ampliación. El procedimiento está definido y usa herramientas
> que ya existen; falta afinarlo con pruebas (tareas `eval/tareas/1[6-9]_*.yaml`) y,
> si hace falta, una herramienta de linaje (`trazar_linaje`) que hoy no existe.

## Principios

- Diagnóstico antes que arreglo: cada síntoma se demuestra con una cifra.
- El origen es la verdad hasta que se demuestre lo contrario: no "arregles" datos
  buenos para que cuadren con una carga mala.
- Todo arreglo es un plan revisable; si borra o reemplaza, es destructivo.
- Un arreglo solo está terminado cuando la validación pasa.

## Procedimiento

1. **Síntoma.** Qué falla y dónde: plan de carga bloqueado, validación que no cuadra,
   dashboard con cifras raras. Anota la cifra.
2. **Comparar con la referencia.** `perfilar_fichero` de la nueva entrega frente a la
   tabla destino (`listar_esquema`) y, si existe, una entrega anterior buena.
3. **Clasificar el defecto** (lista de comprobación):
   - *Duplicados*: `filas_duplicadas > 0` en el perfil o "clave repetida" en el plan.
   - *Columna renombrada*: el plan de carga dice "¿Quizá se renombró X?".
   - *Cambio de unidad*: mín/máx/suma ~×100 (céntimos) frente a la referencia; precios
     enteros donde antes había decimales.
   - *Cambio de formato*: tipo inferido distinto (fecha como texto, coma decimal).
   - *Huérfanos*: `comprobar_relacion` > 0 tras cargar.
4. **Localizar el origen (linaje).** Hoy, manual: qué fichero alimenta la tabla
   (auditoría con `ver_auditoria`), qué vistas usan la tabla (`impacto.dependientes` de
   `planificar_vista`/`planificar_borrado`) y qué dashboards usan esas vistas.
5. **Proponer el arreglo** con evidencia y opciones; la persona elige. Arreglos
   posibles con las herramientas actuales:
   - Renombrado o unidad: pedir al proveedor el fichero corregido, o preparar una copia
     corregida en `datos/` (la persona decide) y cargarla con plan.
   - Duplicados exactos: cargar la entrega deduplicada; si ya se cargó, plan de borrado
     de filas (destructivo, con aprobación).
   - Vista de contención: `v_*_limpia` con `DISTINCT` o conversión `/ 100` mientras se
     corrige el origen (documentarlo en la descripción de la vista).
6. **Verificar** con `validar_carga` frente a los valores esperados (si existe
   `manifiesto.json` en la entrega, sus cifras `correcto`) y resumir: defecto, causa,
   arreglo aplicado, validación.

## Pendiente (para la memoria)

- Herramienta `trazar_linaje(objeto)` basada en `pg_depend` + auditoría + preguntas de BI.
- Herramienta `planificar_transformacion_carga` para convertir unidades/renombrar sin
  editar ficheros a mano.
- Tareas de evaluación específicas y criterio de éxito.
