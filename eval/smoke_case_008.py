"""Smoke case_008: pipeline real + compare vs expected_sql."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from evaluate import compare_results  # noqa: E402
from executor import execute_query  # noqa: E402
from pipeline import run_pipeline  # noqa: E402

CASE_ID = "case_008"


def main() -> int:
    cases = json.loads((ROOT / "eval" / "golden_dataset.json").read_text(encoding="utf-8"))
    case = next(c for c in cases if c["id"] == CASE_ID)
    question = case["question"]
    expected_sql = case["expected_sql"]

    print(f"SMOKE {CASE_ID}")
    print(f"Q: {question}")
    t0 = time.perf_counter()
    response = run_pipeline(question)
    elapsed = time.perf_counter() - t0

    print(f"elapsed_s: {elapsed:.1f}")
    print(f"safe: {response.guardrail_status.is_safe}")
    print(f"executed: {response.executed}")
    print(f"sql: {response.sql}")
    if response.execution_error:
        print(f"execution_error: {response.execution_error}")
    print(f"ILIKE in sql: {'ILIKE' in (response.sql or '').upper()}")

    if not response.executed or response.results is None:
        print("RESULT: FAIL (no execution)")
        return 1

    expected = execute_query(expected_sql)
    if expected.error is not None:
        print(f"RESULT: FAIL (expected_sql error: {expected.error})")
        return 1

    match = compare_results(expected.rows, response.results)
    print(f"rows_generated: {len(response.results)}")
    print(f"rows_expected: {len(expected.rows)}")
    print(f"execution_match: {match}")
    print("RESULT:", "PASS" if match else "MISS")
    return 0 if match else 2


if __name__ == "__main__":
    raise SystemExit(main())
