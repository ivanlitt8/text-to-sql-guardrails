"""
Tests de guardrails: validación determinística sin LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guardrails import (  # noqa: E402
    detect_write_intent,
    enforce_limit,
    validate_sql_guardrails,
)

SCHEMA_TABLES = ["aerolineas", "ciudades", "vuelos", "pasajeros", "reservas"]


def test_select_valido_es_seguro():
    sql = "SELECT destino, origen FROM vuelos WHERE origen = 'EZE' LIMIT 10"
    result = validate_sql_guardrails(sql, SCHEMA_TABLES)

    assert result.is_safe is True
    assert result.blocked_reason is None
    assert result.query_type == "SELECT"
    assert result.sanitized_sql is not None
    assert "LIMIT" in result.sanitized_sql.upper()


def test_inyecta_limit_1000_si_falta():
    sql = "SELECT destino FROM vuelos"
    result = validate_sql_guardrails(sql, SCHEMA_TABLES)

    assert result.is_safe is True
    assert result.sanitized_sql is not None
    assert result.sanitized_sql.upper().endswith("LIMIT 1000")
    # Helper directo
    assert enforce_limit(sql).upper().endswith("LIMIT 1000")


def test_no_duplica_limit_existente():
    sql = "SELECT destino FROM vuelos LIMIT 5"
    result = validate_sql_guardrails(sql, SCHEMA_TABLES)

    assert result.is_safe is True
    assert result.sanitized_sql is not None
    assert result.sanitized_sql.upper().count("LIMIT") == 1
    assert "LIMIT 5" in result.sanitized_sql.upper()


def test_enforce_limit_limpia_punto_y_coma_y_basura_trailing():
    """case_014-like: ';' + ruido tras el statement no debe romper el parser."""
    sql = (
        "SELECT p.nombre, COUNT(r.id) AS total_reservas "
        "FROM reservas r JOIN pasajeros p ON r.pasajero_id = p.id "
        "GROUP BY p.nombre HAVING COUNT(r.id) > 2;\n /******/"
    )
    cleaned = enforce_limit(sql)
    assert ";" not in cleaned
    assert "/******/" not in cleaned
    assert cleaned.upper().endswith("LIMIT 1000")
    assert cleaned.upper().startswith("SELECT")


def test_enforce_limit_limpia_backticks_trailing():
    """case_002/011: backticks al cierre rompían DuckDB al inyectar LIMIT."""
    sql = "SELECT COUNT(*) FROM vuelos WHERE destino = 'Madrid'\n``"
    cleaned = enforce_limit(sql)
    assert "`" not in cleaned
    assert cleaned == "SELECT COUNT(*) FROM vuelos WHERE destino = 'Madrid' LIMIT 1000"


def test_enforce_limit_limpia_artefacto_en_medio_del_select():
    sql = (
        "SELECT COUNT(*) AS /******/ total_reservas FROM reservas "
        "WHERE fecha_reserva >= CURRENT_DATE - INTERVAL '1 month'"
    )
    cleaned = enforce_limit(sql)
    assert "/******/" not in cleaned
    assert "COUNT(*) AS total_reservas" in cleaned.replace("  ", " ")
    assert cleaned.upper().endswith("LIMIT 1000")


def test_enforce_limit_rstrip_solo_punto_y_coma_final():
    sql = "SELECT destino FROM vuelos;"
    assert enforce_limit(sql) == "SELECT destino FROM vuelos LIMIT 1000"


@pytest.mark.parametrize(
    "sql,keyword",
    [
        ("DROP TABLE vuelos", "DROP"),
        ("DELETE FROM reservas WHERE id = 1", "DELETE"),
        ("INSERT INTO vuelos (origen) VALUES ('EZE')", "INSERT"),
        ("UPDATE vuelos SET precio = 0", "UPDATE"),
        ("ALTER TABLE vuelos ADD COLUMN x INT", "ALTER"),
        ("CREATE TABLE hack (id INT)", "CREATE"),
        ("TRUNCATE TABLE reservas", "TRUNCATE"),
        ("GRANT SELECT ON vuelos TO public", "GRANT"),
    ],
)
def test_bloquea_ddl_dml_destructivos(sql, keyword):
    result = validate_sql_guardrails(sql, SCHEMA_TABLES)

    assert result.is_safe is False
    assert result.sanitized_sql is None
    assert result.blocked_reason is not None
    assert keyword in result.blocked_reason


def test_bloquea_tabla_inventada():
    sql = "SELECT * FROM tabla_que_no_existe"
    result = validate_sql_guardrails(sql, SCHEMA_TABLES)

    assert result.is_safe is False
    assert result.sanitized_sql is None
    assert result.blocked_reason is not None
    assert "tabla_que_no_existe" in result.blocked_reason


def test_bloquea_prefijo_de_tabla_inventado():
    """Hallazgo del benchmark: sqlcoder alucinó sqlite.vuelos."""
    sql = "SELECT * FROM sqlite.vuelos"
    result = validate_sql_guardrails(sql, SCHEMA_TABLES)

    assert result.is_safe is False
    assert result.blocked_reason is not None
    assert "sqlite.vuelos" in result.blocked_reason.lower() or "sqlite" in result.blocked_reason.lower()


def test_bloquea_subqueries_excesivas():
    # 4 SELECT → supera el máximo de 3 (SPECS §7).
    sql = """
    SELECT * FROM vuelos WHERE id IN (
      SELECT vuelo_id FROM reservas WHERE pasajero_id IN (
        SELECT id FROM pasajeros WHERE pais_residencia IN (
          SELECT pais FROM aerolineas
        )
      )
    )
    """
    result = validate_sql_guardrails(sql, SCHEMA_TABLES)

    assert result.is_safe is False
    assert result.sanitized_sql is None
    assert result.blocked_reason is not None
    assert "SELECT" in result.blocked_reason or "anidad" in result.blocked_reason.lower()


def test_permite_hasta_tres_select():
    sql = """
    SELECT destino FROM vuelos WHERE id IN (
      SELECT vuelo_id FROM reservas WHERE pasajero_id IN (
        SELECT id FROM pasajeros
      )
    )
    """
    result = validate_sql_guardrails(sql, SCHEMA_TABLES)

    assert result.is_safe is True
    assert result.sanitized_sql is not None


# --- Intención de escritura en la pregunta NL -------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Actualizá el precio de todos los vuelos a Miami sumándole un 10%",
        "Borrá todas las reservas canceladas del último año",
        "Eliminá los pasajeros sin email",
        "Insertá una aerolínea nueva",
        "Modificá el estado de la reserva 1",
        "Cambiá el destino del vuelo 10",
        "Please update all flight prices",
        "Delete cancelled reservations",
    ],
)
def test_detect_write_intent_bloquea_imperativos(question):
    assert detect_write_intent(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "¿Cuál es el precio actualizado del vuelo?",
        "¿Qué pasajeros tienen reservas canceladas?",
        "Listá los destinos más reservados",
        "¿Cuáles son los 5 vuelos más baratos?",
        "Mostrá reservas eliminadas históricamente",
        "¿Cuántos vuelos hay con destino a Madrid?",
    ],
)
def test_detect_write_intent_permite_lectura_y_participios(question):
    assert detect_write_intent(question) is False
