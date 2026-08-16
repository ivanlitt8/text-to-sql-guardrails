"""
Tests de sql_generator: prompt Defog, validación sqlparse y generate_sql.
Los unitarios mockean la llamada a Ollama (no requieren el daemon).
El test de integración sí habla con Ollama real y se salta si no está disponible.
"""

from __future__ import annotations

import re
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
    _apply_chile_residence_fix,
    _normalize_sql,
    _repair_column_typos,
    _strip_unsolicited_airline_join,
    _resolve_ollama_timeout,
    _validate_sql_syntax,
    build_defog_prompt,
    generate_sql,
)


SAMPLE_SCHEMA = """
CREATE TABLE vuelos (
    id INTEGER PRIMARY KEY,
    origen VARCHAR,
    destino VARCHAR,
    precio DECIMAL,
    fecha DATE
);
""".strip()


FULL_SCHEMA_SNIPPET = """
CREATE TABLE aerolineas (
    id INTEGER PRIMARY KEY,
    nombre VARCHAR NOT NULL,
    pais VARCHAR NOT NULL
);

CREATE TABLE vuelos (
    id INTEGER PRIMARY KEY,
    aerolinea_id INTEGER NOT NULL,
    origen VARCHAR NOT NULL,
    destino VARCHAR NOT NULL,
    fecha DATE NOT NULL,
    duracion_minutos INTEGER NOT NULL,
    precio DECIMAL(10,2) NOT NULL,
    asientos_disponibles INTEGER NOT NULL
);

CREATE TABLE pasajeros (
    id INTEGER PRIMARY KEY,
    nombre VARCHAR NOT NULL,
    email VARCHAR NOT NULL,
    pais_residencia VARCHAR NOT NULL
);

CREATE TABLE reservas (
    id INTEGER PRIMARY KEY,
    pasajero_id INTEGER NOT NULL,
    vuelo_id INTEGER NOT NULL,
    fecha_reserva DATE NOT NULL,
    precio_pagado DECIMAL(10,2) NOT NULL,
    estado VARCHAR NOT NULL
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
    # Política dialecto DuckDB / geografía / ausencia
    assert "Dialect: DuckDB" in prompt
    assert "Do not use to_date()" in prompt
    assert "dayofweek" in prompt
    assert "NOT EXISTS or LEFT JOIN" in prompt
    # Remediación F1–F6 (golden 009–018)
    assert "HAVING SUM(r.precio_pagado) > (" in prompt
    assert "SELECT AVG(precio_pagado) FROM reservas" in prompt
    assert "SELECT AVG(cnt) FROM (" in prompt
    assert "WHERE NOT EXISTS (" in prompt
    assert "HAVING COUNT(*) > 3 AND AVG(precio) > 200" in prompt
    assert "HAVING COUNT(*) > 2" in prompt
    assert "total_confirmadas" in prompt
    assert "dayofweek(fecha_reserva) IN (0, 6)" in prompt
    assert "CURRENT_DATE + INTERVAL '7 days'" in prompt
    assert "LEFT JOIN reservas r ON r.vuelo_id = v.id" in prompt
    assert "GROUP BY p.pais_residencia, a.nombre" in prompt
    assert "c.pais = 'España'" in prompt
    assert "Output raw SQL only" in prompt
    assert "SELECT DISTINCT" in prompt
    assert "do not JOIN aerolineas unless asked" in prompt
    # JOIN policy suavizada (no la prosa agresiva anterior)
    assert "JOIN policy:" in prompt
    assert "Never omit explicit filters" in prompt
    assert "AGGREGATION MAPPING" not in prompt
    assert "STRICT FILTERS" not in prompt


def test_build_defog_prompt_no_cierra_el_fence_sql():
    prompt = build_defog_prompt("pregunta", SAMPLE_SCHEMA)
    assert prompt.count("```") == 1


# --- Repair de typos de columna (paso B, case_016) --------------------------


def test_repair_column_typos_tfcha_a_fecha_case_016():
    """SQL real del golden 81% (200309Z): v.tfcha → v.fecha."""
    sql = (
        "SELECT v.id, v.origen, v.destino, a.nombre, COUNT(r.id) AS total_reservas "
        "FROM vuelos v JOIN aerolineas a ON v.aerolinea_id = a.id "
        "LEFT JOIN reservas r ON r.vuelo_id = v.id AND r.estado = 'confirmada' "
        "WHERE v.fecha >= CURRENT_DATE AND v.tfcha < CURRENT_DATE + INTERVAL '7 days' "
        "GROUP BY v.id, v.origen, v.destino, a.nombre "
        "ORDER BY total_reservas DESC NULLS LAST LIMIT 1000"
    )
    repaired = _repair_column_typos(sql, FULL_SCHEMA_SNIPPET)
    assert "v.tfcha" not in repaired
    assert "v.fecha < CURRENT_DATE + INTERVAL '7 days'" in repaired
    assert "JOIN aerolineas" in repaired  # no reescribe JOINs


def test_repair_identidad_casos_verdes_golden_81():
    """No debe alterar SQL que ya matcheaba (003 / 011 / 013)."""
    cases = [
        (
            "SELECT a.nombre AS aerolinea, AVG(v.precio) AS precio_promedio "
            "FROM vuelos v JOIN aerolineas a ON v.aerolinea_id = a.id "
            "GROUP BY a.nombre"
        ),
        (
            "SELECT p.nombre FROM pasajeros p WHERE id NOT IN "
            "(SELECT r.pasajero_id FROM reservas r) ORDER BY p.nombre"
        ),
        (
            "SELECT destino, COUNT(*) AS total_vuelos, AVG(precio) AS precio_medio "
            "FROM vuelos GROUP BY destino "
            "HAVING COUNT(*) > 3 AND AVG(precio) > 200 ORDER BY destino"
        ),
    ]
    for sql in cases:
        assert _repair_column_typos(sql, FULL_SCHEMA_SNIPPET) == sql


def test_repair_column_typos_ambiguo_no_cambia():
    sql = "SELECT v.xxx FROM vuelos v"
    assert _repair_column_typos(sql, FULL_SCHEMA_SNIPPET) == sql


@patch("sql_generator._call_ollama")
def test_generate_sql_aplica_repair_de_typos(mock_ollama):
    mock_ollama.return_value = (
        "SELECT v.id, v.tfcha FROM vuelos v WHERE v.destino = 'Madrid'"
    )
    result = generate_sql("fecha del vuelo", FULL_SCHEMA_SNIPPET)
    assert "tfcha" not in result.sql
    assert "v.fecha" in result.sql


def test_resolve_ollama_timeout_default(monkeypatch):
    monkeypatch.delenv("OLLAMA_TIMEOUT", raising=False)
    assert _resolve_ollama_timeout() == 600.0


def test_resolve_ollama_timeout_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_TIMEOUT", "120")
    assert _resolve_ollama_timeout() == 120.0


CASE_008_QUESTION = (
    "¿Qué pasajeros de Chile hicieron reservas para vuelos con destino "
    "a un país distinto al de su residencia?"
)
CASE_008_SQL_GENERATED = (
    "SELECT p.nombre FROM pasajeros p JOIN reservas r ON p.id = r.pasajero_id "
    "JOIN vuelos v ON r.vuelo_id = v.id JOIN ciudades c ON v.destino = c.ciudad "
    "WHERE p.pais_residencia != c.pais AND r.estado = 'confirmada' LIMIT 1000"
)
CASE_016_QUESTION = (
    "Muestra los vuelos programados para salir en los próximos 7 días "
    "con su cantidad de reservas."
)
CASE_016_SQL_REPAIRED = (
    "SELECT v.id, v.origen, v.destino, a.nombre, COUNT(r.id) AS total_reservas "
    "FROM vuelos v JOIN aerolineas a ON v.aerolinea_id = a.id "
    "LEFT JOIN reservas r ON r.vuelo_id = v.id AND r.estado = 'confirmada' "
    "WHERE v.fecha >= CURRENT_DATE AND v.fecha < CURRENT_DATE + INTERVAL '7 days' "
    "GROUP BY v.id, v.origen, v.destino, a.nombre "
    "ORDER BY total_reservas DESC NULLS LAST LIMIT 1000"
)


def test_chile_fix_inserta_igualdad_y_distinct():
    out = _apply_chile_residence_fix(CASE_008_SQL_GENERATED, CASE_008_QUESTION)
    assert "p.pais_residencia = 'Chile'" in out
    assert "p.pais_residencia != c.pais" in out
    assert out.upper().startswith("SELECT DISTINCT")


def test_chile_fix_identidad_casos_ajenos():
    q_sql = [
        (
            "¿Cuál es el precio promedio de los vuelos por aerolínea?",
            "SELECT a.nombre AS aerolinea, AVG(v.precio) AS precio_promedio "
            "FROM vuelos v JOIN aerolineas a ON v.aerolinea_id = a.id "
            "GROUP BY a.nombre",
        ),
        (
            "¿Qué pasajeros registrados nunca han realizado una reserva?",
            "SELECT p.nombre FROM pasajeros p WHERE id NOT IN "
            "(SELECT r.pasajero_id FROM reservas r) ORDER BY p.nombre",
        ),
        (
            "¿Qué destinos tienen más de 3 vuelos asignados y un precio medio superior a 200?",
            "SELECT destino, COUNT(*) AS total_vuelos, AVG(precio) AS precio_medio "
            "FROM vuelos GROUP BY destino HAVING COUNT(*) > 3 AND AVG(precio) > 200 "
            "ORDER BY destino",
        ),
        (CASE_016_QUESTION, CASE_016_SQL_REPAIRED),
        (
            "¿Cuál es el pasajero de Argentina que más dinero gastó en vuelos hacia Europa?",
            "SELECT p.nombre, SUM(r.precio_pagado) AS total_gastado FROM pasajeros p "
            "JOIN reservas r ON p.id = r.pasajero_id JOIN vuelos v ON r.vuelo_id = v.id "
            "JOIN ciudades c ON v.destino = c.ciudad "
            "WHERE p.pais_residencia = 'Argentina' AND c.pais = 'España' "
            "AND r.estado = 'confirmada' GROUP BY p.id, p.nombre "
            "ORDER BY total_gastado DESC LIMIT 1",
        ),
    ]
    for question, sql in q_sql:
        assert _apply_chile_residence_fix(sql, question) == sql


@patch("sql_generator._call_ollama")
def test_generate_sql_aplica_chile_fix(mock_ollama):
    mock_ollama.return_value = CASE_008_SQL_GENERATED
    result = generate_sql(CASE_008_QUESTION, FULL_SCHEMA_SNIPPET)
    assert "p.pais_residencia = 'Chile'" in result.sql
    assert result.sql.upper().startswith("SELECT DISTINCT")


CASE_012_QUESTION = "¿Qué vuelos no tienen ninguna reserva confirmada registrada?"
CASE_012_SQL_GENERATED = (
    "SELECT v.id, v.origen, v.destino, a.nombre FROM vuelos v "
    "JOIN aerolineas a ON v.aerolinea_id = a.id "
    "WHERE NOT EXISTS (SELECT 1 FROM reservas r WHERE r.vuelo_id = v.id "
    "AND r.estado = 'confirmada') ORDER BY v.id LIMIT 1000"
)


def test_airline_strip_012_saca_join_y_columna():
    out = _strip_unsolicited_airline_join(CASE_012_SQL_GENERATED, CASE_012_QUESTION)
    assert "aerolineas" not in out.lower()
    assert "a.nombre" not in out.lower()
    assert "NOT EXISTS" in out.upper()
    assert "v.origen" in out
    assert "v.destino" in out


def test_airline_strip_016_conserva_left_join_reservas():
    out = _strip_unsolicited_airline_join(CASE_016_SQL_REPAIRED, CASE_016_QUESTION)
    assert "JOIN aerolineas" not in out.lower()
    assert "a.nombre" not in out.lower()
    assert re.search(r"LEFT\s+JOIN\s+reservas", out, flags=re.IGNORECASE)
    assert "r.estado = 'confirmada'" in out
    assert "INTERVAL '7 days'" in out
    assert "COUNT(r.id)" in out


def test_airline_strip_identidad_si_pregunta_pide_aerolinea():
    q_sql = [
        (
            "¿Cuál es el precio promedio de los vuelos por aerolínea?",
            "SELECT a.nombre AS aerolinea, AVG(v.precio) AS precio_promedio "
            "FROM vuelos v JOIN aerolineas a ON v.aerolinea_id = a.id "
            "GROUP BY a.nombre",
        ),
        (
            "¿Cuál fue el vuelo con mayor cantidad de reservas confirmadas, "
            "incluyendo origen, destino y aerolínea?",
            "SELECT v.id, v.origen, v.destino, a.nombre, COUNT(r.id) AS total_confirmadas "
            "FROM reservas r JOIN vuelos v ON r.vuelo_id = v.id "
            "JOIN aerolineas a ON v.aerolinea_id = a.id "
            "WHERE r.estado = 'confirmada' "
            "GROUP BY v.id, v.origen, v.destino, a.nombre "
            "ORDER BY total_confirmadas DESC LIMIT 1",
        ),
        (
            "Aerolíneas con una cantidad de vuelos superior al promedio "
            "de vuelos por aerolínea.",
            "SELECT a.nombre AS aerolinea, COUNT(v.id) AS n_vuelos "
            "FROM aerolineas a JOIN vuelos v ON v.aerolinea_id = a.id "
            "GROUP BY a.id, a.nombre HAVING COUNT(v.id) > 1",
        ),
        (
            "Muestra el total de ingresos por país de residencia del pasajero "
            "y aerolínea.",
            "SELECT p.pais_residencia, a.nombre AS aerolinea, "
            "SUM(r.precio_pagado) AS ingresos FROM reservas r "
            "JOIN pasajeros p ON r.pasajero_id = p.id "
            "JOIN vuelos v ON r.vuelo_id = v.id "
            "JOIN aerolineas a ON v.aerolinea_id = a.id "
            "WHERE r.estado = 'confirmada' "
            "GROUP BY p.pais_residencia, a.nombre",
        ),
    ]
    for question, sql in q_sql:
        assert _strip_unsolicited_airline_join(sql, question) == sql


def test_airline_strip_identidad_casos_sin_join_aerolineas():
    q_sql = [
        (CASE_008_QUESTION, CASE_008_SQL_GENERATED),
        (
            "¿Qué pasajeros registrados nunca han realizado una reserva?",
            "SELECT p.nombre FROM pasajeros p WHERE id NOT IN "
            "(SELECT r.pasajero_id FROM reservas r) ORDER BY p.nombre",
        ),
        (
            "¿Qué destinos tienen más de 3 vuelos asignados y un precio medio superior a 200?",
            "SELECT destino, COUNT(*) AS total_vuelos, AVG(precio) AS precio_medio "
            "FROM vuelos GROUP BY destino HAVING COUNT(*) > 3 AND AVG(precio) > 200 "
            "ORDER BY destino",
        ),
    ]
    for question, sql in q_sql:
        assert _strip_unsolicited_airline_join(sql, question) == sql


@patch("sql_generator._call_ollama")
def test_generate_sql_aplica_airline_strip(mock_ollama):
    mock_ollama.return_value = CASE_012_SQL_GENERATED
    result = generate_sql(CASE_012_QUESTION, FULL_SCHEMA_SNIPPET)
    assert "aerolineas" not in result.sql.lower()
    assert "NOT EXISTS" in result.sql.upper()


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
def test_generate_sql_limpia_backticks_y_fence_basura(mock_ollama):
    mock_ollama.return_value = (
        "SELECT COUNT(*) FROM vuelos WHERE destino = 'Madrid'\n``"
    )
    result = generate_sql("¿Cuántos a Madrid?", SAMPLE_SCHEMA)
    assert "`" not in result.sql
    assert "SELECT COUNT(*) FROM vuelos WHERE destino = 'Madrid'" == result.sql


@patch("sql_generator._call_ollama")
def test_generate_sql_recupera_select_tras_artefacto_fence(mock_ollama):
    mock_ollama.return_value = (
        "/******/ SELECT COUNT(*) AS total_reservas FROM reservas "
        "WHERE dayofweek(fecha_reserva) IN (0, 6)"
    )
    result = generate_sql("fines de semana", SAMPLE_SCHEMA)
    assert result.sql.upper().startswith("SELECT")
    assert "/******/" not in result.sql
    assert "total_reservas" in result.sql


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
