"""
eval/evaluate.py

Evalúa el pipeline end-to-end contra eval/golden_dataset.json.

Métricas:
  - execution_rate: % de casos no-adversariales con executed=True
  - adversarial_block_rate: % de adversarial bloqueados por guardrails
  - avg_confidence: excluye casos con adversarial_blocked=True
  - avg_judge_score: excluye adversarial_blocked=True y judge_degraded=True
    (fallbacks técnicos del juez); ver judge_fallback_rate /
    degraded_judge_ids en el reporte JSON
  - high_confidence_rate: % con confidence_final >= 0.70
  - execution_accuracy: % de casos con expected_sql donde el result set
    del SQL generado coincide con el del expected_sql (si ambos corren)

Paralelización:
  Los casos se ejecutan con ThreadPoolExecutor. El número de workers se
  lee de EVAL_MAX_WORKERS (o MAX_WORKERS); default 2. Usar 1 para
  comportamiento secuencial (baseline).

  En el servidor Ollama se recomienda OLLAMA_MAX_LOADED_MODELS=2 y
  OLLAMA_NUM_PARALLEL=2 para evitar swap de modelos (sqlcoder / qwen)
  en VRAM cuando hay workers concurrentes.

  OLLAMA_TIMEOUT (segundos, default 900) aplica a generador y juez para
  que un cuelgue de Ollama no deje el eval bloqueado sin fin. No usar
  valores < 530 si se quiere cubrir los casos más lentos del golden.

Uso (desde la raíz del repo, con .venv activo y Ollama levantado):
  python eval/evaluate.py
  EVAL_MAX_WORKERS=1 python eval/evaluate.py   # secuencial
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
DEFAULT_MAX_WORKERS = 2

# Serializa prints de casos concurrentes para que no se intercalen.
_PRINT_LOCK = threading.Lock()


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
    judge_degraded: bool = False
    judge_reasoning: str = ""
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
    # Auditoría: avg_confidence excluye bloqueos adversariales correctos
    avg_metrics_excluded_count: int = 0
    avg_metrics_excluded_ids: list[str] = field(default_factory=list)
    avg_metrics_exclusion_reason: str = ""
    # Fallbacks técnicos del juez (no bajan avg_judge_score)
    judge_fallback_rate: float = 0.0
    degraded_judge_ids: list[str] = field(default_factory=list)
    cases: list[CaseResult] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    total_elapsed_s: float = 0.0


def _resolve_max_workers() -> int:
    """
    Lee EVAL_MAX_WORKERS o MAX_WORKERS del entorno.
    Default 2; mínimo 1. Valores inválidos caen al default.
    """
    raw = os.getenv("EVAL_MAX_WORKERS") or os.getenv("MAX_WORKERS")
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_WORKERS
    try:
        value = int(str(raw).strip())
    except ValueError:
        return DEFAULT_MAX_WORKERS
    return max(1, value)


def _run_one_case(case: dict) -> CaseResult:
    """
    Ejecuta un caso del golden de punta a punta.

    Aísla excepciones inesperadas en un CaseResult con error, para que
    un fallo individual no interrumpa al resto del pool.
    """
    case_id = case["id"]
    question = case["question"]
    difficulty = case["difficulty"]
    with _PRINT_LOCK:
        print(
            f"[{case_id}] start {difficulty}: {question[:70]}…",
            flush=True,
        )
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
            judge_degraded=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    case_result.elapsed_s = round(time.perf_counter() - case_t0, 2)
    return case_result


def main() -> int:
    cases = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    max_workers = _resolve_max_workers()
    # Buffer indexado: preserva el orden del golden sin race conditions.
    results: list[CaseResult | None] = [None] * len(cases)

    print(f"Golden dataset: {len(cases)} casos (workers={max_workers})")
    print(
        "Nota Ollama: OLLAMA_MAX_LOADED_MODELS=2 y OLLAMA_NUM_PARALLEL=2 "
        "recomendados para evitar swap de modelos en VRAM. "
        "OLLAMA_TIMEOUT default 900s (casos lentos del golden ~530s; "
        "timeouts duros del generador pedían más techo)."
    )
    print("-" * 60)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(_run_one_case, case): i
            for i, case in enumerate(cases)
        }
        for future in as_completed(future_to_index):
            i = future_to_index[future]
            case = cases[i]
            case_id = case["id"]
            difficulty = case["difficulty"]
            question = case["question"]
            try:
                case_result = future.result()
            except Exception as exc:
                # Defensa extra: el worker no debería propagar, pero
                # garantizamos que el pool no se detenga.
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
                    judge_degraded=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results[i] = case_result
            with _PRINT_LOCK:
                print(f"[{case_id}] done {difficulty}: {question[:70]}…", flush=True)
                _print_case_line(case_result)

    assert all(r is not None for r in results)
    ordered: list[CaseResult] = [r for r in results if r is not None]

    finished = datetime.now(timezone.utc)
    summary = _summarize(ordered, started, finished, time.perf_counter() - t0)
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
        expected_exec = execute_query(expected_sql)
        expected_rows = expected_exec.rows if expected_exec.error is None else []
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
        judge_degraded=bool(response.judge_verdict.is_degraded),
        judge_reasoning=response.judge_verdict.reasoning or "",
        error=response.execution_error,
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

    # Bloqueos adversariales correctos no deben bajar avg_confidence.
    excluded_for_conf = [r for r in results if r.adversarial_blocked is True]
    scored_for_conf = [r for r in results if r.adversarial_blocked is not True]
    avg_confidence = (
        sum(r.confidence_final for r in scored_for_conf) / len(scored_for_conf)
        if scored_for_conf
        else 0.0
    )

    # avg_judge_score: excluir adversarial_blocked y fallbacks técnicos del juez.
    degraded = [r for r in results if r.judge_degraded]
    scored_for_judge = [
        r
        for r in results
        if r.adversarial_blocked is not True and not r.judge_degraded
    ]
    avg_judge = (
        sum(r.judge_score for r in scored_for_judge) / len(scored_for_judge)
        if scored_for_judge
        else 0.0
    )
    judge_fallback_rate = (
        len(degraded) / len(results) if results else 0.0
    )

    high = [r for r in results if r.confidence_final >= HIGH_CONFIDENCE_THRESHOLD]
    high_rate = len(high) / len(results) if results else 0.0

    exec_acc: float | None = None
    if with_expected:
        exec_acc = sum(1 for r in with_expected if r.execution_match) / len(
            with_expected
        )

    patterns = _detect_patterns(results)

    exclusion_bits: list[str] = []
    if excluded_for_conf:
        exclusion_bits.append(
            "avg_confidence excluye adversarial_blocked=True "
            f"({', '.join(r.id for r in excluded_for_conf)})."
        )
    exclusion_bits.append(
        "avg_judge_score excluye adversarial_blocked=True y "
        "judge_degraded=True (fallbacks técnicos del juez)."
    )
    if degraded:
        exclusion_bits.append(
            f"degraded_judge_ids: {', '.join(r.id for r in degraded)}."
        )

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
        avg_metrics_excluded_count=len(excluded_for_conf),
        avg_metrics_excluded_ids=[r.id for r in excluded_for_conf],
        avg_metrics_exclusion_reason=(
            " ".join(exclusion_bits)
            if exclusion_bits
            else (
                "Sin exclusiones: avg_confidence sobre no-adversariales; "
                "avg_judge_score sobre no-adversariales y no-degradados."
            )
        ),
        judge_fallback_rate=round(judge_fallback_rate, 4),
        degraded_judge_ids=[r.id for r in degraded],
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

    judge_fail = sum(1 for r in results if r.judge_degraded)
    if judge_fail:
        patterns.append(
            f"Juez degradado / fallback técnico en {judge_fail} caso(s): "
            + ", ".join(r.id for r in results if r.judge_degraded)
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

    low_conf_expected = [
        r.id
        for r in results
        if r.confidence_final < 0.5 and r.adversarial_blocked is True
    ]
    low_conf_real = [
        r.id
        for r in results
        if r.confidence_final < 0.5 and r.adversarial_blocked is not True
    ]
    if low_conf_expected:
        patterns.append(
            "confianza baja por bloqueo correcto (esperado): "
            + ", ".join(low_conf_expected)
        )
    if low_conf_real:
        patterns.append(
            "confianza baja por falla real (revisar): " + ", ".join(low_conf_real)
        )

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
    if s.avg_metrics_excluded_count:
        print(
            f"  (excluidos de avg conf: {s.avg_metrics_excluded_count} — "
            f"{', '.join(s.avg_metrics_excluded_ids)})"
        )
    print(f"Judge fallback rate:      {s.judge_fallback_rate:.1%}")
    if s.degraded_judge_ids:
        print(f"  (degraded: {', '.join(s.degraded_judge_ids)})")
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
