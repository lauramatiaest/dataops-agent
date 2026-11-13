"""Patrón planificar → aplicar contra PostgreSQL real."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from dataops_agent.aplicador import Aplicador, ErrorVerificacion, RequiereConfirmacion
from dataops_agent.conexion import GestorConexiones
from dataops_agent.config import Config
from dataops_agent.documentacion import describir_modelo, diccionario_markdown
from dataops_agent.ficheros import abrir
from dataops_agent.planes import AlmacenPlanes, ErrorPlan
from dataops_agent.planificador import ColumnaDef, ErrorPlanificacion, Planificador
from dataops_agent.seguridad import SQLRechazado, ValidadorSQL
from dataops_agent.transformaciones import OrigenTransformado, Transformacion
from dataops_agent.validaciones import validar_carga

CLIENTES = "cliente_id,nombre,ciudad\n1,Ana,Madrid\n2,Luis,Sevilla\n3,Eva,Madrid\n"
PEDIDOS = "pedido_id,cliente_id,importe,estado\n10,1,100.50,entregado\n11,1,20.00,cancelado\n12,2,5.25,entregado\n"


@pytest.fixture
def piezas(cfg: Config) -> tuple[Planificador, Aplicador]:
    gestor = GestorConexiones(cfg)
    validador = ValidadorSQL(cfg.esquema)
    almacen = AlmacenPlanes(timedelta(minutes=30))
    return (
        Planificador(cfg, gestor, validador, almacen),
        Aplicador(cfg, gestor, validador, almacen),
    )


def _fichero(cfg: Config, nombre: str, contenido: str) -> str:
    (cfg.dir_datos / nombre).write_text(contenido, encoding="utf-8")
    return nombre


def _cargar_base(cfg: Config, plan: Planificador, apl: Aplicador) -> None:
    """Deja creadas y cargadas clientes y pedidos."""
    _fichero(cfg, "clientes.csv", CLIENTES)
    _fichero(cfg, "pedidos.csv", PEDIDOS)
    apl.aplicar(plan.tabla("clientes", desde_fichero="clientes.csv", clave_primaria=["cliente_id"]).id)
    apl.aplicar(plan.tabla("pedidos", desde_fichero="pedidos.csv", clave_primaria=["pedido_id"]).id)
    apl.aplicar(plan.carga("clientes", "clientes.csv").id)
    apl.aplicar(plan.carga("pedidos", "pedidos.csv").id)


def test_tabla_desde_fichero_infiere_tipos(cfg: Config, piezas, conn_admin) -> None:
    plan, apl = piezas
    _fichero(cfg, "pedidos.csv", PEDIDOS)
    p = plan.tabla("pedidos", desde_fichero="pedidos.csv", clave_primaria=["pedido_id"])
    assert p.aplicable, p.bloqueos
    assert "importe numeric(12,2) NOT NULL" in p.sql_completo()
    # Planificar no cambia nada:
    assert conn_admin.execute("SELECT to_regclass('proyecto_test.pedidos')").fetchone()[0] is None
    resultado = apl.aplicar(p.id)
    assert resultado["estado"] == "aplicado"
    assert conn_admin.execute("SELECT to_regclass('proyecto_test.pedidos')").fetchone()[0]


def test_tabla_con_columnas_explicitas_y_errores(piezas) -> None:
    plan, _ = piezas
    p = plan.tabla(
        "t",
        columnas=[ColumnaDef(nombre="id", tipo="integer", nulable=False),
                  ColumnaDef(nombre="x", tipo="money")],
        clave_primaria=["id", "no_existe"],
    )
    assert not p.aplicable
    assert any("money" in b for b in p.bloqueos)
    assert any("no_existe" in b for b in p.bloqueos)
    with pytest.raises(ValueError):
        plan.tabla("Mala-Tabla", columnas=[ColumnaDef(nombre="id", tipo="integer")])


def test_clave_repetida_en_fichero_bloquea(cfg: Config, piezas) -> None:
    plan, _ = piezas
    _fichero(cfg, "dup.csv", "id,v\n1,a\n1,b\n")
    p = plan.tabla("dup", desde_fichero="dup.csv", clave_primaria=["id"])
    assert any("se repite" in b for b in p.bloqueos)


def test_carga_y_verificacion(cfg: Config, piezas, conn_admin) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    assert conn_admin.execute("SELECT count(*), sum(importe) FROM proyecto_test.pedidos").fetchone() == (
        3, pytest.approx(125.75)
    )


def test_carga_detecta_columna_renombrada_y_tipos(cfg: Config, piezas) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    _fichero(cfg, "pedidos_mal.csv", "pedido_id,cliente_id,importe_total,estado\n20,1,abc,x\n")
    p = plan.carga("pedidos", "pedidos_mal.csv")
    assert any("importe_total" in b and "importe" in b for b in p.bloqueos)
    _fichero(cfg, "pedidos_tipo.csv", "pedido_id,cliente_id,importe,estado\n21,1,1.2.3,entregado\n")
    assert any("no es válido" in b for b in plan.carga("pedidos", "pedidos_tipo.csv").bloqueos)


def test_carga_duplicada_bloqueada(cfg: Config, piezas) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    p = plan.carga("pedidos", "pedidos.csv")  # mismas claves otra vez
    assert any("ya existen" in b for b in p.bloqueos)


def test_reemplazar_es_destructivo_y_exige_confirmacion(cfg: Config, piezas, conn_admin) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    p = plan.carga("pedidos", "pedidos.csv", modo="reemplazar")
    assert p.destructivo and p.impacto["filas_eliminadas"] == 3
    with pytest.raises(RequiereConfirmacion):
        apl.aplicar(p.id)
    assert conn_admin.execute("SELECT count(*) FROM proyecto_test.pedidos").fetchone()[0] == 3
    apl.aplicar(p.id, permitir_destructivo=True)
    assert conn_admin.execute("SELECT count(*) FROM proyecto_test.pedidos").fetchone()[0] == 3


def test_fichero_modificado_tras_planificar(cfg: Config, piezas, conn_admin) -> None:
    plan, apl = piezas
    _fichero(cfg, "clientes.csv", CLIENTES)
    apl.aplicar(plan.tabla("clientes", desde_fichero="clientes.csv").id)
    p = plan.carga("clientes", "clientes.csv")
    _fichero(cfg, "clientes.csv", CLIENTES + "4,Nuevo,Bilbao\n")
    with pytest.raises(ErrorVerificacion, match="ha cambiado"):
        apl.aplicar(p.id)
    # Transacción deshecha: no se cargó nada.
    assert conn_admin.execute("SELECT count(*) FROM proyecto_test.clientes").fetchone()[0] == 0


def test_relacion_con_huerfanos_se_bloquea(cfg: Config, piezas, conn_admin) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    _fichero(cfg, "pedidos2.csv", "pedido_id,cliente_id,importe,estado\n13,99,1.00,entregado\n")
    apl.aplicar(plan.carga("pedidos", "pedidos2.csv").id)
    p = plan.relacion("pedidos", "cliente_id", "clientes", "cliente_id")
    assert not p.aplicable
    assert p.impacto["evidencia"]["huerfanos"] == 1
    assert p.impacto["evidencia"]["muestra_huerfanos"] == ["99"]
    with pytest.raises(ErrorPlan):
        apl.aplicar(p.id)


def test_relacion_valida(cfg: Config, piezas, conn_admin) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    p = plan.relacion("pedidos", "cliente_id", "clientes", "cliente_id")
    assert p.aplicable, p.bloqueos
    assert p.impacto["evidencia"]["cobertura_pct"] == 100.0
    apl.aplicar(p.id)
    fks = conn_admin.execute(
        "SELECT conname FROM pg_constraint WHERE contype = 'f' AND conrelid = 'proyecto_test.pedidos'::regclass"
    ).fetchall()
    assert fks == [("fk_pedidos_cliente_id",)]
    assert not plan.relacion("pedidos", "cliente_id", "clientes", "cliente_id").aplicable


def test_relacion_exige_padre_unico(cfg: Config, piezas) -> None:
    plan, apl = piezas
    _fichero(cfg, "a.csv", "x\n1\n")
    _fichero(cfg, "b.csv", "x\n1\n")
    apl.aplicar(plan.tabla("a", desde_fichero="a.csv").id)
    apl.aplicar(plan.tabla("b", desde_fichero="b.csv").id)
    p = plan.relacion("a", "x", "b", "x")
    assert any("no es clave primaria" in b for b in p.bloqueos)
    apl.aplicar(plan.clave("b", ["x"]).id)


def test_vista_crear_y_redefinir(cfg: Config, piezas, conn_admin) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    consulta = "SELECT estado, sum(importe) AS total FROM pedidos GROUP BY estado"
    p = plan.vista("v_ventas", consulta)
    assert p.impacto["columnas"] == ["estado", "total"] and p.impacto["filas_resultado"] == 2
    apl.aplicar(p.id)
    # Misma forma: CREATE OR REPLACE, no destructivo.
    p2 = plan.vista("v_ventas", consulta.replace("FROM pedidos", "FROM pedidos WHERE estado <> 'cancelado'"), reemplazar=True)
    assert not p2.destructivo and "CREATE OR REPLACE VIEW" in p2.sql_completo()
    apl.aplicar(p2.id)
    assert conn_admin.execute("SELECT count(*) FROM proyecto_test.v_ventas").fetchone()[0] == 1
    # Columnas distintas: hay que borrar la vista -> destructivo.
    p3 = plan.vista("v_ventas", "SELECT count(*) AS n FROM pedidos", reemplazar=True)
    assert p3.destructivo and "DROP VIEW" in p3.sql_completo()


def test_vista_rechaza_sql_peligroso(piezas) -> None:
    plan, _ = piezas
    with pytest.raises(SQLRechazado):
        plan.vista("v", "SELECT * FROM public.secreto")
    with pytest.raises(SQLRechazado):
        plan.vista("v", "SELECT 1; DROP TABLE pedidos")


def test_vista_con_error_sql_se_bloquea(piezas) -> None:
    plan, _ = piezas
    p = plan.vista("v", "SELECT columna_inexistente FROM generate_series(1,2)")
    assert any("falla en PostgreSQL" in b for b in p.bloqueos)


def test_borrado_de_filas(cfg: Config, piezas, conn_admin) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    p = plan.borrado("filas", "pedidos", "estado = 'cancelado'")
    assert p.destructivo and p.impacto["filas_afectadas"] == 1
    with pytest.raises(RequiereConfirmacion):
        apl.aplicar(p.id)
    apl.aplicar(p.id, permitir_destructivo=True)
    assert conn_admin.execute("SELECT count(*) FROM proyecto_test.pedidos").fetchone()[0] == 2
    with pytest.raises(ErrorPlanificacion):
        plan.borrado("filas", "pedidos", "")
    with pytest.raises(SQLRechazado):
        plan.borrado("filas", "pedidos", "1=1; DROP TABLE clientes")


def test_borrado_con_dependientes_bloqueado(cfg: Config, piezas) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    apl.aplicar(plan.relacion("pedidos", "cliente_id", "clientes", "cliente_id").id)
    p = plan.borrado("tabla", "clientes")
    assert any("dependen" in b for b in p.bloqueos)
    assert any("claves foráneas" in b for b in plan.borrado("vaciar", "clientes").bloqueos)


def test_borrado_de_filas_con_hijas_bloqueado(cfg: Config, piezas, conn_admin) -> None:
    """M-11: sin CASCADE, borrar filas referenciadas fallaría; el plan lo dice con la cifra."""
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    apl.aplicar(plan.relacion("pedidos", "cliente_id", "clientes", "cliente_id").id)
    # Clientes de Madrid: 1 (con los pedidos 10 y 11) y 3 (sin pedidos).
    p = plan.borrado("filas", "clientes", "ciudad = 'Madrid'")
    assert p.impacto["filas_afectadas"] == 2
    assert p.impacto["filas_hijas_afectadas"] == {"fk_pedidos_cliente_id": 2}
    assert any("2 filas de pedidos" in b and "fk_pedidos_cliente_id" in b for b in p.bloqueos)
    # Un borrado que no deja hijas huérfanas sigue siendo posible (con confirmación).
    libre = plan.borrado("filas", "clientes", "cliente_id = 3")
    assert libre.aplicable, libre.bloqueos
    assert libre.impacto["filas_hijas_afectadas"] == {"fk_pedidos_cliente_id": 0}
    apl.aplicar(libre.id, permitir_destructivo=True)
    assert conn_admin.execute("SELECT count(*) FROM proyecto_test.clientes").fetchone()[0] == 2


def test_carga_con_valores_sin_padre_bloqueada(cfg: Config, piezas) -> None:
    """M-16: el plan de carga detecta filas cuyo padre no existe antes de aplicar."""
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    apl.aplicar(plan.relacion("pedidos", "cliente_id", "clientes", "cliente_id").id)
    _fichero(
        cfg,
        "pedidos_nuevos.csv",
        "pedido_id,cliente_id,importe,estado\n"
        "20,1,1.00,entregado\n21,9,2.00,entregado\n22,9,3.00,entregado\n23,8,4.00,entregado\n",
    )
    p = plan.carga("pedidos", "pedidos_nuevos.csv")
    assert p.impacto["filas_sin_padre"] == {"fk_pedidos_cliente_id": 3}
    assert any(
        "3 filas del fichero" in b and "clientes.cliente_id" in b and "8, 9" in b
        for b in p.bloqueos
    ), p.bloqueos
    _fichero(cfg, "pedidos_ok.csv", "pedido_id,cliente_id,importe,estado\n24,2,1.00,entregado\n")
    ok = plan.carga("pedidos", "pedidos_ok.csv")
    assert ok.aplicable, ok.bloqueos
    assert ok.impacto["filas_sin_padre"] == {}


def test_carga_transformada_repara_sin_tocar_el_fichero(cfg: Config, piezas, conn_admin) -> None:
    """M-15: entrega con duplicados, columna renombrada y céntimos, reparada en el plan."""
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    _fichero(cfg, "lineas.csv", "linea_id,pedido_id,precio_unitario\n1,10,28.25\n")
    apl.aplicar(plan.tabla("lineas", desde_fichero="lineas.csv", clave_primaria=["linea_id"]).id)
    apl.aplicar(plan.carga("lineas", "lineas.csv").id)
    roto = "linea_id,pedido_id,precio\n2,10,1050\n2,10,1050\n3,12,499\n"
    _fichero(cfg, "lineas_rotas.csv", roto)

    # Sin transformación: el plan se bloquea (columna renombrada y clave repetida).
    bloqueado = plan.carga("lineas", "lineas_rotas.csv")
    assert any("precio" in b for b in bloqueado.bloqueos)
    assert any("se repite" in b for b in bloqueado.bloqueos)

    t = Transformacion.crear({"precio": "precio_unitario"}, {"precio": 100}, deduplicar=True)
    p = plan.carga("lineas", "lineas_rotas.csv", transformacion=t)
    assert p.aplicable, p.bloqueos
    assert p.impacto["filas_a_insertar"] == 2
    assert p.impacto["transformacion"]["duplicadas_exactas"] == 1
    apl.aplicar(p.id)
    assert conn_admin.execute(
        "SELECT count(*), sum(precio_unitario) FROM proyecto_test.lineas"
    ).fetchone() == (3, Decimal("43.74"))
    # El fichero entregado sigue intacto.
    assert (cfg.dir_datos / "lineas_rotas.csv").read_text(encoding="utf-8") == roto

    # La validación compara con el origen tal y como se cargó (transformado).
    origenes = [
        abrir(cfg.dir_datos, "lineas.csv"),
        OrigenTransformado(abrir(cfg.dir_datos, "lineas_rotas.csv"), t),
    ]
    with psycopg.connect(cfg.dsn("lector")) as conn:
        comprobaciones = validar_carga(
            conn, cfg.esquema, "lineas", origenes, ["linea_id"], ["precio_unitario"]
        )
    assert all(c.ok for c in comprobaciones), [c.como_dict() for c in comprobaciones]


def test_documentacion_guarda_comentarios_sin_inyeccion(cfg: Config, piezas, conn_admin) -> None:
    """C: las descripciones se guardan como COMMENT ON construido por el servidor; un
    texto con apariencia de SQL se guarda literal y no se ejecuta."""
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    malicioso = "Ciudad'; DROP TABLE proyecto_test.pedidos; --"
    p = plan.documentacion({
        "clientes": {"descripcion": "Un cliente por fila.", "columnas": {"ciudad": malicioso}},
    })
    assert p.aplicable and not p.destructivo
    assert p.impacto["descripciones"] == 2
    apl.aplicar(p.id)
    assert conn_admin.execute(
        "SELECT obj_description('proyecto_test.clientes'::regclass, 'pg_class')"
    ).fetchone()[0] == "Un cliente por fila."
    assert conn_admin.execute(
        "SELECT col_description('proyecto_test.clientes'::regclass, 3)"
    ).fetchone()[0] == malicioso
    assert conn_admin.execute("SELECT to_regclass('proyecto_test.pedidos')").fetchone()[0]

    # Repetir lo mismo no genera plan; cambiarlo avisa de la sustitución.
    with pytest.raises(ErrorPlanificacion, match="ya estaban"):
        plan.documentacion({"clientes": {"descripcion": "Un cliente por fila."}})
    cambio = plan.documentacion({"clientes": {"descripcion": "Clientes registrados."}})
    assert cambio.impacto["reemplaza"][0]["antes"] == "Un cliente por fila."
    assert cambio.advertencias
    with pytest.raises(ErrorPlanificacion, match="no tiene la columna"):
        plan.documentacion({"clientes": {"columnas": {"no_existe": "x"}}})
    with pytest.raises(ErrorPlanificacion, match="No existe"):
        plan.documentacion({"inexistente": {"descripcion": "x"}})

    # El modelo y el diccionario reflejan las descripciones y lo que falta.
    with psycopg.connect(cfg.dsn("lector")) as conn:
        modelo = describir_modelo(conn, cfg.esquema)
    clientes = next(o for o in modelo["objetos"] if o["nombre"] == "clientes")
    assert clientes["descripcion"] == "Un cliente por fila."
    assert "pedidos" in modelo["pendiente_de_documentar"]
    md = diccionario_markdown(modelo)
    assert "erDiagram" in md
    assert "Un cliente por fila." in md


def test_fallo_en_aplicacion_deshace_todo(cfg: Config, piezas, conn_admin) -> None:
    plan, apl = piezas
    _cargar_base(cfg, plan, apl)
    # El SQL es válido para sqlglot, pero PostgreSQL lo rechaza al ejecutar.
    p = plan.vista("v_x", "SELECT 1 AS uno")
    conn_admin.execute("CREATE TABLE proyecto_test.v_x (i int)")  # aparece entre medias
    with pytest.raises(psycopg.errors.DuplicateTable):
        apl.aplicar(p.id)
    with pytest.raises(ErrorPlan, match="fallido"):
        apl.aplicar(p.id)


def test_ruta_de_datos_protegida(cfg: Config, piezas, tmp_path: Path) -> None:
    plan, _ = piezas
    (tmp_path / "fuera.csv").write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fuera del directorio"):
        plan.tabla("x", desde_fichero="../fuera.csv")
