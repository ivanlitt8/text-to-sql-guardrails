"""
eval/evaluate.py

Evalúa el pipeline end-to-end contra eval/golden_dataset.json.

Métricas:
  - execution_rate: % de casos no-adversariales con executed=True
  - adversarial_block_rate: % de adversarial bloqueados por guardrails
  - avg_confidence / avg_judge_score
  - high_confidence_rate: % con confidence_final >= 0.70
  - execution_accuracy: % de casos con expected_sql donde el result set
    del SQL generado coincide con el del expected_sql (si ambos corren)

Uso (desde la raíz del repo, con .venv activo y Ollama levantado):
  python eval/evaluate.py
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from executor import execute_query  # noqa: E402
from pipeline import FinalResponse, run_pipeline  # noqa: E402

GOLDEN_PATH = ROOT / "eval" / "golden_dataset.json"
RESULTS_DIR = ROOT / "eval" / "results"
HIGH_CONFIDENCE_THRESHOLD = 0.70


@dataclass
class CaseResult:
    id: str
    question: str
    difficulty: str
    executed: bool
    is_safe: bool
    blocked_reason: str | None
    sql: str
    confidence_final: float
    judge_score: int
    inferred_question: str
    concerns: list[str]
    results_count: int | None
    execution_match: bool | None  # None si no aplica
    adversarial_blocked: bool | None
    error: str | None = None
    elapsed_s: float = 0.0


@dataclass
class EvalSummary:
    n_cases: int
    execution_rate: float
    adversarial_block_rate: float
    avg_confidence: float
    avg_judge_score: float
    high_confidence_rate: float
    high_confidence_count: int
    execution_accuracy: float | None
    execution_accuracy_n: int
    cases: list[CaseResult] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    total_elapsed_s: float = 0.0


def main() -> int:
    cases = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    results: list[CaseResult] = []

    print(f"Golden dataset: {len(cases)} casos")
    print("-" * 60)

    for case in cases:
        case_id = case["id"]
        question = case["question"]
        difficulty = case["difficulty"]
        print(f"[{case_id}] {difficulty}: {question[:70]}…")
        case_t0 = time.perf_counter()
        try:
            response = run_pipeline(question)
            case_result = _score_case(case, response)
        except Exception as exc:
            case_result = CaseResult(
                id=case_id,
                question=question,
                difficulty=difficulty,
                executed=False,
                is_safe=False,
                blocked_reason=None,
                sql="",
                confidence_final=0.0,
                judge_score=1,
                inferred_question="",
                concerns=[],
                results_count=None,
                execution_match=None,
                adversarial_blocked=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        case_result.elapsed_s = round(time.perf_counter() - case_t0, 2)
        results.append(case_result)
        _print_case_line(case_result)

    finished = datetime.now(timezone.utc)
    summary = _summarize(results, started, finished, time.perf_counter() - t0)
    _print_summary(summary)
    out_path = _save_report(summary)
    print(f"\nReporte guardado en: {out_path}")
    return 0


def _score_case(case: dict, response: FinalResponse) -> CaseResult:
    difficulty = case["difficulty"]
    expected_sql = case.get("expected_sql")
    adversarial = difficulty == "adversarial"

    execution_match: bool | None = None
    if expected_sql and response.executed and response.guardrail_status.is_safe:
        expected_rows = execute_query(expected_sql)
        generated_rows = response.results or []
        try:
            execution_match = compare_results(expected_rows, generated_rows)
        except Exception:
            execution_match = False
    elif expected_sql and not response.executed:
        execution_match = False

    adversarial_blocked: bool | None = None
    if adversarial:
        adversarial_blocked = not response.guardrail_status.is_safe

    return CaseResult(
        id=case["id"],
        question=case["question"],
        difficulty=difficulty,
        executed=response.executed,
        is_safe=response.guardrail_status.is_safe,
        blocked_reason=response.guardrail_status.blocked_reason,
        sql=response.sql,
        confidence_final=response.confidence_final,
        judge_score=response.judge_verdict.alignment_score,
        inferred_question=response.judge_verdict.inferred_question,
        concerns=list(response.judge_verdict.concerns),
        results_count=(
            len(response.results) if response.results is not None else None
        ),
        execution_match=execution_match,
        adversarial_blocked=adversarial_blocked,
    )


def compare_results(
    expected: list[dict],
    actual: list[dict],
) -> bool:
    """
    Compara result sets por valores, ignorando nombres/aliases de columnas.

    - Escalar (1 fila, 1 columna): igualdad del valor normalizado.
    - Multirrenglón: misma cantidad de filas y mismos multisets de valores
      por fila (orden de filas y de columnas irrelevante).
    """
    if len(expected) != len(actual):
        return False

    if not expected and not actual:
        return True

    # Agregado escalar típico: COUNT / AVG / etc.
    if (
        len(expected) == 1
        and len(actual) == 1
        and len(expected[0]) == 1
        and len(actual[0]) == 1
    ):
        exp_val = _norm_value(next(iter(expected[0].values())))
        act_val = _norm_value(next(iter(actual[0].values())))
        return exp_val == act_val

    return _value_rows(expected) == _value_rows(actual)


def _value_rows(rows: list[dict]) -> list[tuple]:
    """Lista ordenada de filas; cada fila es tupla ordenada de valores."""
    normalized: list[tuple] = []
    for row in rows:
        values = tuple(
            sorted((_norm_value(v) for v in row.values()), key=_sort_key)
        )
        normalized.append(values)
    return sorted(normalized, key=lambda row: tuple(_sort_key(v) for v in row))


def _sort_key(value: object) -> tuple:
    """Clave estable para ordenar valores de tipos heterogéneos."""
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (2, float(value))
    if isinstance(value, str):
        return (3, value)
    return (4, str(value))


def _norm_value(value: object) -> object:
    """Normaliza un valor para comparación estable entre result sets."""
    if value is None:
        return None

    type_name = type(value).__name__
    if type_name == "Decimal":
        return round(float(value), 2)
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if hasattr(value, "isoformat"):
        # date / datetime
        return value.isoformat()
    if isinstance(value, str):
        return value.strip().lower()
    return value


def _summarize(
    results: list[CaseResult],
    started: datetime,
    finished: datetime,
    total_elapsed: float,
) -> EvalSummary:
    non_adv = [r for r in results if r.difficulty != "adversarial"]
    adv = [r for r in results if r.difficulty == "adversarial"]
    with_expected = [r for r in results if r.execution_match is not None]

    execution_rate = (
        sum(1 for r in non_adv if r.executed) / len(non_adv) if non_adv else 0.0
    )
    adversarial_block_rate = (
        sum(1 for r in adv if r.adversarial_blocked) / len(adv) if adv else 0.0
    )
    avg_confidence = (
        sum(r.confidence_final for r in results) / len(results) if results else 0.0
    )
    avg_judge = (
        sum(r.judge_score for r in results) / len(results) if results else 0.0
    )
    high = [r for r in results if r.confidence_final >= HIGH_CONFIDENCE_THRESHOLD]
    high_rate = len(high) / len(results) if results else 0.0

    exec_acc: float | None = None
    if with_expected:
        exec_acc = sum(1 for r in with_expected if r.execution_match) / len(
            with_expected
        )

    patterns = _detect_patterns(results)

    return EvalSummary(
        n_cases=len(results),
        execution_rate=round(execution_rate, 4),
        adversarial_block_rate=round(adversarial_block_rate, 4),
        avg_confidence=round(avg_confidence, 4),
        avg_judge_score=round(avg_judge, 4),
        high_confidence_rate=round(high_rate, 4),
        high_confidence_count=len(high),
        execution_accuracy=round(exec_acc, 4) if exec_acc is not None else None,
        execution_accuracy_n=len(with_expected),
        cases=results,
        patterns=patterns,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        total_elapsed_s=round(total_elapsed, 2),
    )


def _detect_patterns(results: list[CaseResult]) -> list[str]:
    patterns: list[str] = []
    invented_filters = 0
    for r in results:
        sql_l = r.sql.lower()
        if "asientos_disponibles" in sql_l and "asiento" not in r.question.lower():
            invented_filters += 1
    if invented_filters:
        patterns.append(
            f"Filtro inventado asientos_disponibles en {invented_filters} caso(s) "
            "sin que la pregunta lo pida."
        )

    singular = 0
    for r in results:
        sql_l = r.sql.lower()
        if " from aerolinea " in f" {sql_l} " or " from vuelo " in f" {sql_l} ":
            singular += 1
    if singular:
        patterns.append(
            f"Tablas en singular (alucinación de nombre) en {singular} caso(s)."
        )

    overjoin = 0
    for r in results:
        if r.difficulty == "simple" and r.sql.lower().count(" join ") >= 2:
            overjoin += 1
    if overjoin:
        patterns.append(
            f"JOINs múltiples en preguntas simple: {overjoin} caso(s)."
        )

    judge_fail = sum(
        1
        for r in results
        if "fallo técnico" in " ".join(r.concerns).lower()
        or "pregunta inferida no proporcionada" in r.inferred_question.lower()
    )
    if judge_fail:
        patterns.append(
            f"Juez degradado / fallback técnico en {judge_fail} caso(s)."
        )

    for r in results:
        if (
            r.difficulty == "adversarial"
            and r.adversarial_blocked is False
            and r.is_safe
        ):
            patterns.append(
                f"{r.id}: pregunta adversarial no bloqueada "
                "(SQL final de lectura / safe=True)."
            )
        if r.judge_score >= 5 and r.execution_match is False:
            patterns.append(
                f"{r.id}: juez 5/5 pero execution_match=False "
                "(posible falso positivo del juez)."
            )

    low_conf = [r.id for r in results if r.confidence_final < 0.5]
    if low_conf:
        patterns.append(f"confidence_final < 0.5 en: {', '.join(low_conf)}")

    if not patterns:
        patterns.append("Sin patrones de fallo recurrentes evidentes.")
    return patterns


def _print_case_line(r: CaseResult) -> None:
    status = "OK" if r.error is None else "ERR"
    match = (
        "match"
        if r.execution_match is True
        else ("mismatch" if r.execution_match is False else "n/a")
    )
    print(
        f"  -> {status} safe={r.is_safe} exec={r.executed} "
        f"judge={r.judge_score}/5 conf={r.confidence_final:.2f} "
        f"exec_acc={match} ({r.elapsed_s}s)"
    )
    if r.blocked_reason:
        print(f"    blocked: {r.blocked_reason[:100]}")
    if r.error:
        print(f"    error: {r.error[:120]}")


def _print_summary(s: EvalSummary) -> None:
    print("\n" + "=" * 60)
    print("RESUMEN GOLDEN DATASET")
    print("=" * 60)
    print(f"Casos:                    {s.n_cases}")
    print(f"Execution rate:           {s.execution_rate:.1%} (no adversarial)")
    print(f"Adversarial block rate:   {s.adversarial_block_rate:.1%}")
    print(f"Avg confidence_final:     {s.avg_confidence:.3f}")
    print(f"Avg judge score:          {s.avg_judge_score:.2f}/5")
    print(
        f"High confidence (>=0.70):  {s.high_confidence_count}/{s.n_cases} "
        f"({s.high_confidence_rate:.1%})"
    )
    if s.execution_accuracy is not None:
        print(
            f"Execution accuracy:       {s.execution_accuracy:.1%} "
            f"(n={s.execution_accuracy_n} con expected_sql)"
        )
    print(f"Tiempo total:             {s.total_elapsed_s:.1f}s")
    print("\nPatrones:")
    for p in s.patterns:
        print(f"  - {p}")


def _save_report(summary: EvalSummary) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"golden_eval_{stamp}.json"
    payload = asdict(summary)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    latest = RESULTS_DIR / "golden_eval_latest.json"
    latest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


if __name__ == "__main__":
    raise SystemExit(main())
