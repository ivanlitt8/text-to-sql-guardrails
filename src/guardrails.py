"""
guardrails.py

Capa de seguridad determinística (SIN LLM). Ver docs/SPECS.md sección 7
para la lista completa de reglas y sección 6 para GuardrailResult.
"""

from __future__ import annotations

import os
import re

import sqlparse
from dotenv import load_dotenv
from pydantic import BaseModel
from sqlparse.tokens import Keyword, Name

BLOCKED_KEYWORDS = [
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
]

# v1: más de 3 SELECT → rechazo (SPECS §7, conteo simple de keywords).
_MAX_SELECT_KEYWORDS = 3

_TABLE_LEAD_KEYWORDS = frozenset(
    {
        "FROM",
        "INTO",
        "UPDATE",
        "TABLE",
        "JOIN",
        "INNER JOIN",
        "LEFT JOIN",
        "RIGHT JOIN",
        "FULL JOIN",
        "CROSS JOIN",
        "LEFT OUTER JOIN",
        "RIGHT OUTER JOIN",
        "FULL OUTER JOIN",
    }
)

_SQL_KEYWORD_NAMES = frozenset(
    {
        "SELECT",
        "FROM",
        "WHERE",
        "JOIN",
        "LEFT",
        "RIGHT",
        "INNER",
        "OUTER",
        "FULL",
        "CROSS",
        "ON",
        "AS",
        "AND",
        "OR",
        "NOT",
        "IN",
        "IS",
        "NULL",
        "LIKE",
        "ILIKE",
        "BETWEEN",
        "GROUP",
        "ORDER",
        "BY",
        "HAVING",
        "LIMIT",
        "OFFSET",
        "UNION",
        "ALL",
        "DISTINCT",
        "WITH",
        "ASC",
        "DESC",
    }
)


class GuardrailResult(BaseModel):
    is_safe: bool
    blocked_reason: str | None
    query_type: str
    sanitized_sql: str | None = None


def validate_sql_guardrails(
    sql: str,
    schema_tables: list[str],
) -> GuardrailResult:
    """
    Aplica las reglas determinísticas de SPECS.md §7 sobre `sql`.

    Args:
        sql: consulta a validar.
        schema_tables: nombres de tablas permitidas (schema real).

    Returns:
        GuardrailResult con is_safe, motivo de bloqueo si aplica,
        query_type y sanitized_sql (con LIMIT si hacía falta) cuando es segura.
    """
    query_type = _detect_query_type(sql)

    blocked = _find_blocked_keyword(sql)
    if blocked is not None:
        return GuardrailResult(
            is_safe=False,
            blocked_reason=(
                f"Operación prohibida detectada: {blocked}. "
                "Solo se permiten consultas de lectura (SELECT)."
            ),
            query_type=query_type if query_type != "UNKNOWN" else blocked,
            sanitized_sql=None,
        )

    if query_type != "SELECT":
        return GuardrailResult(
            is_safe=False,
            blocked_reason=(
                f"Tipo de consulta no permitido: {query_type}. "
                "Solo se permiten SELECT."
            ),
            query_type=query_type,
            sanitized_sql=None,
        )

    select_count = _count_select_keywords(sql)
    if select_count > _MAX_SELECT_KEYWORDS:
        return GuardrailResult(
            is_safe=False,
            blocked_reason=(
                f"Demasiados SELECT anidados ({select_count} > "
                f"{_MAX_SELECT_KEYWORDS}). "
                "Simplificá la consulta o dividila en pasos."
            ),
            query_type=query_type,
            sanitized_sql=None,
        )

    unknown = _unknown_tables(sql, schema_tables)
    if unknown:
        return GuardrailResult(
            is_safe=False,
            blocked_reason=(
                "Tablas no presentes en el schema: "
                + ", ".join(unknown)
            ),
            query_type=query_type,
            sanitized_sql=None,
        )

    sanitized = enforce_limit(sql)
    return GuardrailResult(
        is_safe=True,
        blocked_reason=None,
        query_type=query_type,
        sanitized_sql=sanitized,
    )


def enforce_limit(sql: str, limit: int | None = None) -> str:
    """
    Si el SQL no tiene LIMIT, agrega `LIMIT {limit}` al final.
    El default sale de DEFAULT_ROW_LIMIT (env) o 1000.
    """
    if limit is None:
        limit = _default_row_limit()

    if _has_limit(sql):
        return sql.strip().rstrip(";").strip()

    cleaned = sql.strip().rstrip(";").strip()
    return f"{cleaned} LIMIT {limit}"


def check_query(
    sql: str,
    schema_tables: list[str] | None = None,
) -> GuardrailResult:
    """
    Alias del stub original. Preferir validate_sql_guardrails.
    Si schema_tables es None, omite la validación de pertenencia al schema.
    """
    if schema_tables is None:
        return _validate_without_schema_check(sql)
    return validate_sql_guardrails(sql, schema_tables)


def _validate_without_schema_check(sql: str) -> GuardrailResult:
    """Igual que validate_sql_guardrails pero omite el chequeo de tablas."""
    query_type = _detect_query_type(sql)

    blocked = _find_blocked_keyword(sql)
    if blocked is not None:
        return GuardrailResult(
            is_safe=False,
            blocked_reason=(
                f"Operación prohibida detectada: {blocked}. "
                "Solo se permiten consultas de lectura (SELECT)."
            ),
            query_type=query_type if query_type != "UNKNOWN" else blocked,
            sanitized_sql=None,
        )

    if query_type != "SELECT":
        return GuardrailResult(
            is_safe=False,
            blocked_reason=(
                f"Tipo de consulta no permitido: {query_type}. "
                "Solo se permiten SELECT."
            ),
            query_type=query_type,
            sanitized_sql=None,
        )

    select_count = _count_select_keywords(sql)
    if select_count > _MAX_SELECT_KEYWORDS:
        return GuardrailResult(
            is_safe=False,
            blocked_reason=(
                f"Demasiados SELECT anidados ({select_count} > "
                f"{_MAX_SELECT_KEYWORDS}). "
                "Simplificá la consulta o dividila en pasos."
            ),
            query_type=query_type,
            sanitized_sql=None,
        )

    return GuardrailResult(
        is_safe=True,
        blocked_reason=None,
        query_type=query_type,
        sanitized_sql=enforce_limit(sql),
    )

def _default_row_limit() -> int:
    load_dotenv()
    raw = os.getenv("DEFAULT_ROW_LIMIT", "1000")
    try:
        return int(raw)
    except ValueError:
        return 1000


def _detect_query_type(sql: str) -> str:
    statements = sqlparse.parse(sql)
    if not statements or not str(statements[0]).strip():
        return "UNKNOWN"
    stmt_type = statements[0].get_type()
    return stmt_type if stmt_type else "UNKNOWN"


def _flatten_tokens(sql: str) -> list:
    statements = sqlparse.parse(sql)
    if not statements:
        return []
    return [t for t in statements[0].flatten() if not t.is_whitespace]


def _find_blocked_keyword(sql: str) -> str | None:
    """Detecta keywords prohibidas como tokens SQL (no substrings en literales)."""
    for token in _flatten_tokens(sql):
        value = token.value.upper().strip()
        # sqlparse puede emitir "CREATE TABLE" como un solo keyword compuesto.
        for blocked in BLOCKED_KEYWORDS:
            if value == blocked or value.startswith(blocked + " "):
                return blocked
    # Fallback por si el parser no tokeniza algún DDL raro.
    upper = sql.upper()
    for blocked in BLOCKED_KEYWORDS:
        if re.search(rf"\b{blocked}\b", upper):
            return blocked
    return None


def _count_select_keywords(sql: str) -> int:
    count = 0
    for token in _flatten_tokens(sql):
        value = token.value.upper().strip()
        if value == "SELECT" or value.startswith("SELECT "):
            count += 1
    if count == 0 and re.search(r"\bSELECT\b", sql, flags=re.IGNORECASE):
        return len(re.findall(r"\bSELECT\b", sql, flags=re.IGNORECASE))
    return count


def _has_limit(sql: str) -> bool:
    for token in _flatten_tokens(sql):
        if token.value.upper().strip() == "LIMIT":
            return True
    return bool(re.search(r"\bLIMIT\b", sql, flags=re.IGNORECASE))


def _is_name_token(token) -> bool:
    return token.ttype in Name or (
        token.ttype is not None and str(token.ttype).startswith("Token.Name")
    )


def _is_table_lead(value_upper: str) -> bool:
    return value_upper in _TABLE_LEAD_KEYWORDS or value_upper.endswith("JOIN")


def _extract_referenced_tables(sql: str) -> list[str]:
    """Tablas tras FROM / JOIN / INTO / UPDATE / TABLE (determinístico)."""
    tokens = _flatten_tokens(sql)
    tables: list[str] = []
    seen: set[str] = set()
    stop_after = {
        "ON",
        "WHERE",
        "GROUP",
        "ORDER",
        "LIMIT",
        "HAVING",
        "SET",
        "UNION",
        "EXCEPT",
        "INTERSECT",
        ",",
        ";",
    }

    i = 0
    while i < len(tokens):
        value_upper = tokens[i].value.upper()
        if not _is_table_lead(value_upper):
            i += 1
            continue

        i += 1
        if i >= len(tokens) or tokens[i].value == "(":
            continue

        raw_name = tokens[i].value.strip("`\"[]")
        # sqlparse suele partir schema.tabla en Name '.' Name
        if (
            i + 2 < len(tokens)
            and tokens[i + 1].value == "."
            and _is_name_token(tokens[i + 2])
        ):
            raw_name = (
                f"{raw_name}.{tokens[i + 2].value.strip('`\"[]')}"
            )
            i += 2

        # Prefijos inventados (p.ej. sqlite.vuelos) no están en el schema.
        if "." in raw_name:
            catalog, base = raw_name.rsplit(".", 1)
            if catalog.lower() in {"main", "public"}:
                candidate = base
            else:
                candidate = raw_name
        else:
            candidate = raw_name

        key = candidate.lower()
        base_for_keyword = candidate.split(".")[-1]
        if (
            candidate
            and key not in seen
            and base_for_keyword.upper() not in _SQL_KEYWORD_NAMES
        ):
            seen.add(key)
            tables.append(candidate)

        i += 1
        if i < len(tokens) and tokens[i].value.upper() == "AS":
            i += 2
        elif i < len(tokens):
            nxt = tokens[i]
            nxt_upper = nxt.value.upper()
            if (
                not _is_table_lead(nxt_upper)
                and nxt_upper not in stop_after
                and nxt.ttype is not Keyword
                and _is_name_token(nxt)
            ):
                i += 1

    return tables


def _unknown_tables(sql: str, schema_tables: list[str]) -> list[str]:
    allowed = {t.lower() for t in schema_tables}
    unknown: list[str] = []
    seen: set[str] = set()
    for table in _extract_referenced_tables(sql):
        key = table.lower()
        if key not in allowed and key not in seen:
            seen.add(key)
            unknown.append(table)
    return unknown
