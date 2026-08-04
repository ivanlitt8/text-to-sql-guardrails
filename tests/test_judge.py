"""
Tests del juez (Agente 2).
Unitarios con mock de la llamada al modelo; integración contra Ollama real.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from judge import (  # noqa: E402
    JudgeVerdict,
    _build_judge_prompt,
    evaluate_sql_alignment,
)


def _sample_verdict(**overrides) -> JudgeVerdict:
    data = {
        "inferred_question": "¿Cuáles son los destinos distintos?",
        "alignment_score": 5,
        "concerns": [],
    }
    data.update(overrides)
    return JudgeVerdict(**data)


# --- Prompt -----------------------------------------------------------------


def test_build_judge_prompt_incluye_pregunta_y_sql():
    prompt = _build_judge_prompt(
        "¿Destinos distintos?",
        "SELECT DISTINCT destino FROM vuelos",
    )
    assert "¿Destinos distintos?" in prompt
    assert "SELECT DISTINCT destino FROM vuelos" in prompt
    assert "inferred_question" in prompt or "Inferí" in prompt
    assert "alignment_score" in prompt
    assert "MANDATORY" in prompt
    assert "Do NOT omit it" in prompt


def test_judge_verdict_acepta_omision_de_inferred_question():
    """Resiliencia: qwen a veces omite inferred_question en tool calls."""
    verdict = JudgeVerdict(alignment_score=3, concerns=["detalle"])
    assert verdict.inferred_question == "Pregunta inferida no proporcionada"
    assert verdict.alignment_score == 3


# --- Unitarios con mock -----------------------------------------------------


@patch("judge._call_judge_model")
def test_evaluate_sql_alignment_retorna_judge_verdict(mock_call):
    mock_call.return_value = _sample_verdict()

    result = evaluate_sql_alignment(
        "¿Cuáles son los destinos distintos?",
        "SELECT DISTINCT destino FROM vuelos",
    )

    assert isinstance(result, JudgeVerdict)
    assert result.alignment_score == 5
    assert result.concerns == []
    assert "destino" in result.inferred_question.lower() or result.inferred_question
    mock_call.assert_called_once()


@patch("judge._call_judge_model")
def test_evaluate_sql_alignment_propaga_veredicto_desalineado(mock_call):
    mock_call.return_value = _sample_verdict(
        inferred_question="¿Cuántos vuelos hay en total?",
        alignment_score=2,
        concerns=["La SQL cuenta filas; la pregunta pedía listar destinos."],
    )

    result = evaluate_sql_alignment(
        "¿Cuáles son los destinos distintos?",
        "SELECT COUNT(*) FROM vuelos",
    )

    assert result.alignment_score == 2
    assert len(result.concerns) >= 1


@patch("judge._call_judge_model")
def test_evaluate_sql_alignment_fallback_si_falla_el_modelo(mock_call):
    mock_call.side_effect = ConnectionError("Ollama no responde")

    result = evaluate_sql_alignment(
        "¿Cuántos pasajeros hay?",
        "SELECT COUNT(*) FROM pasajeros",
    )

    assert isinstance(result, JudgeVerdict)
    assert result.alignment_score == 1
    assert result.concerns
    assert any("Fallo técnico" in c or "ConnectionError" in c for c in result.concerns)


# --- Integración (Ollama real) ----------------------------------------------


def _ollama_disponible() -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://localhost:11434/api/tags", timeout=2
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _modelo_juez_disponible() -> bool:
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(
            "http://localhost:11434/api/tags", timeout=2
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        names = {m.get("name", "") for m in payload.get("models", [])}
        # Ollama puede listar "qwen2.5:3b" o variantes con digest.
        return any("qwen2.5" in n and "3b" in n for n in names) or any(
            n.startswith("qwen2.5:3b") for n in names
        )
    except Exception:
        return False


@pytest.mark.integration
def test_judge_integracion_sql_alineado_y_desalineado():
    if not _ollama_disponible():
        pytest.skip("Ollama no está disponible en localhost:11434")
    if not _modelo_juez_disponible():
        pytest.skip("Modelo qwen2.5:3b no está descargado en Ollama")

    question = "¿Cuáles son los destinos distintos en la tabla de vuelos?"
    sql_aligned = "SELECT DISTINCT destino FROM vuelos"
    sql_misaligned = "SELECT COUNT(*) AS total FROM pasajeros"

    aligned = evaluate_sql_alignment(question, sql_aligned)
    misaligned = evaluate_sql_alignment(question, sql_misaligned)

    assert isinstance(aligned, JudgeVerdict)
    assert isinstance(misaligned, JudgeVerdict)
    assert 1 <= aligned.alignment_score <= 5
    assert 1 <= misaligned.alignment_score <= 5
    # El SQL alineado debe puntuar estrictamente mejor que el desalineado.
    assert aligned.alignment_score > misaligned.alignment_score
    assert aligned.inferred_question.strip()
    assert misaligned.inferred_question.strip()
    # El desalineado debería mencionar alguna preocupación.
    assert misaligned.alignment_score <= 3 or len(misaligned.concerns) > 0
