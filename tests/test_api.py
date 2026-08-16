"""
Tests de la capa HTTP (FastAPI). Mockean run_pipeline y los checks de ready.
No requieren Ollama.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import ReadyResponse, app, check_readiness  # noqa: E402
from guardrails import GuardrailResult  # noqa: E402
from judge import JudgeVerdict  # noqa: E402
from pipeline import FinalResponse  # noqa: E402


def _final_response(**overrides) -> FinalResponse:
    data = {
        "question": "¿Cuántos vuelos hay con destino a Madrid?",
        "sql": "SELECT COUNT(*) FROM vuelos WHERE destino = 'Madrid'",
        "executed": True,
        "results": [{"count": 4}],
        "confidence_final": 0.8,
        "guardrail_status": GuardrailResult(
            is_safe=True,
            blocked_reason=None,
            query_type="SELECT",
            sanitized_sql="SELECT COUNT(*) FROM vuelos WHERE destino = 'Madrid' LIMIT 1000",
        ),
        "judge_verdict": JudgeVerdict(
            inferred_question="¿Cuántos vuelos van a Madrid?",
            reasoning="COUNT con filtro destino.",
            alignment_score=5,
            concerns=[],
            is_degraded=False,
        ),
        "execution_error": None,
    }
    data.update(overrides)
    return FinalResponse(**data)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_ok(client: TestClient):
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@patch("api.run_pipeline")
def test_ask_delega_en_pipeline(mock_pipeline, client: TestClient):
    mock_pipeline.return_value = _final_response()
    response = client.post(
        "/v1/ask",
        json={"question": "  ¿Cuántos vuelos hay con destino a Madrid?  "},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["executed"] is True
    assert body["results"] == [{"count": 4}]
    assert body["guardrail_status"]["is_safe"] is True
    mock_pipeline.assert_called_once_with(
        "¿Cuántos vuelos hay con destino a Madrid?"
    )


@patch("api.run_pipeline")
def test_ask_write_intent_sigue_siendo_200(mock_pipeline, client: TestClient):
    mock_pipeline.return_value = _final_response(
        question="Borrá todas las reservas",
        sql="",
        executed=False,
        results=None,
        confidence_final=0.2,
        guardrail_status=GuardrailResult(
            is_safe=False,
            blocked_reason="Intención de escritura detectada.",
            query_type="WRITE_INTENT",
            sanitized_sql=None,
        ),
        judge_verdict=JudgeVerdict(
            inferred_question="No evaluable",
            reasoning="Bloqueado",
            alignment_score=1,
            concerns=["escritura"],
            is_degraded=False,
        ),
    )
    response = client.post("/v1/ask", json={"question": "Borrá todas las reservas"})
    assert response.status_code == 200
    assert response.json()["executed"] is False
    assert response.json()["guardrail_status"]["query_type"] == "WRITE_INTENT"


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "   "}])
def test_ask_question_vacia_422(client: TestClient, payload: dict):
    response = client.post("/v1/ask", json=payload)
    assert response.status_code == 422


@patch("api.check_readiness")
def test_ready_ok(mock_ready, client: TestClient):
    mock_ready.return_value = ReadyResponse(
        ready=True,
        checks={
            "duckdb": True,
            "ollama": True,
            "generator_model": True,
            "judge_model": True,
        },
        details=None,
    )
    response = client.get("/v1/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True


@patch("api.check_readiness")
def test_ready_503_si_falta_algo(mock_ready, client: TestClient):
    mock_ready.return_value = ReadyResponse(
        ready=False,
        checks={
            "duckdb": True,
            "ollama": False,
            "generator_model": False,
            "judge_model": False,
        },
        details={"ollama": "ConnectionError"},
    )
    response = client.get("/v1/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False


@patch("api.ollama.Client")
@patch("api.open_connection_for_schema")
def test_check_readiness_modelos_presentes(mock_db, mock_client_cls):
    mock_db.return_value.execute.return_value = None
    mock_db.return_value.close.return_value = None
    mock_client_cls.return_value.list.return_value = {
        "models": [
            {"name": "sqlcoder:latest"},
            {"name": "qwen2.5:3b"},
        ]
    }
    result = check_readiness()
    assert result.ready is True
    assert result.checks["generator_model"] is True
    assert result.checks["judge_model"] is True
