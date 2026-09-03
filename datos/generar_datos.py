"""Generador de datos sintéticos de un comercio electrónico (semilla fija).

Produce dos conjuntos:

* ``datos/demo/``: clientes.csv, productos.json, pedidos.csv y lineas_pedido.csv.
  Datos limpios. Detalle intencionado para la demo: la línea de pedido referencia al
  producto con la columna ``ref_producto``, mientras que el producto se identifica por
  ``sku``. Los valores coinciden al 100 %, pero los nombres no: el agente debe aportar
  la evidencia y PREGUNTAR antes de crear esa relación, en lugar de inventarla.
* ``datos/estropeado/``: una segunda entrega (octubre de 2026) con defectos inyectados
  para la skill de diagnóstico y reparación: filas duplicadas, una columna renombrada
  (``precio_unitario`` → ``precio``) e importes en céntimos.

Reproducibilidad: los NÚMEROS (ids, fechas, cantidades, precios, estados) salen de
``random.Random(semilla)`` de la biblioteca estándar, estable entre versiones de
Python; Faker solo genera textos (nombres, emails, ciudades), que pueden variar entre
versiones de Faker sin afectar a ningún total. Cada conjunto incluye ``manifiesto.json``
con recuentos y sumas de control, que es la "verdad" contra la que se evalúa.

Uso:  python datos/generar_datos.py [--semilla 42] [--salida datos]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from faker import Faker

N_CLIENTES = 500
N_PRODUCTOS = 80
N_PEDIDOS = 3000
CATEGORIAS = ["Electrónica", "Hogar", "Deporte", "Libros", "Moda", "Alimentación"]
ESTADOS = ["entregado", "enviado", "cancelado", "devuelto"]
PESOS_ESTADO = [70, 10, 12, 8]
CANALES = ["web", "app", "tienda"]
GAMAS = ["Básico", "Plus", "Pro", "Eco", "Mini", "Max"]
CENTIMO = Decimal("0.01")


@dataclass
class Conjunto:
    clientes: list[dict[str, Any]]
    productos: list[dict[str, Any]]
    pedidos: list[dict[str, Any]]
    lineas: list[dict[str, Any]]


def _fecha(rnd: random.Random, desde: date, hasta: date) -> date:
    return desde + timedelta(days=rnd.randrange((hasta - desde).days + 1))


def _importe(linea: dict[str, Any]) -> Decimal:
    bruto = Decimal(linea["cantidad"]) * Decimal(linea["precio_unitario"])
    neto = bruto * (Decimal(100) - Decimal(linea["descuento_pct"])) / Decimal(100)
    return neto.quantize(CENTIMO, rounding=ROUND_HALF_UP)


def generar_base(semilla: int) -> Conjunto:
    rnd = random.Random(semilla)
    fake = Faker("es_ES")
    fake.seed_instance(semilla)

    clientes = []
    for i in range(1, N_CLIENTES + 1):
        nombre = fake.name()
        clientes.append(
            {
                "cliente_id": i,
                "nombre": nombre,
                "email": f"cliente{i:04d}@{fake.free_email_domain()}",
                "ciudad": fake.city(),
                "segmento": rnd.choices(["particular", "empresa"], weights=[85, 15])[0],
                "fecha_alta": _fecha(rnd, date(2024, 1, 1), date(2026, 6, 30)).isoformat(),
            }
        )

    productos = []
    for i in range(1, N_PRODUCTOS + 1):
        categoria = CATEGORIAS[(i - 1) % len(CATEGORIAS)]
        precio = (Decimal(rnd.randint(399, 49_999)) / 100).quantize(CENTIMO)
        productos.append(
            {
                "sku": f"SKU-{i:04d}",
                "nombre": f"{categoria} {rnd.choice(GAMAS)} {i:03d}",
                "categoria": categoria,
                "precio_unitario": str(precio),
                "activo": rnd.random() > 0.05,
            }
        )

    pedidos, lineas = [], []
    for pedido_id in range(1, N_PEDIDOS + 1):
        pedidos.append(
            {
                "pedido_id": pedido_id,
                "cliente_id": rnd.randint(1, N_CLIENTES),
                "fecha_pedido": _fecha(rnd, date(2025, 1, 1), date(2026, 9, 30)).isoformat(),
                "estado": rnd.choices(ESTADOS, weights=PESOS_ESTADO)[0],
                "canal": rnd.choice(CANALES),
            }
        )
        lineas += _lineas_de(rnd, pedido_id, productos, inicio_id=len(lineas) + 1)
    return Conjunto(clientes, productos, pedidos, lineas)


def _lineas_de(
    rnd: random.Random, pedido_id: int, productos: list[dict[str, Any]], inicio_id: int
) -> list[dict[str, Any]]:
    resultado = []
    for k, producto in enumerate(rnd.sample(productos, rnd.randint(1, 4))):
        resultado.append(
            {
                "linea_id": inicio_id + k,
                "pedido_id": pedido_id,
                "ref_producto": producto["sku"],
                "cantidad": rnd.randint(1, 5),
                "precio_unitario": producto["precio_unitario"],
                "descuento_pct": rnd.choices([0, 5, 10, 20], weights=[70, 15, 10, 5])[0],
            }
        )
    return resultado


Filas = list[dict[str, Any]]


def generar_estropeado(base: Conjunto, semilla: int) -> tuple[Filas, Filas, dict[str, Any]]:
    """Entrega de octubre de 2026 con defectos conocidos (la verdad queda en el manifiesto)."""
    rnd = random.Random(semilla + 1)
    primer_pedido = len(base.pedidos) + 1
    primera_linea = len(base.lineas) + 1
    pedidos, lineas = [], []
    for pedido_id in range(primer_pedido, primer_pedido + 400):
        pedidos.append(
            {
                "pedido_id": pedido_id,
                "cliente_id": rnd.randint(1, N_CLIENTES),
                "fecha_pedido": _fecha(rnd, date(2026, 10, 1), date(2026, 10, 15)).isoformat(),
                "estado": rnd.choices(ESTADOS, weights=PESOS_ESTADO)[0],
                "canal": rnd.choice(CANALES),
            }
        )
        lineas += _lineas_de(rnd, pedido_id, base.productos, primera_linea + len(lineas))

    importe_correcto = sum((_importe(linea) for linea in lineas), Decimal(0))
    # Defecto 1: duplicados exactos (re-envío parcial del proveedor).
    pedidos_dup = rnd.sample(pedidos, 12)
    lineas_dup = rnd.sample(lineas, 45)
    # Defecto 2 y 3: columna renombrada e importes en céntimos.
    lineas_rotas = [
        {
            "linea_id": linea["linea_id"],
            "pedido_id": linea["pedido_id"],
            "ref_producto": linea["ref_producto"],
            "cantidad": linea["cantidad"],
            "precio": int((Decimal(linea["precio_unitario"]) * 100).to_integral_value()),
            "descuento_pct": linea["descuento_pct"],
        }
        for linea in lineas + lineas_dup
    ]
    lineas_rotas.sort(key=lambda fila: fila["linea_id"])
    pedidos_rotos = sorted(pedidos + pedidos_dup, key=lambda fila: fila["pedido_id"])
    verdad = {
        "descripcion": "Entrega de octubre de 2026 con defectos inyectados",
        "defectos": [
            {"tipo": "duplicados", "fichero": "pedidos_2026_10.csv", "filas_duplicadas": 12},
            {"tipo": "duplicados", "fichero": "lineas_pedido_2026_10.csv", "filas_duplicadas": 45},
            {"tipo": "columna_renombrada", "fichero": "lineas_pedido_2026_10.csv",
             "esperada": "precio_unitario", "recibida": "precio"},
            {"tipo": "unidad_cambiada", "fichero": "lineas_pedido_2026_10.csv",
             "columna": "precio", "detalle": "céntimos en lugar de euros (x100)"},
        ],
        "correcto": {
            "pedidos_unicos": len(pedidos),
            "lineas_unicas": len(lineas),
            "importe_lineas": str(importe_correcto),
        },
        "recibido": {"filas_pedidos": len(pedidos_rotos), "filas_lineas": len(lineas_rotas)},
    }
    return pedidos_rotos, lineas_rotas, verdad


def manifiesto_base(c: Conjunto, semilla: int) -> dict[str, Any]:
    """Recuentos y sumas de control: la "verdad" para validar y evaluar."""
    estado_de = {p["pedido_id"]: p["estado"] for p in c.pedidos}
    categoria_de = {p["sku"]: p["categoria"] for p in c.productos}
    importe_total = Decimal(0)
    sin_cancelados = Decimal(0)
    por_estado: dict[str, Decimal] = {e: Decimal(0) for e in ESTADOS}
    por_categoria: dict[str, Decimal] = {k: Decimal(0) for k in CATEGORIAS}
    for linea in c.lineas:
        importe = _importe(linea)
        estado = estado_de[linea["pedido_id"]]
        importe_total += importe
        por_estado[estado] += importe
        if estado != "cancelado":
            sin_cancelados += importe
            por_categoria[categoria_de[linea["ref_producto"]]] += importe
    clientes_con_pedido = len({p["cliente_id"] for p in c.pedidos})
    fecha_de = {p["pedido_id"]: p["fecha_pedido"] for p in c.pedidos}
    meses_sin_cancelados = {
        fecha_de[linea["pedido_id"]][:7] for linea in c.lineas
        if estado_de[linea["pedido_id"]] != "cancelado"
    }
    return {
        "semilla": semilla,
        "nota": "importe de línea = cantidad * precio_unitario * (1 - descuento_pct/100), "
                "redondeado a céntimos",
        "filas": {
            "clientes": len(c.clientes),
            "productos": len(c.productos),
            "pedidos": len(c.pedidos),
            "lineas_pedido": len(c.lineas),
        },
        "sumas": {
            "cantidad_lineas": sum(linea["cantidad"] for linea in c.lineas),
            "importe_total": str(importe_total),
            "importe_sin_cancelados": str(sin_cancelados),
        },
        "pedidos_por_estado": dict(sorted(Counter(p["estado"] for p in c.pedidos).items())),
        "importe_por_estado": {k: str(v) for k, v in por_estado.items()},
        "importe_por_categoria_sin_cancelados": {k: str(v) for k, v in por_categoria.items()},
        "clientes_con_pedido": clientes_con_pedido,
        "meses_con_ventas_sin_cancelados": len(meses_sin_cancelados),
        "categorias": len(CATEGORIAS),
    }


def _csv(ruta: Path, filas: list[dict[str, Any]]) -> None:
    with ruta.open("w", encoding="utf-8", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=list(filas[0]))
        escritor.writeheader()
        escritor.writerows(filas)


def _json(ruta: Path, datos: Any) -> None:
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--semilla", type=int, default=42)
    parser.add_argument("--salida", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)

    base = generar_base(args.semilla)
    demo = args.salida / "demo"
    demo.mkdir(parents=True, exist_ok=True)
    _csv(demo / "clientes.csv", base.clientes)
    _json(demo / "productos.json", base.productos)
    _csv(demo / "pedidos.csv", base.pedidos)
    _csv(demo / "lineas_pedido.csv", base.lineas)
    _json(demo / "manifiesto.json", manifiesto_base(base, args.semilla))

    pedidos_rotos, lineas_rotas, verdad = generar_estropeado(base, args.semilla)
    roto = args.salida / "estropeado"
    roto.mkdir(parents=True, exist_ok=True)
    _csv(roto / "pedidos_2026_10.csv", pedidos_rotos)
    _csv(roto / "lineas_pedido_2026_10.csv", lineas_rotas)
    _json(roto / "manifiesto.json", verdad)
    print(f"Datos generados en {demo} y {roto} (semilla {args.semilla}).")


if __name__ == "__main__":
    main()
