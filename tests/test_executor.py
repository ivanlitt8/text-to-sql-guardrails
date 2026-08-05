"""
Tests del executor DuckDB (sin LLM).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from executor import ExecutionResult, execute_query, list_schema_tables  # noqa: E402


def test_execute_query_select_devuelve_filas():
    result = execute_query(
        "SELECT COUNT(*) AS n FROM vuelos",
        db_path=str(ROOT / "data" / "vuelos.duckdb"),
    )
    assert isinstance(result, ExecutionResult)
    assert result.error is None
    assert len(result.rows) == 1
    assert result.rows[0]["n"] > 0


def test_execute_query_sql_invalido_propaga_error():
    result = execute_query(
        "SELECT * FROM tabla_inexistente_xyz",
        db_path=str(ROOT / "data" / "vuelos.duckdb"),
    )
    assert result.rows == []
    assert result.error is not None
    assert "tabla_inexistente_xyz" in result.error.lower() or "Catalog" in result.error


def test_execute_query_columna_inexistente_propaga_error():
    result = execute_query(
        "SELECT v.pais FROM vuelos v LIMIT 1",
        db_path=str(ROOT / "data" / "vuelos.duckdb"),
    )
    assert result.rows == []
    assert result.error is not None
    assert "pais" in result.error.lower() or "Binder" in result.error or "column" in result.error.lower()


def test_list_schema_tables():
    tables = list_schema_tables(str(ROOT / "data" / "vuelos.duckdb"))
    assert set(tables) == {"aerolineas", "vuelos", "pasajeros", "reservas"}
