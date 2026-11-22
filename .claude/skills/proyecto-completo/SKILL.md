---
name: proyecto-completo
description: Orquesta un proyecto de datos de principio a fin en seis fases (configuración, descubrimiento, diseño, construcción, validación y documentación) con un punto de control humano para aprobar el diseño antes de construir. Úsala cuando la persona pida construir un modelo o un dashboard "desde cero", "completo", "de principio a fin" a partir de unos ficheros, o retomar un proyecto a medias.
---

# Proyecto completo por fases

Esta skill no hace el trabajo técnico: decide **qué fase toca**, llama a la skill de
cada fase y garantiza que nada se construye sin un diseño aprobado. Cada fase termina
con un **resumen de fase** (3-6 líneas: qué se ha hecho, cifras comprobadas y qué
queda) para que la persona pueda seguir el avance o interrumpir.

| Fase | Skill | Entregable | Puerta para pasar a la siguiente |
|---|---|---|---|
| 1. Configuración | — | estado del entorno | servicios disponibles y alcance claro |
| 2. Descubrimiento | `ingesta` (pasos de perfil) | informe de ficheros | sin dudas abiertas sobre los datos |
| 3. Diseño | — | propuesta de modelo | **aprobación expresa de la persona** |
| 4. Construcción | `ingesta`, `diagnostico-reparacion`, `modelado`, `transformacion`, `analitica-avanzada`, `reporting` | tablas, relaciones, vistas y dashboards | cada plan aplicado y cada carga validada |
| 5. Validación | `validacion` | informe de controles | todos los controles en verde |
| 6. Documentación | `documentacion` | comentarios y diccionario | — |

Anuncia cada fase al empezarla ("Fase 3/6 · Diseño").

## Fase 1 · Configuración (y retomar)

1. `estado_bi`, `listar_ficheros`, `listar_esquema` y `listar_planes_pendientes`.
2. Decide el punto de partida:
   - Esquema vacío → empieza en la fase 2.
   - Ya hay tablas o vistas → enséñalas y pregunta si se amplía el modelo o se
     retoma; **nunca** se borra nada para "empezar limpio" (eso solo con petición
     expresa y `planificar_borrado`). Retoma en la primera fase cuyo entregable falte.
   - Planes pendientes de otra sesión → enuméralos y pregunta si se aplican o se
     retiran con `descartar_plan`; no los apliques por tu cuenta.
3. Si la herramienta de BI no está disponible, dilo: se puede construir el modelo y
   dejar los dashboards para cuando lo esté.
4. Aclara el objetivo de negocio en una frase (qué decisiones debe apoyar el
   dashboard). Si la persona no lo da, propón uno y sigue.

## Fase 2 · Descubrimiento

Solo lectura: `perfilar_fichero` de cada fichero relevante. Para cada uno, resume:
filas, columnas clave candidatas, tipos dudosos, duplicados y advertencias. Busca
relaciones posibles por nombre y por significado (p. ej. `ref_producto` ↔ `sku`) y
márcalas como **hipótesis**: hasta cargar las tablas no hay evidencia de valores.

Entregable: una tabla por fichero y una lista de dudas (claves, defectos de la
entrega, reglas de negocio). Si hay defectos (duplicados, columnas renombradas,
cambios de unidad), anótalos para la construcción: se reparan al cargar con la skill
`diagnostico-reparacion`, nunca editando el fichero.

## Fase 3 · Diseño (punto de control)

Presenta la propuesta, breve y completa:

1. **Tablas**: nombre, fichero de origen, clave primaria y reparaciones previstas.
2. **Relaciones**: hija.columna → padre.columna, marcando las que tienen nombres
   distintos (necesitarán confirmación con evidencia en la construcción).
3. **Reglas de negocio** que hay que decidir: qué estados cuentan como venta, moneda,
   fecha de corte, cálculo del importe (convención de CLAUDE.md por defecto).
4. **Vistas**: cada `v_*` con su grano (una fila por…) y la pregunta que responde;
   incluye los análisis de `analitica-avanzada` que aporten al objetivo.
5. **Dashboards**: plantilla de `reporting` en la que se basan y sus secciones.

Termina con: **"¿Apruebas este diseño o quieres cambiar algo?"** y **detente**. No
aplicas ningún plan hasta que la persona lo apruebe en esta conversación. Si pide
cambios, rehaz solo la parte afectada y vuelve a pedir aprobación. Las decisiones de
negocio que tome quedan fijadas para el resto de fases.

## Fase 4 · Construcción

Sigue el diseño aprobado, en este orden y con las reglas de cada skill:

1. Tablas y cargas (`ingesta`; si la carga se bloquea por defectos,
   `diagnostico-reparacion`). Valida cada carga en cuanto se aplique.
2. Claves y relaciones (`modelado`), con `comprobar_relacion` en cada una.
3. Vistas (`transformacion` y `analitica-avanzada`), con las reglas de negocio aprobadas.
4. Preguntas y dashboards (`reporting`).

Si algo obliga a **desviarse del diseño** (una relación con huérfanos, una columna que
no existe, un bloqueo), para, explica la evidencia y pregunta: el cambio de diseño
también se aprueba. Un bloqueo nunca se rodea.

## Fase 5 · Validación

Repaso final, aunque cada carga ya se validara:

- `validar_carga` de cada tabla contra sus ficheros (recuento, sumas, clave, huérfanos).
- `comprobar_relacion` de cada clave foránea.
- Cuadre de cada vista con su origen mediante `consultar_solo_lectura` (p. ej. la
  suma de importes de una vista mensual = la suma por líneas con la misma regla).
- `listar_planes_pendientes`: no debe quedar ninguno; los sobrantes se retiran con
  `descartar_plan`.

Entregable: tabla de controles (control, esperado, obtenido, resultado). Si alguno
falla, vuelve a la fase 4 para corregirlo y repite la validación.

## Fase 6 · Documentación

Skill `documentacion`: descripciones de negocio de tablas, vistas y columnas
(incluidas las reglas aprobadas en la fase 3) y `generar_diccionario`.

## Cierre

Resumen final en una tabla por fase con lo creado (tablas, relaciones, vistas,
dashboards con su enlace), los controles superados con sus cifras (de la fase 5), las
decisiones de negocio tomadas y lo pendiente.
