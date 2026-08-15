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

    Combina few-shot concreto (agregaciones, JOINs, subconsultas, anti-joins,
    HAVING, fechas DuckDB, geografía vía ciudades) con una JOIN policy breve
    que no poda filtros explícitos de la pregunta.
    """
    return (
        "### Task\n"
        "Generate a SQL query to answer the following question.\n"
        "Use ONLY exact table/column names from the schema. "
        "Do not invent tables. Do not add filters the question does not ask.\n"
        "Dialect: DuckDB. Prefer CURRENT_DATE, INTERVAL, and dayofweek(col). "
        "Do not use to_date() or ::text casts on DATE columns. "
        "Output raw SQL only: no markdown fences, no backticks, no /* */ junk.\n"
        "Country of origin/destination: JOIN ciudades "
        "(do not invent literals like 'Europa'). "
        "Absence of related rows: use NOT EXISTS or LEFT JOIN ... IS NULL. "
        "When listing people who may match multiple rows, use SELECT DISTINCT. "
        "Select only columns needed; do not JOIN aerolineas unless asked.\n"
        "JOIN policy: JOIN tables when the question needs columns from more "
        "than one table (e.g. ingresos from reservas + destino from vuelos). "
        "Prefer the simplest correct query; avoid unnecessary JOINs, but do "
        "not refuse a needed JOIN. "
        "Never omit explicit filters mentioned in the question (such as "
        "specific reservation statuses or dates) when trying to optimize "
        "the query.\n\n"
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
        "Q: ¿Cuáles fueron los 5 destinos más reservados en los últimos "
        "90 días y cuánto generaron en ingresos?\n"
        "A: SELECT v.destino, COUNT(r.id) AS total_reservas, "
        "SUM(r.precio_pagado) AS ingresos FROM reservas r "
        "JOIN vuelos v ON r.vuelo_id = v.id "
        "WHERE r.fecha_reserva >= CURRENT_DATE - INTERVAL '90 days' "
        "GROUP BY v.destino ORDER BY total_reservas DESC LIMIT 5\n\n"
        "Q: ¿Cuál fue el vuelo con mayor cantidad de reservas "
        "confirmadas, incluyendo origen, destino y aerolínea?\n"
        "A: SELECT v.id, v.origen, v.destino, a.nombre AS aerolinea, "
        "COUNT(r.id) AS total_confirmadas FROM reservas r "
        "JOIN vuelos v ON r.vuelo_id = v.id "
        "JOIN aerolineas a ON v.aerolinea_id = a.id "
        "WHERE r.estado = 'confirmada' "
        "GROUP BY v.id, v.origen, v.destino, a.nombre "
        "ORDER BY total_confirmadas DESC LIMIT 1\n\n"
        "Q: ¿Cuántos vuelos salen hacia Roma?\n"
        "A: SELECT COUNT(*) FROM vuelos WHERE destino = 'Roma'\n\n"
        "Q: ¿Qué pasajeros de Chile hicieron reservas para vuelos con "
        "destino a un país distinto al de su residencia?\n"
        "A: SELECT DISTINCT p.nombre FROM pasajeros p "
        "JOIN reservas r ON p.id = r.pasajero_id "
        "JOIN vuelos v ON r.vuelo_id = v.id "
        "JOIN ciudades c ON v.destino = c.ciudad "
        "WHERE p.pais_residencia = 'Chile' "
        "AND c.pais != p.pais_residencia "
        "AND r.estado = 'confirmada' ORDER BY p.nombre\n\n"
        # F1: gasto vs AVG de reservas confirmadas
        "Q: Pasajeros que gastaron más que el precio promedio de todas "
        "las reservas confirmadas.\n"
        "A: SELECT p.nombre, SUM(r.precio_pagado) AS total_gastado "
        "FROM pasajeros p JOIN reservas r ON p.id = r.pasajero_id "
        "WHERE r.estado = 'confirmada' GROUP BY p.id, p.nombre "
        "HAVING SUM(r.precio_pagado) > ("
        "SELECT AVG(precio_pagado) FROM reservas "
        "WHERE estado = 'confirmada') "
        "ORDER BY total_gastado DESC\n\n"
        # F2: conteo por aerolínea vs promedio de conteos
        "Q: Aerolíneas con una cantidad de vuelos superior al promedio "
        "de vuelos por aerolínea.\n"
        "A: SELECT a.nombre, COUNT(v.id) AS n_vuelos "
        "FROM aerolineas a JOIN vuelos v ON v.aerolinea_id = a.id "
        "GROUP BY a.id, a.nombre "
        "HAVING COUNT(v.id) > ("
        "SELECT AVG(cnt) FROM ("
        "SELECT COUNT(*) AS cnt FROM vuelos GROUP BY aerolinea_id) t) "
        "ORDER BY n_vuelos DESC\n\n"
        # F3: anti-join / NOT EXISTS
        "Q: ¿Qué vuelos no tienen ninguna reserva confirmada registrada?\n"
        "A: SELECT v.id, v.origen, v.destino, v.fecha FROM vuelos v "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM reservas r "
        "WHERE r.vuelo_id = v.id AND r.estado = 'confirmada') "
        "ORDER BY v.id\n\n"
        # F4: HAVING con AVG (no columna cruda)
        "Q: ¿Qué destinos tienen más de 3 vuelos asignados y un precio "
        "medio superior a 200?\n"
        "A: SELECT destino, COUNT(*) AS total_vuelos, "
        "AVG(precio) AS precio_medio FROM vuelos "
        "GROUP BY destino "
        "HAVING COUNT(*) > 3 AND AVG(precio) > 200 "
        "ORDER BY destino\n\n"
        # F4b: listar pasajeros con HAVING (no COUNT escalar del total)
        "Q: ¿Qué pasajeros tienen más de 2 reservas en estado confirmada?\n"
        "A: SELECT p.nombre, COUNT(*) AS total_confirmadas "
        "FROM pasajeros p JOIN reservas r ON p.id = r.pasajero_id "
        "WHERE r.estado = 'confirmada' "
        "GROUP BY p.id, p.nombre "
        "HAVING COUNT(*) > 2 "
        "ORDER BY total_confirmadas DESC, p.nombre\n\n"
        # F5: fechas DuckDB (fin de semana + ventana futura)
        "Q: ¿Cuántas reservas se realizaron en fines de semana durante "
        "el último mes?\n"
        "A: SELECT COUNT(*) AS total_reservas FROM reservas "
        "WHERE fecha_reserva >= CURRENT_DATE - INTERVAL '1 month' "
        "AND dayofweek(fecha_reserva) IN (0, 6)\n\n"
        "Q: Muestra los vuelos programados para salir en los próximos "
        "7 días con su cantidad de reservas.\n"
        "A: SELECT v.id, v.origen, v.destino, v.fecha, "
        "COUNT(r.id) AS total_reservas FROM vuelos v "
        "LEFT JOIN reservas r ON r.vuelo_id = v.id "
        "WHERE v.fecha >= CURRENT_DATE "
        "AND v.fecha < CURRENT_DATE + INTERVAL '7 days' "
        "GROUP BY v.id, v.origen, v.destino, v.fecha "
        "ORDER BY v.fecha, v.id\n\n"
        # F6: matriz ingresos + Europa vía ciudades
        "Q: Muestra el total de ingresos por país de residencia del "
        "pasajero y aerolínea.\n"
        "A: SELECT p.pais_residencia, a.nombre AS aerolinea, "
        "SUM(r.precio_pagado) AS ingresos FROM reservas r "
        "JOIN pasajeros p ON r.pasajero_id = p.id "
        "JOIN vuelos v ON r.vuelo_id = v.id "
        "JOIN aerolineas a ON v.aerolinea_id = a.id "
        "WHERE r.estado = 'confirmada' "
        "GROUP BY p.pais_residencia, a.nombre "
        "ORDER BY ingresos DESC\n\n"
        "Q: ¿Cuál es el pasajero de Argentina que más dinero gastó en "
        "vuelos hacia Europa?\n"
        "A: SELECT p.nombre, SUM(r.precio_pagado) AS total_gastado "
        "FROM pasajeros p "
        "JOIN reservas r ON p.id = r.pasajero_id "
        "JOIN vuelos v ON r.vuelo_id = v.id "
        "JOIN ciudades c ON v.destino = c.ciudad "
        "WHERE p.pais_residencia = 'Argentina' "
        "AND c.pais = 'España' AND r.estado = 'confirmada' "
        "GROUP BY p.id, p.nombre "
        "ORDER BY total_gastado DESC LIMIT 1\n\n"
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
    from guardrails import _SQL_ARTIFACT_RE, sanitize_generated_sql

    text = _CONTROL_TOKEN_RE.sub("", raw).strip()
    text = _normalize_sql(text)
    if text.endswith("```"):
        text = text[: text.rfind("```")].rstrip()

    # Solo artefacts de fence (sin cortar en ';') para no perder un SELECT
    # que venga después de basura tipo `/******/`.
    text = _SQL_ARTIFACT_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text).strip()

    if not text:
        return text

    upper = text.upper()
    for keyword in ("WITH ", "SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        idx = upper.find(keyword)
        if idx != -1:
            return sanitize_generated_sql(text[idx:])
    return sanitize_generated_sql(text)


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
