"""
main.py

Punto de entrada CLI del MVP. Solo orquesta (SPECS.md §9):
  python main.py "¿Cuáles son los 5 vuelos más baratos?"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Permite `python src/main.py` y el launcher de la raíz.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pipeline import FinalResponse, run_pipeline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Text-to-SQL Vuelos: pregunta en lenguaje natural → SQL.",
    )
    parser.add_argument(
        "question",
        nargs="?",
        help="Pregunta en español o inglés",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Ruta a la base DuckDB (default: DUCKDB_PATH / data/vuelos.duckdb)",
    )
    args = parser.parse_args(argv)

    if not args.question or not args.question.strip():
        parser.error("Pasá una pregunta, p.ej. python main.py \"¿Cuántos vuelos hay?\"")

    response = run_pipeline(args.question.strip(), db_path=args.db)
    print(_format_response(response))
    return 0


def _format_response(response: FinalResponse) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("PREGUNTA")
    lines.append(response.question)
    lines.append("")
    lines.append("SQL")
    lines.append(response.sql or "(vacío)")
    lines.append("")
    lines.append("GUARDRAILS")
    lines.append(f"  seguro: {response.guardrail_status.is_safe}")
    lines.append(f"  tipo:   {response.guardrail_status.query_type}")
    if response.guardrail_status.blocked_reason:
        lines.append(f"  motivo: {response.guardrail_status.blocked_reason}")
    lines.append("")
    lines.append("JUEZ")
    lines.append(f"  score:    {response.judge_verdict.alignment_score}/5")
    lines.append(f"  inferida: {response.judge_verdict.inferred_question}")
    if response.judge_verdict.concerns:
        lines.append("  concerns:")
        for concern in response.judge_verdict.concerns:
            lines.append(f"    - {concern}")
    lines.append("")
    lines.append(
        f"CONFIDENCE FINAL: {response.confidence_final:.2f}  |  "
        f"ejecutado: {response.executed}"
    )
    lines.append("")
    lines.append("RESULTADOS")
    if not response.executed:
        lines.append("  (no ejecutado)")
    elif not response.results:
        lines.append("  (sin filas o error de ejecución)")
    else:
        preview = response.results[:20]
        lines.append(json.dumps(preview, ensure_ascii=False, indent=2))
        if len(response.results) > 20:
            lines.append(f"  … ({len(response.results) - 20} filas más)")
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
