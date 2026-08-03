"""
Test de validación de la capa de datos, SIN modelos de lenguaje de por
medio. Este es el primer test que debería pasar antes de tocar cualquier
código que involucre LLMs (ver docs/HISTORY.md, entrada del boilerplate
inicial, "próximos pasos" #4).
"""

import duckdb
import pytest


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    with open("data/schema.sql") as f:
        connection.execute(f.read())
    with open("data/seed_data.sql") as f:
        connection.execute(f.read())
    yield connection
    connection.close()


def test_tablas_existen(con):
    tablas = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    assert tablas == {"aerolineas", "vuelos", "pasajeros", "reservas"}


def test_hay_datos_cargados(con):
    for tabla in ["aerolineas", "vuelos", "pasajeros", "reservas"]:
        count = con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
        assert count > 0, f"La tabla {tabla} está vacía"


def test_query_de_ejemplo_ejecuta_sin_error(con):
    """Corresponde al case_001 del golden dataset."""
    result = con.execute(
        """
        SELECT v.destino, COUNT(r.id) AS total_reservas, SUM(r.precio_pagado) AS ingresos
        FROM reservas r
        JOIN vuelos v ON r.vuelo_id = v.id
        WHERE r.fecha_reserva >= DATE '2026-08-02' - INTERVAL 90 DAY
        GROUP BY v.destino
        ORDER BY total_reservas DESC
        LIMIT 5
        """
    ).fetchall()
    assert len(result) <= 5
    assert len(result) > 0
