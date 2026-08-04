"""
judge.py

Agente 2: Juez / verificador. Ver docs/SPECS.md sección 3.

Modelo: qwen2.5:3b (JUDGE_MODEL), vía Ollama + instructor.
Recibe SQL (sin resultados de ejecución), infiere qué pregunta responde
y la compara con la pregunta original (JudgeVerdict, SPECS.md §6).
No valida existencia de tablas/columnas en el schema (eso es guardrails).
"""

from __future__ import annotations

import os
from typing import Any

import instructor
from dotenv import load_dotenv
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
    alignment_score: int = Field(
        default=3,
        ge=1,
        le=5,
        description=(
            "Alineación entre la pregunta original y la inferida, de 1 "
            "(desalineada) a 5 (excelente)."
        ),
    )
    concerns: list[str] = Field(
        default_factory=list,
        description=(
            "Lista de discrepancias concretas (filtros, JOINs, columnas, "
            "LIMIT, etc.). Lista vacía si no hay problemas."
        ),
    )


def evaluate_sql_alignment(question: str, sql: str) -> JudgeVerdict:
    """
    Evalúa si la SQL generada responde realmente a la pregunta original.

    Ante fallo técnico (Ollama caído, parseo, etc.) devuelve un veredicto
    de fallback seguro (score=1 + concern del error), sin propagar la
    excepción al orquestador.
    """
    prompt = _build_judge_prompt(question, sql)

    try:
        return _call_judge_model(prompt)
    except Exception as exc:
        return _fallback_verdict(question, sql, exc)


def judge_sql(original_question: str, sql: str) -> JudgeVerdict:
    """Alias del stub original; preferir evaluate_sql_alignment."""
    return evaluate_sql_alignment(original_question, sql)


def _build_judge_prompt(question: str, sql: str) -> str:
    """
    Instruye al modelo a inferir la pregunta que responde el SQL y
    puntuación de alineación contra la pregunta original.
    """
    return (
        "Sos un juez imparcial de Text-to-SQL. NO tenés resultados de "
        "ejecución de la base de datos; solo la consulta SQL.\n\n"
        "MANDATORY: You MUST provide the 'inferred_question' field in "
        "your response JSON. Do NOT omit it.\n\n"
        "Tu tarea:\n"
        "1. Inferí en lenguaje natural qué pregunta responde EXACTAMENTE "
        "esta consulta SQL (campo inferred_question — OBLIGATORIO).\n"
        "2. Compará esa pregunta inferida con la pregunta original del "
        "usuario de forma imparcial.\n"
        "3. Asigná alignment_score de 1 a 5:\n"
        "   - 5: alineación excelente (misma intención y filtros).\n"
        "   - 3: parcialmente alineada (faltan o sobran aspectos).\n"
        "   - 1: claramente desalineada o irrelevante.\n"
        "4. Listá concerns concretos si hay discrepancias (filtros "
        "faltantes, agregaciones incorrectas, columnas/tablas distintas "
        "a lo pedido, límites, ordenamientos, etc.). Si no hay "
        "problemas, devolvé concerns como lista vacía.\n\n"
        "NO evalúes si las tablas o columnas existen en un schema real; "
        "eso está fuera de tu alcance.\n\n"
        f"Pregunta original del usuario:\n{question}\n\n"
        f"Consulta SQL a juzgar:\n{sql}\n"
    )


def _call_judge_model(prompt: str) -> JudgeVerdict:
    """Invoca Ollama vía instructor con salida JudgeVerdict."""
    client = _get_instructor_client()
    return client.create(
        response_model=JudgeVerdict,
        messages=[
            {
                "role": "system",
                "content": (
                    "Respondé únicamente con el esquema JudgeVerdict "
                    "solicitado. "
                    "MANDATORY: You MUST provide the 'inferred_question' "
                    "field in your response JSON. Do NOT omit it. "
                    "Sé preciso y conciso en concerns."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_retries=2,
    )


def _get_instructor_client() -> Any:
    """Cliente instructor apuntando a Ollama (JUDGE_MODEL)."""
    load_dotenv()
    model = os.getenv("JUDGE_MODEL", "qwen2.5:3b")
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    base_url = host if host.endswith("/v1") else f"{host}/v1"

    # qwen2.5 soporta tools; from_provider elige TOOLS automáticamente.
    return instructor.from_provider(
        f"ollama/{model}",
        base_url=base_url,
    )


def _fallback_verdict(
    question: str,
    sql: str,
    exc: Exception,
) -> JudgeVerdict:
    """Veredicto seguro cuando falla la llamada al modelo."""
    detail = f"{type(exc).__name__}: {exc}"
    return JudgeVerdict(
        inferred_question=(
            "No se pudo inferir la pregunta: fallo técnico del juez."
        ),
        alignment_score=1,
        concerns=[
            (
                "Fallo técnico al evaluar alineación con el modelo juez "
                f"({os.getenv('JUDGE_MODEL', 'qwen2.5:3b')}): {detail}"
            ),
            f"Pregunta original (contexto): {question[:200]}",
            f"SQL bajo evaluación (contexto): {sql[:200]}",
        ],
    )
