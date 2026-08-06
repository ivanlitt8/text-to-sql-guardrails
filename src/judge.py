"""
judge.py

Agente 2: Juez / verificador. Ver docs/SPECS.md sección 3.

Modelo: qwen2.5:3b (JUDGE_MODEL), vía Ollama + instructor (Mode.JSON).
Recibe SQL (sin resultados de ejecución), razona (CoT), infiere qué
pregunta responde y la compara con la original (JudgeVerdict, SPECS §6).
No valida existencia de tablas/columnas en el schema (eso es guardrails).
"""

from __future__ import annotations

import os
from typing import Any

import instructor
from dotenv import load_dotenv
from instructor import Mode
from pydantic import BaseModel, Field


_DEFAULT_INFERRED = "Pregunta inferida no proporcionada"


class JudgeVerdict(BaseModel):
    inferred_question: str = Field(
        default=_DEFAULT_INFERRED,
        description=(
            "Pregunta en lenguaje natural que esta consulta SQL responde "
            "exactamente. Campo OBLIGATORIO: nunca omitirlo."
        ),
    )
    reasoning: str = Field(
        default="",
        description=(
            "Análisis paso a paso comparando la intención de la pregunta "
            "original con lo que realmente ejecuta la consulta SQL "
            "(tablas, filtros, agregaciones, ORDER BY/LIMIT)."
        ),
    )
    alignment_score: int = Field(
        default=3,
        ge=1,
        le=5,
        description=(
            "Alineación entre la pregunta original y la inferida, de 1 "
            "(desalineada) a 5 (excelente). Asignar SOLO después de reasoning."
        ),
    )
    concerns: list[str] = Field(
        default_factory=list,
        description=(
            "Lista de discrepancias concretas (filtros, JOINs, columnas, "
            "LIMIT, etc.). Lista vacía si no hay problemas."
        ),
    )
    is_degraded: bool = Field(
        default=False,
        description=(
            "Indica si el veredicto proviene de un fallback por falla técnica."
        ),
    )


def evaluate_sql_alignment(question: str, sql: str) -> JudgeVerdict:
    """
    Evalúa si la SQL generada responde realmente a la pregunta original.

    Ante fallo técnico (Ollama caído, parseo, etc.) devuelve un veredicto
    de fallback seguro (score=1, is_degraded=True), sin propagar la
    excepción al orquestador.
    """
    prompt = _build_judge_prompt(question, sql)

    try:
        verdict = _call_judge_model(prompt)
        # El LLM no debe marcar degradación; solo el fallback técnico.
        if verdict.is_degraded:
            return verdict.model_copy(update={"is_degraded": False})
        return verdict
    except Exception as exc:
        return _fallback_verdict(question, sql, exc)


def judge_sql(original_question: str, sql: str) -> JudgeVerdict:
    """Alias del stub original; preferir evaluate_sql_alignment."""
    return evaluate_sql_alignment(original_question, sql)


def _build_judge_prompt(question: str, sql: str) -> str:
    """
    Instruye CoT: primero reasoning crítico, luego score e inferred_question.
    """
    return (
        "Sos un juez imparcial de Text-to-SQL. NO tenés resultados de "
        "ejecución de la base de datos; solo la consulta SQL.\n\n"
        "Respondé en JSON según el esquema JudgeVerdict.\n"
        "MANDATORY: You MUST provide 'inferred_question' and 'reasoning'. "
        "Do NOT omit them. Set is_degraded to false.\n\n"
        "Orden de trabajo (Chain-of-Thought):\n"
        "1. En 'reasoning', analizá paso a paso: tablas usadas, filtros "
        "(WHERE), agregaciones (COUNT/SUM/AVG), JOINs, ORDER BY/LIMIT, "
        "y contrastalos con la intención de la pregunta original. Sé "
        "crítico: listá qué partes de la pregunta cubre y cuáles omite "
        "o distorsiona el SQL.\n"
        "2. Inferí en 'inferred_question' qué pregunta responde EXACTAMENTE "
        "esta consulta SQL.\n"
        "3. Recién entonces asigná 'alignment_score' de 1 a 5:\n"
        "   - 5: alineación excelente (misma intención y filtros).\n"
        "   - 3: parcialmente alineada (faltan o sobran aspectos).\n"
        "   - 1: claramente desalineada o irrelevante.\n"
        "4. Listá 'concerns' concretos si hay discrepancias. Si no hay "
        "problemas, devolvé lista vacía.\n\n"
        "NO evalúes si las tablas o columnas existen en un schema real; "
        "eso está fuera de tu alcance.\n\n"
        f"Pregunta original del usuario:\n{question}\n\n"
        f"Consulta SQL a juzgar:\n{sql}\n"
    )


def _call_judge_model(prompt: str) -> JudgeVerdict:
    """Invoca Ollama vía instructor (Mode.JSON) con salida JudgeVerdict."""
    client = _get_instructor_client()
    return client.create(
        response_model=JudgeVerdict,
        messages=[
            {
                "role": "system",
                "content": (
                    "Respondé únicamente con JSON válido para JudgeVerdict. "
                    "Primero completá 'reasoning' (análisis crítico de "
                    "tablas, filtros y agregaciones vs la pregunta). "
                    "Después 'inferred_question' y 'alignment_score' (1-5). "
                    "MANDATORY: never omit reasoning or inferred_question. "
                    "is_degraded must be false. Sé preciso en concerns."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_retries=2,
    )


def _get_instructor_client() -> Any:
    """Cliente instructor en Mode.JSON apuntando a Ollama (JUDGE_MODEL)."""
    load_dotenv()
    model = os.getenv("JUDGE_MODEL", "qwen2.5:3b")
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    base_url = host if host.endswith("/v1") else f"{host}/v1"

    # Mode.JSON evita tool-calling (InstructorRetryException por cuelgues
    # de formato con qwen2.5:3b).
    return instructor.from_provider(
        f"ollama/{model}",
        base_url=base_url,
        mode=Mode.JSON,
    )


def _fallback_verdict(
    question: str,
    sql: str,
    exc: Exception,
) -> JudgeVerdict:
    """Veredicto seguro cuando falla la llamada al modelo."""
    detail = f"{type(exc).__name__}: {exc}"
    model = os.getenv("JUDGE_MODEL", "qwen2.5:3b")
    return JudgeVerdict(
        inferred_question=(
            "No se pudo inferir la pregunta: fallo técnico del juez."
        ),
        reasoning="Fallback por error de infraestructura",
        alignment_score=1,
        concerns=[
            "Fallo técnico de respuesta en el juez",
            f"Detalle ({model}): {detail}",
            f"Pregunta original (contexto): {question[:200]}",
            f"SQL bajo evaluación (contexto): {sql[:200]}",
        ],
        is_degraded=True,
    )
