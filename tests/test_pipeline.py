"""
Tests del pipeline end-to-end (orquestación).
Unitarios con mocks; integración contra Ollama + DuckDB reales.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guardrails import GuardrailResult  # noqa: E402
from judge import JudgeVerdict  # noqa: E402
from pipeline import FinalResponse, run_pipeline  # noqa: E402
from sql_generator import SQLGenerationError, SQLGenerationResult  # noqa: E402


def _generation(**overrides) -> SQLGenerationResult:
    data = {
        "sql": "SELECT destino FROM vuelos ORDER BY precio ASC LIMIT 5",
        "explanation": "Selecciona destinos de vuelos",
        "tables_used": ["vuelos"],
        "columns_used": ["destino", "precio"],
        "confidence_self_reported": 3,
    }
    data.update(overrides)
    return SQLGenerationResult(**data)


def _verdict(**overrides) -> JudgeVerdict:
    data = {
        "inferred_question": "¿Cuáles son los 5 vuelos más baratos?",
        "alignment_score": 4,
        "concerns": [],
    }
    data.update(overrides)
    return JudgeVerdict(**data)


@patch("pipeline.evaluate_sql_alignment")
@patch("pipeline.execute_query")
@patch("pipeline.validate_sql_guardrails")
@patch("pipeline.generate_sql")
@patch("pipeline._load_schema_context")
def test_run_pipeline_flujo_seguro(
    mock_schema,
    mock_generate,
    mock_guardrails,
    mock_execute,
    mock_judge,
):
    mock_schema.return_value = ("CREATE TABLE vuelos (id INT);", ["vuelos"])
    mock_generate.return_value = _generation()
    mock_guardrails.return_value = GuardrailResult(
        is_safe=True,
        blocked_reason=None,
        query_type="SELECT",
        sanitized_sql="SELECT destino FROM vuelos ORDER BY precio ASC LIMIT 5",
    )
    mock_execute.return_value = [{"destino": "MAD"}, {"destino": "BCN"}]
    mock_judge.return_value = _verdict(alignment_score=5)

    result = run_pipeline("¿Cuáles son los 5 vuelos más baratos?")

    assert isinstance(result, FinalResponse)
    assert result.executed is True
    assert result.results == [{"destino": "MAD"}, {"destino": "BCN"}]
    assert result.guardrail_status.is_safe is True
    assert result.judge_verdict.alignment_score == 5
    assert 0.0 <= result.confidence_final <= 1.0
    # (3/5 + 5/5) / 2 = 0.8
    assert result.confidence_final == 0.8
    mock_execute.assert_called_once()
    mock_judge.assert_called_once()


@patch("pipeline.evaluate_sql_alignment")
@patch("pipeline.execute_query")
@patch("pipeline.validate_sql_guardrails")
@patch("pipeline.generate_sql")
@patch("pipeline._load_schema_context")
def test_run_pipeline_no_ejecuta_si_guardrail_bloquea(
    mock_schema,
    mock_generate,
    mock_guardrails,
    mock_execute,
    mock_judge,
):
    mock_schema.return_value = ("CREATE TABLE vuelos (id INT);", ["vuelos"])
    mock_generate.return_value = _generation(sql="DELETE FROM vuelos")
    mock_guardrails.return_value = GuardrailResult(
        is_safe=False,
        blocked_reason="Operación prohibida detectada: DELETE",
        query_type="DELETE",
        sanitized_sql=None,
    )
    mock_judge.return_value = _verdict(alignment_score=1, concerns=["Destructivo"])

    result = run_pipeline("borrá todos los vuelos")

    assert result.executed is False
    assert result.results is None
    assert result.guardrail_status.is_safe is False
    mock_execute.assert_not_called()
    # El juez igual evalúa el SQL crudo bloqueado.
    mock_judge.assert_called_once_with("borrá todos los vuelos", "DELETE FROM vuelos")


@patch("pipeline.evaluate_sql_alignment")
@patch("pipeline.execute_query")
@patch("pipeline.validate_sql_guardrails")
@patch("pipeline.generate_sql")
@patch("pipeline._load_schema_context")
def test_run_pipeline_generacion_fallida(
    mock_schema,
    mock_generate,
    mock_guardrails,
    mock_execute,
    mock_judge,
):
    mock_schema.return_value = ("CREATE TABLE vuelos (id INT);", ["vuelos"])
    mock_generate.side_effect = SQLGenerationError("modelo caído")

    result = run_pipeline("¿Cuántos vuelos hay?")

    assert result.executed is False
    assert result.sql == ""
    assert result.guardrail_status.is_safe is False
    assert result.judge_verdict.alignment_score == 1
    mock_guardrails.assert_not_called()
    mock_execute.assert_not_called()
    mock_judge.assert_not_called()


def _ollama_disponible() -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://localhost:11434/api/tags", timeout=2
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.mark.integration
def test_run_pipeline_integracion_end_to_end():
    if not _ollama_disponible():
        pytest.skip("Ollama no está disponible en localhost:11434")

    result = run_pipeline(
        "¿Cuáles son los destinos distintos en la tabla de vuelos?"
    )

    assert isinstance(result, FinalResponse)
    assert result.question
    assert result.sql.strip()
    assert isinstance(result.guardrail_status, GuardrailResult)
    assert isinstance(result.judge_verdict, JudgeVerdict)
    assert 0.0 <= result.confidence_final <= 1.0
    if result.guardrail_status.is_safe:
        assert result.executed is True
        assert result.results is not None
    else:
        assert result.executed is False
