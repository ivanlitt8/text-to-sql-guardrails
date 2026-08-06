"""
Tests de schema_extractor: introspección DuckDB → DDL CREATE TABLE.
Sin modelos de lenguaje de por medio.
"""

import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from schema_extractor import extract_schema  # noqa: E402


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    with open(ROOT / "data" / "schema.sql") as f:
        connection.execute(f.read())
    with open(ROOT / "data" / "seed_data.sql") as f:
        connection.execute(f.read())
    yield connection
    connection.close()


def test_extract_schema_contiene_tablas_y_columnas(con):
    ddl = extract_schema(con)

    for tabla in ("aerolineas", "ciudades", "vuelos", "pasajeros", "reservas"):
        assert f"CREATE TABLE {tabla}" in ddl

    assert "destino" in ddl
    assert "aerolinea_id" in ddl
    assert "pasajero_id" in ddl
    assert "precio_pagado" in ddl


def test_extract_schema_incluye_foreign_keys(con):
    ddl = extract_schema(con)

    assert "REFERENCES aerolineas(id)" in ddl
    assert "REFERENCES pasajeros(id)" in ddl
    assert "REFERENCES vuelos(id)" in ddl


def test_extract_schema_no_lee_archivo_estatico(con):
    """El DDL debe salir de la introspección, no de data/schema.sql."""
    con.execute("ALTER TABLE vuelos ADD COLUMN notas VARCHAR")
    ddl = extract_schema(con)
    assert "notas" in ddl
    assert "CREATE TABLE vuelos" in ddl
