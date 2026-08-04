"""
sql_generator.py

Agente 1: Generador de SQL. Ver docs/SPECS.md sección 3.

Modelo: sqlcoder (configurable vía GENERATOR_MODEL), vía Ollama, local.
El modelo solo genera SQL en texto libre (prompt Defog). El contrato
SQLGenerationResult (SPECS.md §6) se ensambla en Python de forma
determinística: no se usa instructor en esta llamada.
"""

from __future__ import annotations

import os
import re

import ollama
import sqlparse
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from sqlparse.tokens import Keyword, Name


class SQLGenerationError(Exception):
    """Fallo al generar o validar SQL (llamada al modelo o sintaxis)."""


class SQLGenerationResult(BaseModel):
    sql: str
    explanation: str
    tables_used: list[str]
    columns_used: list[str]
    confidence_self_reported: int = Field(ge=1, le=5)  # 1-5


# TODO: este valor no refleja una autoevaluación real del modelo;
# sqlcoder no fue entrenado para reportar confidence. Placeholder de MVP.
_DEFAULT_CONFIDENCE = 3

_CONTROL_TOKEN_RE = re.compile(r"<\|[^|>]+\|>")

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
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "WITH",
        "ASC",
        "DESC",
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "CAST",
        "TRUE",
        "FALSE",
    }
)


def build_defog_prompt(question: str, schema: str) -> str:
    """
    Arma el prompt en formato Defog para sqlcoder:
    ### Task / ### Database Schema / ### Answer + fence sql abierto.

    Usa few-shot concreto (en lugar de reglas en prosa largas) para
    anclar agregaciones, JOINs y evitar tablas inventadas.
    """
    return (
        "### Task\n"
        "Generate a SQL query to answer the following question.\n"
        "Use ONLY exact table/column names from the schema. "
        "Do not invent tables. Do not add filters the question does not ask.\n\n"
        "Question:\n"
        f"{question}\n\n"
        "### Examples\n"
        "Q: ¿Cuál es el precio promedio de los vuelos por aerolínea?\n"
        "A: SELECT a.nombre AS aerolinea, AVG(v.precio) AS precio_promedio "
        "FROM vuelos v JOIN aerolineas a ON v.aerolinea_id = a.id "
        "GROUP BY a.nombre\n\n"
        "Q: ¿Cuál es el origen y destino con más vuelos?\n"
        "A: SELECT origen, destino, COUNT(*) AS total_vuelos FROM vuelos "
        "GROUP BY origen, destino ORDER BY total_vuelos DESC LIMIT 1\n\n"
        "Q: ¿Qué pasajeros tienen reservas canceladas?\n"
        "A: SELECT p.nombre, r.precio_pagado FROM reservas r "
        "JOIN pasajeros p ON r.pasajero_id = p.id "
        "WHERE r.estado = 'cancelada'\n\n"
        "### Database Schema\n"
        f"{schema}\n\n"
        "### Answer\n"
        "```sql\n"
    )


def generate_sql(question: str, schema: str) -> SQLGenerationResult:
    """
    Genera una consulta SQL a partir de una pregunta en lenguaje natural
    y el schema relevante de la base de datos (DDL de extract_schema()).

    Pipeline:
      1) Ollama + prompt Defog → texto libre (SQL)
      2) Normalizar y validar con sqlparse
      3) Ensamblar SQLGenerationResult de forma determinística

    Raises:
        SQLGenerationError: si falla la llamada al modelo o el SQL no es
            parseable con sqlparse.
    """
    prompt = build_defog_prompt(question, schema)

    try:
        raw_text = _call_ollama(prompt)
    except SQLGenerationError:
        raise
    except Exception as exc:
        raise SQLGenerationError(
            f"Fallo al llamar al modelo generador: {exc}"
        ) from exc

    normalized_sql = _extract_sql_from_response(raw_text)
    _validate_sql_syntax(normalized_sql)

    tables_used = _extract_tables_used(normalized_sql)
    columns_used = _extract_columns_used(normalized_sql)
    explanation = _build_explanation(normalized_sql, tables_used, columns_used)

    return SQLGenerationResult(
        sql=normalized_sql,
        explanation=explanation,
        tables_used=tables_used,
        columns_used=columns_used,
        confidence_self_reported=_DEFAULT_CONFIDENCE,
    )


def _call_ollama(prompt: str) -> str:
    """
    Invoca Ollama en modo completion (texto libre) con temperature=0.
    No usa instructor: sqlcoder completa SQL, no JSON.
    """
    load_dotenv()
    model = os.getenv("GENERATOR_MODEL", "sqlcoder")
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

    client = ollama.Client(host=host)
    response = client.generate(
        model=model,
        prompt=prompt,
        options={"temperature": 0},
    )
    text = getattr(response, "response", None) or response.get("response", "")
    if not isinstance(text, str):
        raise SQLGenerationError(
            f"Respuesta inesperada de Ollama (tipo {type(text)!r})"
        )
    return text


def _extract_sql_from_response(raw: str) -> str:
    """Limpia tokens de control, fences markdown y prosa previa al SQL."""
    text = _CONTROL_TOKEN_RE.sub("", raw).strip()
    text = _normalize_sql(text)
    if text.endswith("```"):
        text = text[: text.rfind("```")].rstrip()

    if not text:
        return text

    upper = text.upper()
    for keyword in ("WITH ", "SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        idx = upper.find(keyword)
        if idx != -1:
            return text[idx:].strip()
    return text


def _normalize_sql(sql: str) -> str:
    """Quita fences de markdown si el modelo los incluyó en la respuesta."""
    text = sql.strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    lines = lines[1:]  # descarta ``` o ```sql
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _validate_sql_syntax(sql: str) -> None:
    """Valida que el texto sea SQL parseable; si no, levanta SQLGenerationError."""
    if not sql or not sql.strip():
        raise SQLGenerationError("El modelo devolvió SQL vacío")

    try:
        statements = sqlparse.parse(sql)
    except Exception as exc:
        raise SQLGenerationError(
            f"Error al parsear SQL con sqlparse: {exc}"
        ) from exc

    if not statements:
        raise SQLGenerationError(f"SQL no parseable (sin statements): {sql!r}")

    meaningful = [s for s in statements if str(s).strip()]
    if not meaningful:
        raise SQLGenerationError(f"SQL no parseable (solo whitespace): {sql!r}")

    stmt_type = meaningful[0].get_type()
    if stmt_type == "UNKNOWN":
        raise SQLGenerationError(
            f"SQL con sintaxis no reconocida (tipo UNKNOWN): {sql!r}"
        )


def _flatten_tokens(sql: str) -> list:
    statements = sqlparse.parse(sql)
    if not statements:
        return []
    return [t for t in statements[0].flatten() if not t.is_whitespace]


def _is_name_token(token) -> bool:
    return token.ttype in Name or (
        token.ttype is not None and str(token.ttype).startswith("Token.Name")
    )


def _is_table_lead(value_upper: str) -> bool:
    return value_upper in _TABLE_LEAD_KEYWORDS or value_upper.endswith("JOIN")


def _extract_tables_and_aliases(sql: str) -> tuple[list[str], set[str]]:
    """Devuelve (tablas en orden, set de aliases en minúsculas)."""
    tokens = _flatten_tokens(sql)
    tables: list[str] = []
    seen: set[str] = set()
    aliases: set[str] = set()
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
        base = raw_name.split(".")[-1]
        key = base.lower()
        if base and key not in seen and base.upper() not in _SQL_KEYWORD_NAMES:
            seen.add(key)
            tables.append(base)

        i += 1
        if i < len(tokens) and tokens[i].value.upper() == "AS":
            i += 1
            if i < len(tokens) and _is_name_token(tokens[i]):
                aliases.add(tokens[i].value.strip("`\"[]").lower())
                i += 1
        elif i < len(tokens):
            nxt = tokens[i]
            nxt_upper = nxt.value.upper()
            if (
                not _is_table_lead(nxt_upper)
                and nxt_upper not in stop_after
                and nxt.ttype is not Keyword
                and _is_name_token(nxt)
            ):
                aliases.add(nxt.value.strip("`\"[]").lower())
                i += 1

    return tables, aliases


def _extract_tables_used(sql: str) -> list[str]:
    """
    Deriva tablas referenciadas (tras FROM / JOIN / INTO / UPDATE / TABLE)
    de forma determinística con sqlparse.
    """
    tables, _aliases = _extract_tables_and_aliases(sql)
    return tables


def _extract_columns_used(sql: str) -> list[str]:
    """
    Deriva columnas referenciadas en SELECT / WHERE / GROUP BY / ORDER BY /
    HAVING / ON de forma determinística con sqlparse.
    """
    tokens = _flatten_tokens(sql)
    if not tokens:
        return []

    tables, aliases = _extract_tables_and_aliases(sql)
    excluded = {t.lower() for t in tables} | aliases

    columns: list[str] = []
    seen: set[str] = set()

    # Zonas donde esperamos columnas (no nombres de tabla del FROM).
    in_column_zone = False
    i = 0
    while i < len(tokens):
        value_upper = tokens[i].value.upper()

        if value_upper == "SELECT":
            in_column_zone = True
            i += 1
            continue
        if value_upper in {"WHERE", "ON", "HAVING"} or value_upper in {
            "GROUP BY",
            "ORDER BY",
        }:
            in_column_zone = True
            i += 1
            continue
        if _is_table_lead(value_upper) or value_upper in {
            "FROM",
            "LIMIT",
            "OFFSET",
            "UNION",
            "SET",
        }:
            in_column_zone = False
            i += 1
            continue

        if not in_column_zone:
            i += 1
            continue

        # alias.columna
        if (
            i + 2 < len(tokens)
            and _is_name_token(tokens[i])
            and tokens[i + 1].value == "."
            and _is_name_token(tokens[i + 2])
        ):
            col = tokens[i + 2].value.strip("`\"[]")
            key = col.lower()
            if key not in seen and col.upper() not in _SQL_KEYWORD_NAMES:
                seen.add(key)
                columns.append(col)
            i += 3
            continue

        if _is_name_token(tokens[i]):
            raw = tokens[i].value.strip("`\"[]")
            key = raw.lower()
            # Saltar funciones (COUNT(...)), tablas y aliases.
            next_val = tokens[i + 1].value if i + 1 < len(tokens) else ""
            if next_val == "(":
                i += 1
                continue
            if (
                key not in seen
                and key not in excluded
                and raw.upper() not in _SQL_KEYWORD_NAMES
            ):
                # Evitar alias de proyección: expr AS alias
                prev = tokens[i - 1].value.upper() if i > 0 else ""
                if prev != "AS":
                    seen.add(key)
                    columns.append(raw)

        i += 1

    return columns


def _build_explanation(
    sql: str,
    tables_used: list[str],
    columns_used: list[str],
) -> str:
    """Explicación breve y determinística a partir de la estructura del SQL."""
    statements = sqlparse.parse(sql)
    stmt_type = statements[0].get_type() if statements else "UNKNOWN"
    upper = sql.upper()

    if stmt_type == "SELECT":
        if columns_used:
            cols = ", ".join(columns_used[:6])
            head = f"Selecciona columnas ({cols})"
        else:
            head = "Selecciona datos"

        if tables_used:
            head += f" de la tabla {', '.join(tables_used)}"

        clauses: list[str] = []
        if re.search(r"\bJOIN\b", upper):
            clauses.append("con JOIN")
        if re.search(r"\bWHERE\b", upper):
            clauses.append("filtrando con WHERE")
        if re.search(r"\bGROUP\s+BY\b", upper):
            clauses.append("agrupando")
        if re.search(r"\bORDER\s+BY\b", upper):
            clauses.append("ordenando")
        if re.search(r"\bLIMIT\b", upper):
            clauses.append("con LIMIT")

        if clauses:
            return f"{head}, {', '.join(clauses)}."
        return f"{head}."

    table_bit = f" sobre {', '.join(tables_used)}" if tables_used else ""
    return f"Consulta {stmt_type}{table_bit}."
