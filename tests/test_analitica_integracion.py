"""Las plantillas SQL de la skill ``analitica-avanzada`` contra PostgreSQL real.

El SQL se extrae del propio SKILL.md, de modo que si alguien edita la skill y rompe
una plantilla (o deja de pasar el validador), este test falla. Los resultados se
comparan con un cálculo independiente en Python sobre los mismos ficheros.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import psycopg
import pytest

from dataops_agent.aplicador import Aplicador
from dataops_agent.conexion import GestorConexiones
from dataops_agent.config import Config
from dataops_agent.planes import AlmacenPlanes
from dataops_agent.planificador import Planificador
from dataops_agent.seguridad import ValidadorSQL

SKILL = Path(__file__).resolve().parent.parent / ".claude/skills/analitica-avanzada/SKILL.md"
VISTAS = ["v_rfm_clientes", "v_abc_productos", "v_ventas_tendencia"]

FICHEROS = {
    "clientes.csv": """cliente_id,nombre
1,Ana
2,Luis
3,Eva
4,Raúl
5,Marta
6,Sin compras válidas
7,Iker
8,Nuria
""",
    "productos.csv": """sku,nombre,categoria
P1,Taza,Hogar
P2,Libro,Libros
P3,Calcetines,Moda
P4,Nunca vendido,Hogar
""",
    # 2025-03 solo tiene un pedido cancelado: la serie mensual tiene un hueco.
    # Los clientes 7 y 8 tienen cinco pedidos (umbral de frecuencia de la segmentación).
    "pedidos.csv": """pedido_id,cliente_id,fecha_pedido,estado
100,1,2025-01-10,entregado
101,1,2025-02-05,entregado
102,1,2026-02-15,entregado
103,2,2025-01-20,entregado
104,2,2025-03-03,cancelado
105,3,2025-02-11,devuelto
106,3,2025-04-02,entregado
107,4,2026-01-30,entregado
108,5,2026-02-01,enviado
109,6,2025-05-05,cancelado
110,7,2026-02-01,entregado
111,7,2026-02-02,entregado
112,7,2026-02-03,entregado
113,7,2026-02-04,entregado
114,7,2026-02-05,entregado
115,8,2025-04-10,entregado
116,8,2025-04-11,entregado
117,8,2025-04-12,entregado
118,8,2025-04-13,entregado
119,8,2025-04-14,entregado
""",
    "lineas_pedido.csv": """linea_id,pedido_id,ref_producto,cantidad,precio_unitario,descuento_pct
1,100,P1,2,10.00,0
2,100,P2,1,33.33,10
3,101,P1,1,10.00,0
4,102,P3,3,5.55,5
5,103,P2,2,33.33,0
6,104,P1,5,10.00,0
7,105,P1,1,10.00,20
8,106,P3,1,5.55,0
9,107,P2,1,33.33,0
10,108,P1,4,10.00,10
11,109,P3,2,5.55,0
12,110,P3,1,5.55,0
13,111,P3,1,5.55,0
14,112,P3,1,5.55,0
15,113,P3,1,5.55,0
16,114,P3,1,5.55,0
17,115,P3,1,5.55,0
18,116,P3,1,5.55,0
19,117,P3,1,5.55,0
20,118,P3,1,5.55,0
21,119,P3,1,5.55,0
""",
}

VISTA_MENSUAL = """
SELECT date_trunc('month', p.fecha_pedido)::date AS mes,
       count(DISTINCT p.pedido_id) AS pedidos,
       round(sum(round(l.cantidad * l.precio_unitario * (1 - l.descuento_pct / 100.0), 2)), 2)
           AS importe
FROM pedidos p
JOIN lineas_pedido l ON l.pedido_id = p.pedido_id
WHERE p.estado <> 'cancelado'
GROUP BY 1
"""


def plantillas() -> dict[str, str]:
    bloques = re.findall(r"```sql\n(.*?)```", SKILL.read_text(encoding="utf-8"), re.S)
    assert len(bloques) == len(VISTAS), "la skill debe tener una plantilla SQL por vista"
    return dict(zip(VISTAS, bloques, strict=True))


def _filas(nombre: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(FICHEROS[nombre])))


def _esperado() -> dict[str, object]:
    """Cálculo independiente (Python + Decimal) con la misma regla de compra."""
    pedidos = {int(p["pedido_id"]): p for p in _filas("pedidos.csv")
               if p["estado"] != "cancelado"}
    importe_cliente: dict[int, Decimal] = defaultdict(Decimal)
    importe_producto: dict[str, Decimal] = defaultdict(Decimal)
    importe_mes: dict[date, Decimal] = defaultdict(Decimal)
    for linea in _filas("lineas_pedido.csv"):
        pedido = pedidos.get(int(linea["pedido_id"]))
        if pedido is None:
            continue
        importe = (Decimal(linea["cantidad"]) * Decimal(linea["precio_unitario"])
                   * (100 - Decimal(linea["descuento_pct"])) / 100
                   ).quantize(Decimal("0.01"), ROUND_HALF_UP)
        importe_cliente[int(pedido["cliente_id"])] += importe
        importe_producto[linea["ref_producto"]] += importe
        importe_mes[date.fromisoformat(pedido["fecha_pedido"]).replace(day=1)] += importe
    return {"cliente": importe_cliente, "producto": importe_producto, "mes": importe_mes,
            "total": sum(importe_cliente.values())}


@pytest.fixture
def modelo(cfg: Config) -> tuple[Planificador, Aplicador]:
    gestor = GestorConexiones(cfg)
    validador = ValidadorSQL(cfg.esquema)
    almacen = AlmacenPlanes(timedelta(minutes=30))
    plan = Planificador(cfg, gestor, validador, almacen)
    apl = Aplicador(cfg, gestor, validador, almacen)
    claves = {"clientes": "cliente_id", "productos": "sku", "pedidos": "pedido_id",
              "lineas_pedido": "linea_id"}
    for tabla, clave in claves.items():
        (cfg.dir_datos / f"{tabla}.csv").write_text(FICHEROS[f"{tabla}.csv"], encoding="utf-8")
        apl.aplicar(plan.tabla(tabla, desde_fichero=f"{tabla}.csv", clave_primaria=[clave]).id)
        apl.aplicar(plan.carga(tabla, f"{tabla}.csv").id)
    apl.aplicar(plan.vista("v_ventas_mensuales", VISTA_MENSUAL).id)
    for nombre, sql in plantillas().items():
        p = plan.vista(nombre, sql)
        assert p.aplicable, (nombre, p.bloqueos)
        apl.aplicar(p.id)
    return plan, apl


def _consulta(conn: psycopg.Connection, sql: str) -> list[tuple]:
    return conn.execute(sql.replace("FROM v_", "FROM proyecto_test.v_")).fetchall()


def test_rfm(modelo, conn_admin: psycopg.Connection) -> None:
    esperado = _esperado()
    filas = _consulta(conn_admin, "SELECT cliente_id, importe, recencia_dias, frecuencia, "
                                  "segmento_rfm FROM v_rfm_clientes ORDER BY cliente_id")
    # Solo clientes con alguna compra no cancelada (el 6 solo tiene una cancelada).
    assert [f[0] for f in filas] == [1, 2, 3, 4, 5, 7, 8]
    assert {f[0]: f[1] for f in filas} == esperado["cliente"]
    # Recencia hasta la última fecha de los datos (2026-02-15), no hasta hoy:
    assert {f[0]: f[2] for f in filas} == {1: 0, 2: 391, 3: 319, 4: 16, 5: 14, 7: 10, 8: 307}
    assert {f[0]: f[3] for f in filas} == {1: 3, 2: 1, 3: 2, 4: 1, 5: 1, 7: 5, 8: 5}
    # Umbrales de la skill: 90 días y 5 pedidos; los cuatro segmentos aparecen.
    assert {f[0]: f[4] for f in filas} == {
        1: "Ocasionales", 2: "Inactivos", 3: "Inactivos", 4: "Ocasionales",
        5: "Ocasionales", 7: "Fieles", 8: "En riesgo",
    }


def test_abc(modelo, conn_admin: psycopg.Connection) -> None:
    esperado = _esperado()
    filas = _consulta(conn_admin, "SELECT sku, importe, pct_acumulado, clase_abc "
                                  "FROM v_abc_productos ORDER BY posicion")
    assert len(filas) == 4  # todos los productos del catálogo
    assert sum(f[1] for f in filas) == esperado["total"]
    assert filas[-1][2] == Decimal("100.00")
    assert filas[-1][0] == "P4" and filas[-1][1] == 0 and filas[-1][3] == "C"
    assert filas[0][3] == "A"  # el primero siempre es A (su acumulado previo es 0)


def test_tendencia_interanual(modelo, conn_admin: psycopg.Connection) -> None:
    esperado = _esperado()
    filas = _consulta(conn_admin, "SELECT mes, importe, importe_hace_un_anio, "
                                  "variacion_interanual_pct FROM v_ventas_tendencia ORDER BY mes")
    assert [f[0] for f in filas] == sorted(esperado["mes"])  # 2025-03 no tiene ventas
    por_mes = {f[0]: f for f in filas}
    ene_25, ene_26, feb_26 = date(2025, 1, 1), date(2026, 1, 1), date(2026, 2, 1)
    assert por_mes[ene_25][2] is None  # sin año anterior
    assert por_mes[ene_26][2] == esperado["mes"][ene_25]
    variacion = (100 * (esperado["mes"][ene_26] / esperado["mes"][ene_25] - 1)
                 ).quantize(Decimal("0.01"), ROUND_HALF_UP)
    assert por_mes[ene_26][3] == variacion
    assert por_mes[feb_26][2] == esperado["mes"][date(2025, 2, 1)]
