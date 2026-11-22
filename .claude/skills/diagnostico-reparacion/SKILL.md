---
name: diagnostico-reparacion
description: Diagnostica y repara entregas de datos defectuosas (filas duplicadas, columnas renombradas, cambios de unidad como céntimos en lugar de euros, filas sin padre). Úsala SIEMPRE que un plan de carga salga bloqueado por defectos de los datos, cuando una validación falle en datos que antes funcionaban o cuando la persona hable de una "entrega estropeada".
---

# Diagnóstico y reparación ("bombero")

## Principios

- **Diagnóstico antes que arreglo:** cada síntoma se demuestra con una cifra.
- **El origen es la verdad:** no "arregles" datos buenos para que cuadren con una carga
  mala. El fichero entregado **no se modifica nunca**.
- **Nada fuera del perímetro:** no crees ni edites ficheros con la shell ni con
  herramientas de ficheros. La reparación se declara en el plan con
  `planificar_carga_transformada`, que queda en la huella del plan y en la auditoría.
- **Un arreglo solo está terminado cuando la validación pasa.**

## Procedimiento

1. **Síntoma.** Qué falla y dónde: plan de carga bloqueado, validación que no cuadra,
   dashboard con cifras raras. Anota la cifra.
2. **Comparar con la referencia.** `perfilar_fichero` de la nueva entrega frente a la
   tabla destino (`listar_esquema`) y, si existe, una entrega anterior buena o el
   `manifiesto.json` de la entrega.
3. **Clasificar el defecto** (lista de comprobación):
   - *Duplicados exactos*: `filas_duplicadas > 0` en el perfil o "clave repetida" en el plan.
   - *Columna renombrada*: el plan dice "¿Quizá se renombró X?".
   - *Cambio de unidad*: mín/máx/suma ~×100 (céntimos) frente a la referencia; precios
     enteros donde antes había decimales. Demuéstralo con 2 ejemplos concretos.
   - *Filas sin padre*: el plan indica valores que no existen en la tabla padre →
     carga primero la entrega padre (que puede necesitar su propia reparación).
   - *Cambio de formato* (fechas como texto, coma decimal): **no** se repara con
     transformaciones; pide el fichero corregido o usa una vista de contención.
4. **Proponer el arreglo** con la evidencia y deja que la persona decida. Opciones:
   - Pedir al origen el fichero corregido (lo más limpio).
   - **Reparar al cargar** con `planificar_carga_transformada(tabla, fichero,
     renombrar={...}, dividir={columna: 100}, quitar_duplicados_exactos=true)`.
     Solo con la conformidad expresa de la persona.
   - Vista de contención (`v_*_limpia`) mientras se corrige el origen.
5. **Enseñar el plan transformado**: el `impacto.transformacion.descripcion` cuantifica
   cada cambio (filas quitadas, rango antes/después). Resume en lenguaje claro y aplica
   con `aplicar_plan`. Retira con `descartar_plan` los planes bloqueados que sustituyes.
6. **Verificar** con `validar_carga(..., plan_carga=<id del plan transformado>)`, que
   compara la tabla con el origen tal y como se cargó, y contrasta las cifras con el
   `correcto` del `manifiesto.json` si existe (filas únicas, suma de importes).
7. **Resumir**: defecto, evidencia, arreglo aplicado (plan_id), validación y qué
   debería corregir el origen para la próxima entrega.

## Límites y trabajo futuro

- Solo hay tres transformaciones (renombrar, dividir por 10/100/1000 y quitar
  duplicados exactos); no hay expresiones libres a propósito.
- Para saber de qué fichero sale una tabla y qué vistas y dashboards dependen de ella
  (por ejemplo, antes de recargar una entrega), usa `describir_modelo` (skill
  `documentacion`).
