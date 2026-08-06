"""
Tests de sql_generator: prompt Defog, validación sqlparse y generate_sql.
Los unitarios mockean la llamada a Ollama (no requieren el daemon).
El test de integración sí habla con Ollama real y se salta si no está disponible.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from schema_extractor import extract_schema  # noqa: E402
from sql_generator import (  # noqa: E402
    SQLGenerationError,
    SQLGenerationResult,
    _build_explanation,
    _extract_columns_used,
    _extract_tables_used,
    _normalize_sql,
    _validate_sql_syntax,
    build_defog_prompt,
    generate_sql,
)


SAMPLE_SCHEMA = """
CREATE TABLE vuelos (
    id INTEGER PRIMARY KEY,
    origen VARCHAR,
    destino VARCHAR,
    precio DECIMAL
);
""".strip()


# --- Prompt Defog -----------------------------------------------------------


def test_build_defog_prompt_tiene_tres_secciones_y_fence_abierto():
    prompt = build_defog_prompt("¿Cuántos vuelos hay?", SAMPLE_SCHEMA)

    assert "### Task" in prompt
    assert "### Database Schema" in prompt
    assert "### Answer" in prompt
    assert "### Examples" in prompt
    assert "¿Cuántos vuelos hay?" in prompt
    assert "CREATE TABLE vuelos" in prompt
    assert prompt.rstrip().endswith("```sql")
    assert "Do not invent tables" in prompt
    # Few-shots estables: AVG, origen/destino, canceladas, conteo simple
    assert "AVG(v.precio)" in prompt
    assert "GROUP BY origen, destino" in prompt
    assert "estado = 'cancelada'" in prompt
    assert "SELECT COUNT(*) FROM vuelos WHERE destino = 'Roma'" in prompt
    # Few-shot case_001: COUNT+SUM + filtro 90 días, orden por reservas
    assert "COUNT(r.id) AS total_reservas" in prompt
    assert "INTERVAL '90 days'" in prompt
    assert "ORDER BY total_reservas DESC LIMIT 5" in prompt
    # Few-shot case_005: confirmadas + COUNT + GROUP BY v.id (grano vuelo)
    assert "estado = 'confirmada'" in prompt
    assert "COUNT(r.id) AS total_confirmadas" in prompt
    assert "GROUP BY v.id, v.origen, v.destino, a.nombre" in prompt
    assert "ORDER BY total_confirmadas DESC LIMIT 1" in prompt
    # Few-shot case_008: país de destino vía JOIN ciudades
    assert "JOIN ciudades c ON v.destino = c.ciudad" in prompt
    assert "c.pais != p.pais_residencia" in prompt
    # JOIN policy suavizada (no la prosa agresiva anterior)
    assert "JOIN policy:" in prompt
    assert "Never omit explicit filters" in prompt
    assert "AGGREGATION MAPPING" not in prompt
    assert "STRICT FILTERS" not in prompt


def test_build_defog_prompt_no_cierra_el_fence_sql():
    prompt = build_defog_prompt("pregunta", SAMPLE_SCHEMA)
    assert prompt.count("```") == 1


# --- Validación sqlparse ----------------------------------------------------


def test_validate_sql_acepta_select_valido():
    _validate_sql_syntax("SELECT destino FROM vuelos LIMIT 10")


def test_validate_sql_rechaza_vacio():
    with pytest.raises(SQLGenerationError, match="vacío"):
        _validate_sql_syntax("   ")


def test_validate_sql_rechaza_texto_no_sql():
    with pytest.raises(SQLGenerationError, match="UNKNOWN|no parseable|no reconocida"):
        _validate_sql_syntax("esto no es sql en absoluto")


def test_normalize_sql_quita_fence_markdown():
    raw = "```sql\nSELECT 1\n```"
    assert _normalize_sql(raw) == "SELECT 1"


# --- Extracción determinística ----------------------------------------------


def test_extract_tables_and_columns_select_simple():
    sql = "SELECT destino FROM vuelos WHERE origen = 'EZE'"
    assert _extract_tables_used(sql) == ["vuelos"]
    assert _extract_columns_used(sql) == ["destino", "origen"]


def test_extract_tables_and_columns_con_join():
    sql = (
        "SELECT v.destino, COUNT(r.id) "
        "FROM reservas r JOIN vuelos v ON r.vuelo_id = v.id "
        "WHERE r.estado = 'confirmada' GROUP BY v.destino"
    )
    assert _extract_tables_used(sql) == ["reservas", "vuelos"]
    cols = _extract_columns_used(sql)
    assert "destino" in cols
    assert "vuelo_id" in cols
    assert "estado" in cols
    assert "v" not in cols
    assert "r" not in cols


def test_build_explanation_deterministica():
    sql = "SELECT destino FROM vuelos WHERE origen = 'EZE'"
    tables = ["vuelos"]
    columns = ["destino", "origen"]
    explanation = _build_explanation(sql, tables, columns)
    assert "vuelos" in explanation
    assert "WHERE" in explanation or "filtrando" in explanation


# --- generate_sql con mock de Ollama ----------------------------------------


@patch("sql_generator._call_ollama")
def test_generate_sql_devuelve_resultado_estructurado(mock_ollama):
    mock_ollama.return_value = "SELECT destino FROM vuelos WHERE origen = 'EZE'"

    result = generate_sql("¿Destinos desde EZE?", SAMPLE_SCHEMA)

    assert isinstance(result, SQLGenerationResult)
    assert "SELECT" in result.sql.upper()
    assert result.tables_used == ["vuelos"]
    assert "destino" in result.columns_used
    assert result.confidence_self_reported == 3
    assert result.explanation
    mock_ollama.assert_called_once()
    prompt_arg = mock_ollama.call_args[0][0]
    assert "### Task" in prompt_arg
    assert prompt_arg.rstrip().endswith("```sql")


@patch("sql_generator._call_ollama")
def test_generate_sql_normaliza_fence_en_respuesta(mock_ollama):
    mock_ollama.return_value = "```sql\nSELECT destino FROM vuelos\n```"

    result = generate_sql("destinos", SAMPLE_SCHEMA)

    assert result.sql == "SELECT destino FROM vuelos"
    assert "```" not in result.sql


@patch("sql_generator._call_ollama")
def test_generate_sql_limpia_tokens_de_control(mock_ollama):
    mock_ollama.return_value = (
        "SELECT destino FROM vuelos;\n<|im_end|><|im_end|>"
    )

    result = generate_sql("destinos", SAMPLE_SCHEMA)

    assert "<|im_end|>" not in result.sql
    assert result.tables_used == ["vuelos"]


@patch("sql_generator._call_ollama")
def test_generate_sql_levanta_si_sql_invalido(mock_ollama):
    mock_ollama.return_value = "blah blah no sql"

    with pytest.raises(SQLGenerationError):
        generate_sql("pregunta", SAMPLE_SCHEMA)


@patch("sql_generator._call_ollama")
def test_generate_sql_levanta_si_falla_el_modelo(mock_ollama):
    mock_ollama.side_effect = ConnectionError("Ollama caído")

    with pytest.raises(SQLGenerationError, match="Fallo al llamar"):
        generate_sql("pregunta", SAMPLE_SCHEMA)


@patch("sql_generator._call_ollama")
def test_generate_sql_no_devuelve_resultado_vacio_ante_sql_vacio(mock_ollama):
    mock_ollama.return_value = ""

    with pytest.raises(SQLGenerationError, match="vacío"):
        generate_sql("pregunta", SAMPLE_SCHEMA)


# --- Integración (Ollama real) ----------------------------------------------


def _ollama_disponible() -> bool:
    try:
        import urllib.request

        host = "http://localhost:11434"
        with urllib.request.urlopen(f"{host}/api/tags", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.mark.integration
def test_generate_sql_integracion_con_ollama():
    if not _ollama_disponible():
        pytest.skip("Ollama no está disponible en localhost:11434")

    con = duckdb.connect(":memory:")
    with open(ROOT / "data" / "schema.sql", encoding="utf-8") as f:
        con.execute(f.read())
    schema = extract_schema(con)
    con.close()

    result = generate_sql(
        "¿Cuáles son los destinos distintos en la tabla de vuelos?",
        schema,
    )

    assert isinstance(result, SQLGenerationResult)
    assert result.sql.strip()
    assert "SELECT" in result.sql.upper()
    assert result.tables_used
    assert "vuelos" in [t.lower() for t in result.tables_used]
    assert result.confidence_self_reported == 3
    assert result.explanation
    assert "<|" not in result.sql
