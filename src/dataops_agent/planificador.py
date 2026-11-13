"""Planificadores: construyen planes con su SQL, su impacto y sus bloqueos.

Ninguna función de este módulo modifica la base de datos: solo la consultan con el
rol LECTOR para estimar el impacto y detectar problemas ANTES de aplicar. Todo el SQL
que generan se compone con identificadores validados y se vuelve a pasar por el
validador sqlglot (defensa en profundidad: aunque el SQL lo genere el propio servidor,
pasa por la misma barrera que el SQL escrito por el agente).
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field

from dataops_agent import catalogo
from dataops_agent.conexion import GestorConexiones
from dataops_agent.config import Config, validar_identificador
from dataops_agent.documentacion import (
    comentario_objeto,
    comentarios_columnas,
    normalizar_texto,
    sql_legible,
)
from dataops_agent.ficheros import FicheroTabular, abrir, huella, nombre_sql
from dataops_agent.perfilado import perfilar
from dataops_agent.planes import AlmacenPlanes, Paso, Plan, TipoPaso, TipoPlan
from dataops_agent.seguridad import Modo, ValidadorSQL
from dataops_agent.transformaciones import OrigenTransformado, Transformacion
from dataops_agent.validaciones import comprobar_unicidad, huerfanos

# Tipos de columna admitidos al crear tablas (lista cerrada).
_TIPOS_PERMITIDOS = re.compile(
    r"^(smallint|integer|bigint|real|double precision|text|boolean|date|timestamp|"
    r"timestamptz|jsonb|numeric(\(\d{1,3}(,\s?\d{1,3})?\))?|varchar\(\d{1,5}\))$"
)
_MAX_ERRORES_TIPO = 5


class ErrorPlanificacion(ValueError):
    """La petición no tiene sentido (p. ej. la tabla no existe). No genera plan."""


class ColumnaDef(BaseModel):
    """Definición de una columna para ``planificar_tabla``."""

    nombre: str = Field(description="Nombre SQL: minúsculas, dígitos y '_'")
    tipo: str = Field(description="integer, bigint, numeric(12,2), text, date, boolean...")
    nulable: bool = True


class Planificador:
    """Crea planes. Cada método devuelve un :class:`Plan` ya guardado en el almacén."""

    def __init__(
        self,
        cfg: Config,
        gestor: GestorConexiones,
        validador: ValidadorSQL,
        almacen: AlmacenPlanes,
    ) -> None:
        self.cfg = cfg
        self.gestor = gestor
        self.validador = validador
        self.almacen = almacen
        self.esquema = cfg.esquema

    # ================================================================ tablas
    def tabla(
        self,
        nombre: str,
        columnas: list[ColumnaDef] | None = None,
        clave_primaria: list[str] | None = None,
        desde_fichero: str | None = None,
    ) -> Plan:
        """Plan para crear una tabla, a partir de columnas explícitas o de un fichero."""
        validar_identificador(nombre, "nombre de tabla")
        advertencias: list[str] = []
        fichero: FicheroTabular | None = None
        if columnas is None:
            if desde_fichero is None:
                raise ErrorPlanificacion("Indica 'columnas' o 'desde_fichero'.")
            fichero = abrir(self.cfg.dir_datos, desde_fichero)
            perfil = perfilar(fichero)
            columnas = [
                ColumnaDef(nombre=c.nombre_sql, tipo=c.tipo_inferido, nulable=c.nulos > 0)
                for c in perfil.columnas
            ]
            advertencias.append(
                "Tipos inferidos del fichero; revísalos antes de aplicar. "
                f"Candidatas a clave: {perfil.candidatas_clave or 'ninguna'}."
            )
        if not columnas:
            raise ErrorPlanificacion("La tabla necesita al menos una columna.")

        bloqueos: list[str] = []
        nombres = [validar_identificador(c.nombre, "columna") for c in columnas]
        if len(set(nombres)) != len(nombres):
            bloqueos.append("Hay columnas con nombre repetido.")
        for c in columnas:
            if not _TIPOS_PERMITIDOS.match(c.tipo.strip().lower()):
                bloqueos.append(f"Tipo no permitido para {c.nombre}: {c.tipo!r}.")
        clave_primaria = clave_primaria or []
        for c in clave_primaria:
            if c not in nombres:
                bloqueos.append(f"La clave primaria usa una columna inexistente: {c}.")

        with self.gestor.lectura() as conn:
            if catalogo.tipo_objeto(conn, self.esquema, nombre):
                bloqueos.append(f"Ya existe un objeto llamado {nombre} en {self.esquema}.")

        if fichero is not None and clave_primaria and not bloqueos:
            repetidas = _claves_repetidas(fichero, clave_primaria)
            if repetidas:
                bloqueos.append(
                    f"La clave {clave_primaria} se repite en el fichero "
                    f"({len(repetidas)} valores, p. ej. {repetidas[:3]})."
                )

        definiciones = [
            f"{c.nombre} {c.tipo.strip().lower()}{'' if c.nulable else ' NOT NULL'}"
            for c in columnas
        ]
        if clave_primaria:
            definiciones.append(
                f"CONSTRAINT {nombre}_pk PRIMARY KEY ({', '.join(clave_primaria)})"
            )
        sentencia = f"CREATE TABLE {self.esquema}.{nombre} (\n    " + ",\n    ".join(
            definiciones
        ) + "\n)"
        return self._guardar(
            Plan(
                tipo=TipoPlan.CREAR_TABLA,
                resumen=f"Crear la tabla {self.esquema}.{nombre} con {len(columnas)} columnas",
                pasos=[Paso(TipoPaso.SQL, f"Crear tabla {nombre}", sentencia)],
                impacto={
                    "objeto_nuevo": f"{self.esquema}.{nombre}",
                    "columnas": len(columnas),
                    "clave_primaria": clave_primaria,
                },
                bloqueos=bloqueos,
                advertencias=advertencias,
            )
        )

    # ================================================================= carga
    def carga(
        self,
        tabla: str,
        fichero: str,
        modo: str = "anadir",
        transformacion: Transformacion | None = None,
    ) -> Plan:
        """Plan para cargar un fichero en una tabla existente.

        ``modo``: 'anadir' (inserta) o 'reemplazar' (vacía la tabla antes: DESTRUCTIVO).
        ``transformacion``: reparación declarativa (renombrar, dividir, deduplicar) que
        se aplica al leer el fichero; el fichero entregado no se modifica.
        """
        validar_identificador(tabla, "tabla")
        if modo not in {"anadir", "reemplazar"}:
            raise ErrorPlanificacion("modo debe ser 'anadir' o 'reemplazar'.")
        bloqueos: list[str] = []
        advertencias: list[str] = []
        efecto: dict[str, Any] | None = None
        origen: FicheroTabular | OrigenTransformado = abrir(self.cfg.dir_datos, fichero)
        if transformacion is not None:
            origen = OrigenTransformado(origen, transformacion)
            efecto = origen.efecto()
            advertencias.append(
                "Carga con transformación: el fichero original no se modifica; la "
                "transformación queda en el plan y en la auditoría."
            )

        with self.gestor.lectura() as conn:
            if catalogo.tipo_objeto(conn, self.esquema, tabla) != "tabla":
                raise ErrorPlanificacion(f"No existe la tabla {self.esquema}.{tabla}.")
            cols_tabla = {c.nombre: c for c in catalogo.columnas(conn, self.esquema, tabla)}
            clave = catalogo.clave_primaria(conn, self.esquema, tabla)
            filas_actuales = catalogo.contar(conn, self.esquema, tabla)

            # 1) Correspondencia de columnas fichero → tabla (por nombre normalizado).
            mapeo: list[tuple[str, str]] = []
            en_fichero = {nombre_sql(c) for c in origen.columnas}
            sin_origen = [c for c in cols_tabla if c not in en_fichero]
            for original in origen.columnas:
                destino = nombre_sql(original)
                if destino in cols_tabla:
                    mapeo.append((original, destino))
                else:
                    parecida = _columna_parecida(destino, sin_origen)
                    pista = f" ¿Quizá se renombró '{parecida}'?" if parecida else ""
                    bloqueos.append(
                        f"La columna '{original}' del fichero no existe en {tabla}.{pista}"
                    )
            presentes = {d for _, d in mapeo}
            for nombre, col in cols_tabla.items():
                if nombre not in presentes and not col.nulable:
                    bloqueos.append(f"Falta en el fichero la columna obligatoria '{nombre}'.")

            # 2) Recorrido del fichero: recuento, tipos y claves repetidas.
            n_filas, errores_tipo, claves_fichero = _revisar_filas(
                origen, mapeo, {d: cols_tabla[d].tipo for _, d in mapeo}, clave
            )
            bloqueos += errores_tipo
            repetidas = [k for k, n in claves_fichero.items() if n > 1]
            if repetidas:
                bloqueos.append(
                    f"La clave {clave} se repite dentro del fichero "
                    f"({len(repetidas)} valores, p. ej. {[list(k) for k in repetidas[:3]]})."
                )
            # 3) Solapamiento con filas ya existentes (solo en modo añadir).
            if modo == "anadir" and len(clave) == 1 and claves_fichero and filas_actuales:
                ya = _contar_existentes(conn, self.esquema, tabla, clave[0], claves_fichero)
                if ya:
                    bloqueos.append(
                        f"{ya} filas del fichero ya existen en {tabla} (misma clave {clave[0]})."
                    )
            # 4) Integridad referencial: valores del fichero sin fila en la tabla padre.
            #    Sin esta comprobación, la carga fallaría al aplicar por la clave foránea.
            origen_de = {destino: original for original, destino in mapeo}
            huerfanos_carga: dict[str, int] = {}
            for fk in catalogo.claves_foraneas(conn, self.esquema):
                if fk["tabla"] != tabla or fk["tabla_referenciada"] == tabla:
                    continue
                if len(fk["columnas"]) != 1:
                    advertencias.append(
                        f"La clave foránea compuesta {fk['nombre']} no se comprueba al planificar."
                    )
                    continue
                columna, padre = fk["columnas"][0], fk["tabla_referenciada"]
                columna_padre = fk["columnas_referenciadas"][0]
                if columna not in origen_de:
                    continue
                valores = _valores_columna(origen, origen_de[columna])
                faltan = _valores_sin_padre(conn, self.esquema, padre, columna_padre, valores)
                if faltan:
                    filas_sin_padre = sum(valores[v] for v in faltan)
                    huerfanos_carga[fk["nombre"]] = filas_sin_padre
                    bloqueos.append(
                        f"{filas_sin_padre} filas del fichero tienen un {columna} que no existe "
                        f"en {padre}.{columna_padre} ({len(faltan)} valores distintos, p. ej. "
                        f"{_ejemplos(faltan)}). La clave foránea {fk['nombre']} haría fallar "
                        f"la carga: carga antes las filas de {padre}."
                    )
        if n_filas == 0:
            advertencias.append("El fichero no tiene filas.")
        if not clave:
            advertencias.append(
                f"{tabla} no tiene clave primaria: no se pueden detectar duplicados al cargar."
            )

        pasos: list[Paso] = []
        if modo == "reemplazar":
            pasos.append(
                Paso(TipoPaso.SQL, f"Vaciar {tabla} (se pierden {filas_actuales} filas)",
                     f"TRUNCATE {self.esquema}.{tabla}")
            )
        lista_cols = ", ".join(d for _, d in mapeo)
        pasos.append(
            Paso(
                TipoPaso.CARGA,
                f"Cargar {n_filas} filas de {origen.ruta.name}",
                f"COPY {self.esquema}.{tabla} ({lista_cols}) FROM STDIN  "
                f"-- datos de {origen.ruta.name}",
                tabla=tabla,
                fichero=str(origen.ruta),
                columnas=tuple(mapeo),
                filas_esperadas=n_filas,
                huella_fichero=huella(origen.ruta),
                transformacion=transformacion,
            )
        )
        filas_finales = n_filas + (0 if modo == "reemplazar" else filas_actuales)
        return self._guardar(
            Plan(
                tipo=TipoPlan.CARGA,
                resumen=(
                    f"Cargar {origen.ruta.name} en {self.esquema}.{tabla} ({modo}"
                    f"{', con transformación' if transformacion else ''})"
                ),
                pasos=pasos,
                impacto={
                    "filas_a_insertar": n_filas,
                    "filas_actuales": filas_actuales,
                    "filas_eliminadas": filas_actuales if modo == "reemplazar" else 0,
                    "filas_tras_aplicar": filas_finales,
                    "columnas_cargadas": [d for _, d in mapeo],
                    "filas_sin_padre": huerfanos_carga,
                    **({"transformacion": efecto} if efecto else {}),
                },
                destructivo=modo == "reemplazar" and filas_actuales > 0,
                bloqueos=bloqueos,
                advertencias=advertencias,
            )
        )

    # ======================================================= clave primaria
    def clave(self, tabla: str, columnas: list[str]) -> Plan:
        """Plan para añadir una clave primaria a una tabla existente."""
        validar_identificador(tabla, "tabla")
        cols = [validar_identificador(c, "columna") for c in columnas]
        if not cols:
            raise ErrorPlanificacion("Indica al menos una columna.")
        bloqueos: list[str] = []
        with self.gestor.lectura() as conn:
            if catalogo.tipo_objeto(conn, self.esquema, tabla) != "tabla":
                raise ErrorPlanificacion(f"No existe la tabla {self.esquema}.{tabla}.")
            existentes = {c.nombre for c in catalogo.columnas(conn, self.esquema, tabla)}
            if faltan := [c for c in cols if c not in existentes]:
                raise ErrorPlanificacion(f"Columnas inexistentes en {tabla}: {faltan}")
            if catalogo.clave_primaria(conn, self.esquema, tabla):
                bloqueos.append(f"{tabla} ya tiene clave primaria.")
            unicidad = comprobar_unicidad(conn, self.esquema, tabla, cols)
            if not unicidad.ok:
                bloqueos.append(f"Las columnas no identifican filas: {unicidad.obtenido}.")
        sentencia = (
            f"ALTER TABLE {self.esquema}.{tabla} ADD CONSTRAINT {tabla}_pk "
            f"PRIMARY KEY ({', '.join(cols)})"
        )
        return self._guardar(
            Plan(
                tipo=TipoPlan.CLAVE,
                resumen=f"Clave primaria de {tabla}: ({', '.join(cols)})",
                pasos=[Paso(TipoPaso.SQL, "Añadir clave primaria", sentencia)],
                impacto={"tabla": tabla, "columnas": cols},
                bloqueos=bloqueos,
            )
        )

    # ============================================================ relaciones
    def relacion(
        self, tabla_hija: str, columna_hija: str, tabla_padre: str, columna_padre: str
    ) -> Plan:
        """Plan de clave foránea. Comprueba integridad y huérfanos ANTES de crearla."""
        for valor, que in [
            (tabla_hija, "tabla hija"), (columna_hija, "columna hija"),
            (tabla_padre, "tabla padre"), (columna_padre, "columna padre"),
        ]:
            validar_identificador(valor, que)
        bloqueos: list[str] = []
        advertencias: list[str] = []
        with self.gestor.lectura() as conn:
            for t in (tabla_hija, tabla_padre):
                if catalogo.tipo_objeto(conn, self.esquema, t) != "tabla":
                    raise ErrorPlanificacion(f"No existe la tabla {self.esquema}.{t}.")
            tipos_h = {c.nombre: c.tipo for c in catalogo.columnas(conn, self.esquema, tabla_hija)}
            tipos_p = {c.nombre: c.tipo for c in catalogo.columnas(conn, self.esquema, tabla_padre)}
            if columna_hija not in tipos_h:
                raise ErrorPlanificacion(f"No existe {tabla_hija}.{columna_hija}.")
            if columna_padre not in tipos_p:
                raise ErrorPlanificacion(f"No existe {tabla_padre}.{columna_padre}.")

            if _familia(tipos_h[columna_hija]) != _familia(tipos_p[columna_padre]):
                bloqueos.append(
                    f"Tipos incompatibles: {tabla_hija}.{columna_hija} es "
                    f"{tipos_h[columna_hija]} y {tabla_padre}.{columna_padre} es "
                    f"{tipos_p[columna_padre]}."
                )
            if [columna_padre] not in catalogo.conjuntos_unicos(conn, self.esquema, tabla_padre):
                bloqueos.append(
                    f"{tabla_padre}.{columna_padre} no es clave primaria ni única: "
                    "planifica antes su clave primaria (planificar_clave)."
                )
            for fk in catalogo.claves_foraneas(conn, self.esquema):
                if fk["tabla"] == tabla_hija and fk["columnas"] == [columna_hija]:
                    bloqueos.append(f"Ya existe la clave foránea {fk['nombre']}.")

            evidencia: dict[str, object] = {}
            if not any("incompatibles" in b for b in bloqueos):
                evidencia = huerfanos(
                    conn, self.esquema, tabla_hija, columna_hija, tabla_padre, columna_padre
                )
                if evidencia["huerfanos"]:
                    bloqueos.append(
                        f"{evidencia['huerfanos']} filas de {tabla_hija} apuntan a valores "
                        f"inexistentes en {tabla_padre}.{columna_padre} "
                        f"(p. ej. {evidencia['muestra_huerfanos']}). No se crea la relación: "
                        "pregunta a la persona cómo tratarlos."
                    )
                if evidencia["filas_hijas_con_valor"] == 0:
                    advertencias.append(
                        f"{tabla_hija}.{columna_hija} no tiene valores: no hay evidencia "
                        "de que la relación sea correcta."
                    )
        nombre_fk = f"fk_{tabla_hija}_{columna_hija}"[:63]
        sentencia = (
            f"ALTER TABLE {self.esquema}.{tabla_hija} ADD CONSTRAINT {nombre_fk} "
            f"FOREIGN KEY ({columna_hija}) REFERENCES {self.esquema}.{tabla_padre} "
            f"({columna_padre})"
        )
        return self._guardar(
            Plan(
                tipo=TipoPlan.RELACION,
                resumen=(
                    f"Relación {tabla_hija}.{columna_hija} → {tabla_padre}.{columna_padre}"
                ),
                pasos=[Paso(TipoPaso.SQL, f"Crear clave foránea {nombre_fk}", sentencia)],
                impacto={"evidencia": evidencia},
                bloqueos=bloqueos,
                advertencias=advertencias,
            )
        )

    # ================================================================= vistas
    def vista(self, nombre: str, consulta: str, reemplazar: bool = False) -> Plan:
        """Plan para crear (o redefinir) una vista analítica a partir de un SELECT."""
        validar_identificador(nombre, "nombre de vista")
        consulta = consulta.strip().rstrip(";").strip()
        self.validador.exigir(consulta, Modo.LECTURA)
        bloqueos: list[str] = []
        advertencias: list[str] = []
        destructivo = False
        pasos: list[Paso] = []
        impacto: dict[str, object] = {}

        with self.gestor.lectura() as conn:
            existente = catalogo.tipo_objeto(conn, self.esquema, nombre)
            columnas_nuevas, filas, error = _probar_consulta(conn, consulta)
            if error:
                bloqueos.append(f"La consulta falla en PostgreSQL: {error}")
            impacto = {"columnas": columnas_nuevas, "filas_resultado": filas}
            if existente == "tabla":
                bloqueos.append(f"{nombre} es una tabla, no una vista.")
            elif existente == "vista" and not reemplazar:
                bloqueos.append(
                    f"La vista {nombre} ya existe; usa reemplazar=true para redefinirla."
                )
            elif existente == "vista":
                actuales = [c.nombre for c in catalogo.columnas(conn, self.esquema, nombre)]
                dependientes = catalogo.dependientes(conn, self.esquema, nombre)
                impacto["columnas_actuales"] = actuales
                impacto["dependientes"] = dependientes
                # CREATE OR REPLACE VIEW solo admite AÑADIR columnas al final.
                if columnas_nuevas[: len(actuales)] != actuales and not error:
                    if dependientes:
                        bloqueos.append(
                            "Cambiar las columnas exige borrar la vista, pero otros objetos "
                            f"dependen de ella: {dependientes}."
                        )
                    destructivo = True
                    pasos.append(
                        Paso(TipoPaso.SQL, f"Borrar la vista {nombre} (cambian sus columnas)",
                             f"DROP VIEW {self.esquema}.{nombre}")
                    )
                advertencias.append(
                    "Las preguntas de BI que usan esta vista mostrarán la nueva definición."
                )
        verbo = "CREATE VIEW" if destructivo or not existente else "CREATE OR REPLACE VIEW"
        pasos.append(
            Paso(TipoPaso.SQL, f"Definir la vista {nombre}",
                 f"{verbo} {self.esquema}.{nombre} AS\n{consulta}")
        )
        return self._guardar(
            Plan(
                tipo=TipoPlan.VISTA,
                resumen=f"{'Redefinir' if existente else 'Crear'} la vista {self.esquema}.{nombre}",
                pasos=pasos,
                impacto=impacto,
                destructivo=destructivo,
                bloqueos=bloqueos,
                advertencias=advertencias,
            )
        )

    # ================================================================ borrados
    def borrado(self, tipo: str, objeto: str, condicion: str | None = None) -> Plan:
        """Plan DESTRUCTIVO: 'tabla' (DROP), 'vista' (DROP), 'vaciar' (TRUNCATE)
        o 'filas' (DELETE ... WHERE condicion). Nunca usa CASCADE."""
        validar_identificador(objeto, "objeto")
        if tipo not in {"tabla", "vista", "vaciar", "filas"}:
            raise ErrorPlanificacion("tipo debe ser 'tabla', 'vista', 'vaciar' o 'filas'.")
        bloqueos: list[str] = []
        filas_hijas: dict[str, int] = {}
        destino = f"{self.esquema}.{objeto}"
        with self.gestor.lectura() as conn:
            real = catalogo.tipo_objeto(conn, self.esquema, objeto)
            esperado = "vista" if tipo == "vista" else "tabla"
            if real != esperado:
                raise ErrorPlanificacion(f"No existe la {esperado} {destino}.")
            dependientes = catalogo.dependientes(conn, self.esquema, objeto)
            filas = catalogo.contar(conn, self.esquema, objeto)
            if tipo in {"tabla", "vista"}:
                sentencia = f"DROP {'VIEW' if tipo == 'vista' else 'TABLE'} {destino}"
                afectadas = filas
                if dependientes:
                    bloqueos.append(
                        f"Otros objetos dependen de {objeto}: {dependientes}. "
                        "No se usa CASCADE: bórralos antes, uno a uno, si procede."
                    )
            elif tipo == "vaciar":
                sentencia = f"TRUNCATE {destino}"
                afectadas = filas
                if any(d.startswith("clave foránea") for d in dependientes):
                    bloqueos.append(f"Hay claves foráneas que referencian {objeto}.")
            else:
                if not condicion or not condicion.strip():
                    raise ErrorPlanificacion("Un borrado de filas necesita una condición WHERE.")
                condicion = condicion.strip().rstrip(";")
                self.validador.exigir(
                    f"SELECT count(*) FROM {destino} WHERE {condicion}", Modo.LECTURA
                )
                sentencia = f"DELETE FROM {destino} WHERE {condicion}"
                afectadas = conn.execute(
                    f"SELECT count(*) FROM {destino} WHERE {condicion}"  # validado arriba
                ).fetchone()[0]  # type: ignore[index]
                # Filas hijas que referencian las filas a borrar: sin CASCADE, el DELETE
                # fallaría al aplicar. Se cuentan y se bloquea con la cifra.
                for nombre_fk, hija, hijas in self._filas_hijas(conn, objeto, condicion):
                    filas_hijas[nombre_fk] = hijas
                    if hijas:
                        bloqueos.append(
                            f"{hijas} filas de {hija} referencian las filas a borrar "
                            f"(clave foránea {nombre_fk}). No se usa CASCADE: "
                            "bórralas antes, si procede."
                        )
        return self._guardar(
            Plan(
                tipo=TipoPlan.BORRADO,
                resumen=f"DESTRUCTIVO: {sentencia}",
                pasos=[Paso(TipoPaso.SQL, f"Operación destructiva sobre {objeto}", sentencia)],
                impacto={
                    "filas_afectadas": afectadas,
                    "dependientes": dependientes,
                    "filas_hijas_afectadas": filas_hijas,
                },
                destructivo=True,
                bloqueos=bloqueos,
                advertencias=["Operación irreversible: requiere aprobación explícita."],
            )
        )

    # ========================================================= documentación
    def documentacion(self, descripciones: dict[str, Any]) -> Plan:
        """Plan para guardar descripciones como comentarios del modelo (``COMMENT ON``).

        ``descripciones``: {objeto: {"descripcion": "...", "columnas": {col: "..."}}}.
        No es destructivo: solo cambia metadatos. Si ya había un comentario distinto,
        el plan lo enseña (antes → después) como advertencia.
        """
        if not descripciones:
            raise ErrorPlanificacion("No hay ninguna descripción que guardar.")
        pasos: list[Paso] = []
        advertencias: list[str] = []
        reemplazos: list[dict[str, str]] = []
        with self.gestor.lectura() as conn:
            existentes = catalogo.objetos(conn, self.esquema)
            for objeto, contenido in descripciones.items():
                validar_identificador(objeto, "objeto")
                tipo = existentes.get(objeto)
                if tipo is None:
                    raise ErrorPlanificacion(f"No existe {self.esquema}.{objeto}.")
                if not isinstance(contenido, dict):
                    raise ErrorPlanificacion(
                        f"Para {objeto} se espera {{'descripcion': ..., 'columnas': {{...}}}}."
                    )
                columnas_reales = {c.nombre for c in catalogo.columnas(conn, self.esquema, objeto)}
                previas = comentarios_columnas(conn, self.esquema, objeto)
                entradas: list[tuple[str | None, str]] = []
                if contenido.get("descripcion"):
                    entradas.append((None, contenido["descripcion"]))
                for columna, texto in (contenido.get("columnas") or {}).items():
                    if columna not in columnas_reales:
                        raise ErrorPlanificacion(
                            f"{objeto} no tiene la columna {columna}. "
                            f"Columnas: {sorted(columnas_reales)}."
                        )
                    entradas.append((columna, texto))
                for columna, texto in entradas:
                    donde = f"{objeto}.{columna}" if columna else objeto
                    limpio = normalizar_texto(texto, donde)
                    anterior = (
                        previas.get(columna)
                        if columna
                        else comentario_objeto(conn, self.esquema, objeto)
                    )
                    if anterior == limpio:
                        continue
                    if anterior:
                        reemplazos.append({"objeto": donde, "antes": anterior, "despues": limpio})
                    pasos.append(
                        Paso(
                            TipoPaso.COMENTARIO,
                            f"Describir {donde}",
                            sql_legible(self.esquema, objeto, columna, limpio, tipo),
                            tabla=objeto,
                            columna=columna,
                            texto=limpio,
                        )
                    )
        if not pasos:
            raise ErrorPlanificacion("Todas las descripciones ya estaban guardadas tal cual.")
        if reemplazos:
            advertencias.append(
                f"Se sustituyen {len(reemplazos)} descripciones existentes "
                "(ver impacto.reemplaza)."
            )
        return self._guardar(
            Plan(
                tipo=TipoPlan.DOCUMENTACION,
                resumen=f"Documentar {len(pasos)} elementos del modelo en {self.esquema}",
                pasos=pasos,
                impacto={
                    "objetos": sorted({p.tabla for p in pasos if p.tabla}),
                    "descripciones": len(pasos),
                    "reemplaza": reemplazos,
                },
                advertencias=advertencias,
            )
        )

    # ============================================================== internos
    def _filas_hijas(
        self, conn: psycopg.Connection, tabla: str, condicion: str
    ) -> list[tuple[str, str, int]]:
        """Por cada clave foránea que referencia ``tabla`` (salvo autorreferencias),
        cuántas filas de la tabla hija apuntan a las filas que cumplen ``condicion``."""
        resultado: list[tuple[str, str, int]] = []
        for fk in catalogo.claves_foraneas(conn, self.esquema):
            if fk["tabla_referenciada"] != tabla or fk["tabla"] == tabla:
                continue
            hija = validar_identificador(fk["tabla"], "tabla")
            cols_hija = [validar_identificador(c, "columna") for c in fk["columnas"]]
            cols_padre = [validar_identificador(c, "columna") for c in fk["columnas_referenciadas"]]
            izquierda = ", ".join(f"h.{c}" for c in cols_hija)
            consulta = (
                f"SELECT count(*) FROM {self.esquema}.{hija} AS h "
                f"WHERE ({izquierda}) IN (SELECT {', '.join(cols_padre)} "
                f"FROM {self.esquema}.{tabla} WHERE {condicion})"
            )
            self.validador.exigir(consulta, Modo.LECTURA)
            fila = conn.execute(consulta).fetchone()  # validada arriba
            resultado.append((fk["nombre"], hija, int(fila[0]) if fila else 0))
        return resultado

    def _guardar(self, plan: Plan) -> Plan:
        """Revalida cada paso SQL con sqlglot antes de guardar el plan."""
        for paso in plan.pasos:
            if paso.tipo is TipoPaso.SQL:
                resultado = self.validador.validar(paso.sql, Modo.ESCRITURA)
                if not resultado.valido:
                    plan.bloqueos.append(f"SQL rechazado por el validador: {resultado.errores}")
        plan.huella = plan.calcular_huella()
        return self.almacen.guardar(plan)


# ================================================================== utilidades
def _familia(tipo: str) -> str:
    """Agrupa tipos compatibles para claves foráneas."""
    t = tipo.lower()
    if t in {"smallint", "integer", "bigint"}:
        return "entero"
    if t.startswith(("numeric", "real", "double")):
        return "numerico"
    if t.startswith(("text", "character varying", "varchar", "character")):
        return "texto"
    return t


def _columna_parecida(nombre: str, candidatas: list[str]) -> str | None:
    """Sugiere qué columna de la tabla (sin dato en el fichero) pudo renombrarse.

    Primero por inclusión ("precio" ⊂ "precio_unitario"), después por similitud.
    Es solo una pista para la persona: nunca se renombra nada automáticamente.
    """
    contenidas = [c for c in candidatas if nombre in c or c in nombre]
    if len(contenidas) == 1:
        return contenidas[0]
    parecidas = difflib.get_close_matches(nombre, candidatas, n=1, cutoff=0.5)
    return parecidas[0] if parecidas else None


def _valor_valido(valor: str, tipo: str) -> bool:
    """Comprueba en Python si PostgreSQL aceptará el texto para ese tipo."""
    t = tipo.lower()
    v = valor.strip()
    try:
        if t in {"smallint", "integer", "bigint"}:
            int(v)
        elif t.startswith(("numeric", "real", "double")):
            float(v)
            if not re.match(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$", v):
                return False
        elif t == "boolean":
            return v.lower() in {"true", "false", "t", "f", "1", "0", "yes", "no", "y", "n"}
        elif t == "date":
            date.fromisoformat(v)
        elif t.startswith("timestamp"):
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        elif m := re.match(r"character varying\((\d+)\)", t):
            return len(v) <= int(m.group(1))
    except ValueError:
        return False
    return True


def _revisar_filas(
    origen: FicheroTabular | OrigenTransformado,
    mapeo: list[tuple[str, str]],
    tipos: dict[str, str],
    clave: list[str],
) -> tuple[int, list[str], Counter[tuple[str | None, ...]]]:
    """Una pasada por el fichero: recuento, errores de tipo y claves vistas."""
    errores: list[str] = []
    por_columna: Counter[str] = Counter()
    claves: Counter[tuple[str | None, ...]] = Counter()
    origen_de = {destino: original for original, destino in mapeo}
    usa_clave = bool(clave) and all(c in origen_de for c in clave)
    n = 0
    for fila in origen.filas():
        n += 1
        for original, destino in mapeo:
            valor = fila.get(original)
            if valor is not None and not _valor_valido(valor, tipos[destino]):
                por_columna[destino] += 1
                if por_columna[destino] <= _MAX_ERRORES_TIPO:
                    errores.append(
                        f"Fila {n}: '{valor}' no es válido para {destino} ({tipos[destino]})."
                    )
        if usa_clave:
            claves[tuple(fila.get(origen_de[c]) for c in clave)] += 1
    for columna, total in por_columna.items():
        if total > _MAX_ERRORES_TIPO:
            errores.append(f"... {total} valores no válidos en total en {columna}.")
    return n, errores, claves


def _claves_repetidas(fichero: FicheroTabular, clave: list[str]) -> list[tuple[str | None, ...]]:
    origen_de = {nombre_sql(c): c for c in fichero.columnas}
    contador: Counter[tuple[str | None, ...]] = Counter(
        tuple(fila.get(origen_de.get(c, c)) for c in clave) for fila in fichero.filas()
    )
    return [k for k, n in contador.items() if n > 1]


def _contar_existentes(
    conn: psycopg.Connection,
    esquema: str,
    tabla: str,
    columna: str,
    claves: Iterable[tuple[str | None, ...]],
) -> int:
    valores = [k[0] for k in claves if k[0] is not None]
    fila = conn.execute(
        sql.SQL("SELECT count(*) FROM {} WHERE {}::text = ANY(%s)").format(
            catalogo.ident(esquema, tabla), sql.Identifier(columna)
        ),
        (valores,),
    ).fetchone()
    return int(fila[0]) if fila else 0


def _valores_columna(
    origen: FicheroTabular | OrigenTransformado, columna_original: str
) -> Counter[str]:
    """Recuento de cada valor no nulo de una columna del fichero."""
    return Counter(
        valor for fila in origen.filas() if (valor := fila.get(columna_original)) is not None
    )


def _valores_sin_padre(
    conn: psycopg.Connection,
    esquema: str,
    padre: str,
    columna_padre: str,
    valores: Iterable[str],
) -> list[str]:
    """Valores (como texto) que no existen en ``padre.columna_padre``."""
    lista = list(valores)
    if not lista:
        return []
    filas = conn.execute(
        sql.SQL(
            "SELECT v FROM unnest(%s::text[]) AS v "
            "WHERE NOT EXISTS (SELECT 1 FROM {} AS p WHERE p.{}::text = v)"
        ).format(catalogo.ident(esquema, padre), sql.Identifier(columna_padre)),
        (lista,),
    ).fetchall()
    return sorted((f[0] for f in filas), key=_orden_natural)


def _orden_natural(valor: str) -> tuple[int, int | str]:
    return (0, int(valor)) if valor.lstrip("-").isdigit() else (1, valor)


def _ejemplos(valores: list[str], n: int = 3) -> str:
    """Muestra los primeros valores y, si son enteros, el rango completo."""
    if all(v.lstrip("-").isdigit() for v in valores) and len(valores) > n:
        return f"{', '.join(valores[:n])}…; rango {valores[0]}–{valores[-1]}"
    return ", ".join(valores[:n]) + ("…" if len(valores) > n else "")


def _probar_consulta(
    conn: psycopg.Connection, consulta: str
) -> tuple[list[str], int | None, str | None]:
    """Ejecuta la consulta (ya validada) para conocer columnas y nº de filas."""
    try:
        with conn.transaction():
            cursor = conn.execute(f"SELECT * FROM ({consulta}) AS consulta LIMIT 0")
            columnas = [d.name for d in cursor.description or []]
            filas = conn.execute(f"SELECT count(*) FROM ({consulta}) AS consulta").fetchone()
        return columnas, int(filas[0]) if filas else None, None
    except psycopg.Error as error:
        return [], None, str(error).strip().splitlines()[0]
