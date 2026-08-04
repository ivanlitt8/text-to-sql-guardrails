"""
Tests del executor DuckDB (sin LLM).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from executor import execute_query, list_schema_tables  # noqa: E402


def test_execute_query_select_devuelve_filas():
    rows = execute_query(
        "SELECT COUNT(*) AS n FROM vuelos",
        db_path=str(ROOT / "data" / "vuelos.duckdb"),
    )
    assert isinstance(rows, list)
    assert len(rows) == 1
    assert rows[0]["n"] > 0


def test_execute_query_sql_invalido_devuelve_lista_vacia():
    rows = execute_query(
        "SELECT * FROM tabla_inexistente_xyz",
        db_path=str(ROOT / "data" / "vuelos.duckdb"),
    )
    assert rows == []


def test_list_schema_tables():
    tables = list_schema_tables(str(ROOT / "data" / "vuelos.duckdb"))
    assert set(tables) == {"aerolineas", "vuelos", "pasajeros", "reservas"}
