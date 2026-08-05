"""
pipeline.py

Orquestación del flujo MVP (SPECS.md §2 / §9):
  intención NL → generador → guardrails SQL → ejecución → juez → FinalResponse
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from executor import (
    ExecutionResult,
    execute_query,
    list_schema_tables,
    open_connection_for_schema,
)
from guardrails import (
    GuardrailResult,
    detect_write_intent,
    validate_sql_guardrails,
)
from judge import JudgeVerdict, evaluate_sql_alignment
from schema_extractor import extract_schema
from sql_generator import SQLGenerationError, generate_sql


class FinalResponse(BaseModel):
    question: str
    sql: str
    executed: bool
    results: list[dict] | None
    confidence_final: float = Field(ge=0.0, le=1.0)
    guardrail_status: GuardrailResult
    judge_verdict: JudgeVerdict
    execution_error: str | None = None


def run_pipeline(
    question: str,
    db_path: str | None = None,
) -> FinalResponse:
    """
    Ejecuta el pipeline completo para una pregunta en lenguaje natural.
    """
    if detect_write_intent(question):
        return _response_write_intent_blocked(question)

    schema, schema_tables = _load_schema_context(db_path)

    try:
        generation = generate_sql(question, schema)
    except SQLGenerationError as exc:
        return _response_generation_failed(question, exc)

    guardrail = validate_sql_guardrails(generation.sql, schema_tables)

    sql_for_judge = (
        guardrail.sanitized_sql
        if guardrail.is_safe and guardrail.sanitized_sql
        else generation.sql
    )

    executed = False
    results: list[dict] | None = None
    execution_error: str | None = None
    if guardrail.is_safe and guardrail.sanitized_sql:
        exec_result: ExecutionResult = execute_query(
            guardrail.sanitized_sql,
            db_path=db_path,
        )
        if exec_result.error:
            executed = False
            results = None
            execution_error = exec_result.error
        else:
            executed = True
            results = exec_result.rows

    verdict = evaluate_sql_alignment(question, sql_for_judge)
    confidence = _compose_confidence(
        generation.confidence_self_reported,
        verdict.alignment_score,
    )

    return FinalResponse(
        question=question,
        sql=sql_for_judge,
        executed=executed,
        results=results,
        confidence_final=confidence,
        guardrail_status=guardrail,
        judge_verdict=verdict,
        execution_error=execution_error,
    )


def _load_schema_context(db_path: str | None) -> tuple[str, list[str]]:
    con = open_connection_for_schema(db_path)
    try:
        schema_ddl = extract_schema(con)
        tables = list_schema_tables(db_path)
        return schema_ddl, tables
    finally:
        con.close()


def _compose_confidence(generator_score: int, judge_score: int) -> float:
    """Promedio de scores 1-5 normalizados a [0, 1]."""
    gen_norm = max(1, min(5, generator_score)) / 5.0
    judge_norm = max(1, min(5, judge_score)) / 5.0
    return round((gen_norm + judge_norm) / 2.0, 4)


def _response_write_intent_blocked(question: str) -> FinalResponse:
    """Abort temprano: la pregunta pide mutar datos (antes del generador)."""
    guardrail = GuardrailResult(
        is_safe=False,
        blocked_reason=(
            "Intención de escritura detectada en la pregunta "
            "(UPDATE/DELETE/INSERT/…). Solo se permiten consultas de lectura."
        ),
        query_type="WRITE_INTENT",
        sanitized_sql=None,
    )
    verdict = JudgeVerdict(
        inferred_question=(
            "No evaluable: la pregunta pide modificar datos y fue bloqueada."
        ),
        alignment_score=1,
        concerns=[
            "Pipeline abortado antes del generador por intención de escritura."
        ],
    )
    return FinalResponse(
        question=question,
        sql="",
        executed=False,
        results=None,
        confidence_final=_compose_confidence(1, 1),
        guardrail_status=guardrail,
        judge_verdict=verdict,
        execution_error=None,
    )


def _response_generation_failed(
    question: str,
    exc: SQLGenerationError,
) -> FinalResponse:
    """FinalResponse degradado si el generador no pudo producir SQL."""
    guardrail = GuardrailResult(
        is_safe=False,
        blocked_reason=f"Fallo en generación de SQL: {exc}",
        query_type="UNKNOWN",
        sanitized_sql=None,
    )
    verdict = JudgeVerdict(
        inferred_question="No evaluable: el generador no produjo SQL.",
        alignment_score=1,
        concerns=[f"Pipeline detenido antes del juez: {exc}"],
    )
    return FinalResponse(
        question=question,
        sql="",
        executed=False,
        results=None,
        confidence_final=_compose_confidence(1, 1),
        guardrail_status=guardrail,
        judge_verdict=verdict,
        execution_error=None,
    )
