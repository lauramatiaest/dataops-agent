---
name: documentacion
description: Documenta el modelo de datos del proyecto - descripción de negocio de cada tabla, vista y columna guardada como comentarios en PostgreSQL, linaje (de qué fichero sale cada tabla y qué vistas y dashboards la usan) y un diccionario de datos en Markdown con diagrama entidad-relación. Úsala cuando la persona pida documentar, explicar o entregar el modelo, un diccionario de datos o "qué hay en la base de datos", y al terminar de construir un modelo.
---

# Documentación del modelo

La documentación tiene dos partes que no se mezclan:

- **Hechos** (tipos, claves, relaciones, filas, linaje, uso en BI): los da el servidor,
  desde el catálogo y la auditoría. Nunca los escribas tú a mano.
- **Significado de negocio** (qué representa cada tabla y columna): lo redactas tú,
  con evidencia, y se guarda en el modelo como comentarios con plan y aprobación.

## Procedimiento

1. **Inventario.** `describir_modelo` → objetos, columnas, relaciones, linaje, uso en
   BI y `pendiente_de_documentar`. Si ya hay descripciones, respétalas salvo que la
   persona pida cambiarlas.
2. **Evidencia para redactar.** Para cada objeto sin describir, usa lo que ya hay:
   nombre, tipos, claves, relaciones, el fichero de origen, la definición SQL de las
   vistas y, si hace falta, unos ejemplos con `consultar_solo_lectura` (`LIMIT 5`, o
   `count(DISTINCT ...)` para saber si una columna es categórica).
3. **Redactar** descripciones breves (una frase; máximo 500 caracteres):
   - Tabla: qué representa una fila y de dónde sale ("Una fila por pedido; cargada
     desde pedidos.csv y la entrega de octubre").
   - Columna: significado y unidad o formato ("Importe unitario en euros, IVA no
     incluido"). En claves foráneas, a qué apuntan.
   - Vista: qué métrica calcula, con qué granularidad y **qué filtros aplica** (p. ej.
     "excluye pedidos cancelados").
   - **No inventes**: si no sabes la unidad o el significado, dilo ("unidad no
     confirmada") y pregúntalo. Nada de datos personales de ejemplo en las descripciones.
   - **Nada de cifras que cambian con los datos** (recuentos, totales, mínimos y
     máximos, rangos de fechas): se quedan obsoletas en la siguiente carga. Usa esa
     evidencia para entender el significado, pero no la copies; esos hechos ya los
     genera el diccionario a partir del catálogo.
4. **Plan.** `planificar_documentacion({objeto: {"descripcion": ..., "columnas":
   {...}}})`. Enseña un resumen (cuántas descripciones, y las que **sustituyen** a una
   existente, antes → después) y aplica con `aplicar_plan` cuando la persona esté de
   acuerdo. Puedes hacerlo en varios planes (uno por tabla) si el modelo es grande.
5. **BI.** Tras aplicar, `sincronizar_bi`: además de releer el esquema, copia las
   descripciones del modelo en Metabase (tablas, vistas y columnas) y sustituye las que
   difieran, porque el modelo es la fuente de verdad. Informa de cuántas se
   actualizaron. Si devuelve `no_encontradas` (objetos recién creados que Metabase aún
   no conoce), espera unos segundos y vuelve a llamarla.
6. **Diccionario.** `generar_diccionario` → guarda el `markdown` **tal cual** en
   `docs/modelo/diccionario.md` (es un documento generado; no lo retoques a mano: si
   algo está mal, corrige la descripción en el modelo y regenera).
7. **Resumen**: cuántos objetos y columnas quedan descritos, qué sigue pendiente y
   dónde está el diccionario.

## Cuándo no

- Para preguntas puntuales ("¿qué columnas tiene pedidos?") basta `listar_esquema`.
- No documentes objetos fuera del esquema del proyecto (no existen para ti).
