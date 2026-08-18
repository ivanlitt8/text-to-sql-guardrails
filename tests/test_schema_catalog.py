"""
Tests del catálogo HTTP (introspección DuckDB + metadata semántica).
Sin modelos de lenguaje de por medio.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from schema_catalog import (  # noqa: E402
    SchemaUnavailableError,
    build_schema_response,
    load_schema_catalog,
)


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    with open(ROOT / "data" / "schema.sql") as f:
        connection.execute(f.read())
    with open(ROOT / "data" / "seed_data.sql") as f:
        connection.execute(f.read())
    yield connection
    connection.close()


def test_build_schema_response_tablas_y_fks(con):
    payload = build_schema_response(con)
    tables = {table.name: table for table in payload.tables}

    assert set(tables) == {
        "aerolineas",
        "ciudades",
        "vuelos",
        "pasajeros",
        "reservas",
    }
    assert tables["vuelos"].description is not None
    assert "origen" in (tables["vuelos"].description or "")

    aerolinea_id = next(
        column for column in tables["vuelos"].columns if column.name == "aerolinea_id"
    )
    assert aerolinea_id.foreign_key is not None
    assert aerolinea_id.foreign_key.table == "aerolineas"
    assert aerolinea_id.foreign_key.column == "id"

    destino = next(
        column for column in tables["vuelos"].columns if column.name == "destino"
    )
    assert destino.foreign_key is None
    assert destino.description is not None


def test_build_schema_response_columna_sin_catalogo(con):
    """Una columna nueva sale de DuckDB aunque el catálogo no la conozca."""
    con.execute("ALTER TABLE vuelos ADD COLUMN notas VARCHAR")
    payload = build_schema_response(con)
    vuelos = next(table for table in payload.tables if table.name == "vuelos")
    notas = next(column for column in vuelos.columns if column.name == "notas")
    assert notas.description is None
    assert notas.is_primary_key is False


def test_catalogo_hints_sugerencias_y_limitaciones(con):
    payload = build_schema_response(con)
    assert payload.hints
    assert any("ciudades" in hint.body for hint in payload.hints)
    assert payload.prompt_suggestions
    assert all("…" in s or s.endswith("?") for s in payload.prompt_suggestions)
    assert payload.limitations
    assert any("clima" in item.lower() for item in payload.limitations)


def test_sugerencias_no_repiten_golden(con):
    golden = json.loads((ROOT / "eval" / "golden_dataset.json").read_text(encoding="utf-8"))
    golden_questions = {case["question"] for case in golden}
    payload = build_schema_response(con)
    overlap = golden_questions.intersection(payload.prompt_suggestions)
    assert overlap == set()


@patch("schema_catalog.open_connection_for_schema")
def test_load_schema_catalog_503_si_duckdb_falla(mock_open):
    mock_open.side_effect = RuntimeError("duckdb down")
    with pytest.raises(SchemaUnavailableError) as exc_info:
        load_schema_catalog()
    assert "RuntimeError" in str(exc_info.value)
