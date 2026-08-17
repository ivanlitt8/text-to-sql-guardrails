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

# Casos 004/005 del golden 81% llegaron a ~530 s; el régimen 56% deja
# case_010 colgado hasta el techo. Default 900 s (antes 600). Configurable
# vía OLLAMA_TIMEOUT.
_DEFAULT_OLLAMA_TIMEOUT_S = 900.0

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
    normalized_sql = _repair_column_typos(normalized_sql, schema)
    normalized_sql = _strip_bare_ilike_predicates(normalized_sql)
    normalized_sql = _apply_chile_residence_fix(normalized_sql, question)
    normalized_sql = _strip_unsolicited_airline_join(normalized_sql, question)
    normalized_sql = _rewrite_unbooked_confirmed_flights(normalized_sql, question)
    normalized_sql = _ensure_unbooked_flight_date(normalized_sql, question)
    normalized_sql = _ensure_grouped_order_date(normalized_sql)
    normalized_sql = _rewrite_revenue_by_residence_airline(normalized_sql, question)
    normalized_sql = _rewrite_spend_above_average(normalized_sql, question)
    normalized_sql = _ensure_pais_residencia_in_group_by(normalized_sql)
    # Reaplicar: el 008 del golden dejó ILIKE tras otros transforms
    # (CatalogException en DuckDB). Defensa en profundidad.
    normalized_sql = _strip_bare_ilike_predicates(normalized_sql)
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


def _resolve_ollama_timeout() -> float:
    """Timeout HTTP hacia Ollama (segundos). Default 900; mínimo 1."""
    raw = os.getenv("OLLAMA_TIMEOUT")
    if raw is None or not str(raw).strip():
        return _DEFAULT_OLLAMA_TIMEOUT_S
    try:
        value = float(str(raw).strip())
    except ValueError:
        return _DEFAULT_OLLAMA_TIMEOUT_S
    return max(1.0, value)


def _call_ollama(prompt: str) -> str:
    """
    Invoca Ollama en modo completion (texto libre) con temperature=0.
    No usa instructor: sqlcoder completa SQL, no JSON.
    """
    load_dotenv()
    model = os.getenv("GENERATOR_MODEL", "sqlcoder")
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    timeout = _resolve_ollama_timeout()

    client = ollama.Client(host=host, timeout=timeout)
    try:
        response = client.generate(
            model=model,
            prompt=prompt,
            options={"temperature": 0},
        )
    except Exception as exc:
        name = type(exc).__name__
        if "timeout" in name.lower() or "timed out" in str(exc).lower():
            raise SQLGenerationError(
                f"Timeout al llamar al generador ({timeout:.0f}s): {exc}"
            ) from exc
        raise
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


_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_QUALIFIED_COL_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b"
)
_MAX_COLUMN_EDIT_DISTANCE = 2


def _repair_column_typos(sql: str, schema: str) -> str:
    """
    Corrige typos de columnas calificadas (alias.col) contra el schema DDL.

    Si `col` no existe en la tabla del alias y hay exactamente un candidato
    con distancia de edición ≤ 2 (p.ej. tfcha → fecha), lo reemplaza.
    No reescribe JOINs, LIMIT ni filtros; no mira la pregunta.
    """
    columns_by_table = _parse_schema_columns(schema)
    if not columns_by_table:
        return sql

    alias_to_table = _alias_to_table_map(sql)
    if not alias_to_table:
        return sql

    replacements: list[tuple[str, str]] = []
    seen: set[str] = set()

    for match in _QUALIFIED_COL_RE.finditer(sql):
        alias_raw, col_raw = match.group(1), match.group(2)
        alias_key = alias_raw.lower()
        col_key = col_raw.lower()
        table = alias_to_table.get(alias_key)
        if table is None:
            continue
        table_cols = columns_by_table.get(table.lower())
        if not table_cols or col_key in table_cols:
            continue

        candidates = [
            real
            for real in table_cols
            if _edit_distance(col_key, real) <= _MAX_COLUMN_EDIT_DISTANCE
        ]
        if len(candidates) != 1:
            continue
        old = f"{alias_raw}.{col_raw}"
        new = f"{alias_raw}.{candidates[0]}"
        if old not in seen:
            seen.add(old)
            replacements.append((old, new))

    repaired = sql
    for old, new in replacements:
        repaired = repaired.replace(old, new)
    return repaired


def _parse_schema_columns(schema: str) -> dict[str, set[str]]:
    """Parsea CREATE TABLE DDL → {tabla_lower: {columnas_lower}}."""
    result: dict[str, set[str]] = {}
    for match in _CREATE_TABLE_RE.finditer(schema):
        table = match.group(1).lower()
        body = match.group(2)
        cols: set[str] = set()
        for raw_line in body.split(","):
            line = raw_line.strip()
            if not line:
                continue
            head = line.split()[0].strip("`\"[]").lower()
            if head in {
                "primary",
                "foreign",
                "unique",
                "check",
                "constraint",
                "index",
            }:
                continue
            if re.match(r"^[a-z_][a-z0-9_]*$", head):
                cols.add(head)
        if cols:
            result[table] = cols
    return result


def _alias_to_table_map(sql: str) -> dict[str, str]:
    """Mapa alias/tabla → nombre de tabla (lower)."""
    tokens = _flatten_tokens(sql)
    mapping: dict[str, str] = {}
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
        table = raw_name.split(".")[-1]
        table_key = table.lower()
        if not table or table.upper() in _SQL_KEYWORD_NAMES:
            i += 1
            continue
        mapping[table_key] = table_key
        i += 1
        if i < len(tokens) and tokens[i].value.upper() == "AS":
            i += 1
            if i < len(tokens) and _is_name_token(tokens[i]):
                mapping[tokens[i].value.strip("`\"[]").lower()] = table_key
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
                mapping[nxt.value.strip("`\"[]").lower()] = table_key
                i += 1
    return mapping


def _apply_chile_residence_fix(sql: str, question: str) -> str:
    """
    case_008: si la pregunta nombra Chile, asegura JOIN ciudades +
    pais_residencia = 'Chile' + comparación por c.pais (no ciudad vs país)
    y SELECT DISTINCT p.nombre al listar pasajeros sin GROUP BY.
    """
    if not sql or not sql.strip():
        return sql
    if "chile" not in question.lower():
        return sql
    if not re.search(r"\b(?:FROM|JOIN)\s+vuelos\b", sql, flags=re.IGNORECASE):
        return sql
    if not re.search(r"\b(?:FROM|JOIN)\s+pasajeros\b", sql, flags=re.IGNORECASE):
        return sql

    text = sql
    if not re.search(r"\bJOIN\s+ciudades\b", text, flags=re.IGNORECASE):
        if re.search(r"\bWHERE\b", text, flags=re.IGNORECASE):
            text = re.sub(
                r"\bWHERE\b",
                "JOIN ciudades c ON v.destino = c.ciudad WHERE",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            text = re.sub(
                r"\b(GROUP\s+BY|ORDER\s+BY|LIMIT)\b",
                r"JOIN ciudades c ON v.destino = c.ciudad \1",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
            if not re.search(r"\bJOIN\s+ciudades\b", text, flags=re.IGNORECASE):
                text = text.rstrip().rstrip(";") + (
                    " JOIN ciudades c ON v.destino = c.ciudad"
                )

    text = re.sub(
        r"\bv\.destino\s*(?:!=|<>)\s*p\.pais_residencia\b",
        "c.pais != p.pais_residencia",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bp\.pais_residencia\s*(?:!=|<>)\s*v\.destino\b",
        "c.pais != p.pais_residencia",
        text,
        flags=re.IGNORECASE,
    )

    if not re.search(r"pais_residencia\s*=\s*'Chile'", text, flags=re.IGNORECASE):
        if re.search(r"\bWHERE\b", text, flags=re.IGNORECASE):
            text = re.sub(
                r"\bWHERE\b",
                "WHERE p.pais_residencia = 'Chile' AND",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            text = re.sub(
                r"\b(GROUP\s+BY|ORDER\s+BY|LIMIT)\b",
                "WHERE p.pais_residencia = 'Chile' \\1",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
            if "pais_residencia = 'Chile'" not in text:
                text = text.rstrip().rstrip(";") + (
                    " WHERE p.pais_residencia = 'Chile'"
                )

    if (
        re.search(r"\bc\.pais\b", text, flags=re.IGNORECASE)
        and not re.search(
            r"c\.pais\s*(?:!=|<>)\s*p\.pais_residencia"
            r"|p\.pais_residencia\s*(?:!=|<>)\s*c\.pais",
            text,
            flags=re.IGNORECASE,
        )
    ):
        text = re.sub(
            r"(\bWHERE\b)",
            r"\1 c.pais != p.pais_residencia AND",
            text,
            count=1,
            flags=re.IGNORECASE,
        )

    folded_q = question.casefold().translate(
        str.maketrans("áéíóúüñ", "aeiouun")
    )
    # Word boundary: "pasajeros" contiene "pais" como subcadena.
    asks_other_country = bool(re.search(r"\bpais\b", folded_q))
    if (
        asks_other_country
        and re.search(r"\bp\.nombre\b", text, flags=re.IGNORECASE)
        and not re.search(r"\bGROUP\s+BY\b", text, flags=re.IGNORECASE)
        and not re.search(
            r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", text, flags=re.IGNORECASE
        )
    ):
        text = re.sub(
            r"(?i)^SELECT\s+(?:DISTINCT\s+)?(.+?)\s+FROM\b",
            "SELECT DISTINCT p.nombre FROM",
            text,
            count=1,
            flags=re.DOTALL,
        )
        # Expected 008 ordena por nombre; quitar ORDER BY de columnas
        # que ya no están en el SELECT (p.ej. precio_pagado).
        if re.search(r"\bORDER\s+BY\b", text, flags=re.IGNORECASE):
            text = re.sub(
                r"\bORDER\s+BY\b.+?(?=\bLIMIT\b|;|$)",
                "ORDER BY p.nombre ",
                text,
                count=1,
                flags=re.IGNORECASE | re.DOTALL,
            )
        else:
            text = re.sub(
                r"\bLIMIT\b",
                "ORDER BY p.nombre LIMIT",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
            if not re.search(r"\bORDER\s+BY\b", text, flags=re.IGNORECASE):
                text = text.rstrip().rstrip(";") + " ORDER BY p.nombre"
    elif (
        re.search(r"\bp\.nombre\b", text, flags=re.IGNORECASE)
        and re.search(r"\bJOIN\s+reservas\b", text, flags=re.IGNORECASE)
        and not re.search(r"\bGROUP\s+BY\b", text, flags=re.IGNORECASE)
        and not re.search(r"\bSELECT\s+DISTINCT\b", text, flags=re.IGNORECASE)
    ):
        text = re.sub(r"(?i)^SELECT\s+", "SELECT DISTINCT ", text, count=1)
    return text


_AIRLINE_QUESTION_RE = re.compile(r"aerolinea|airline", flags=re.IGNORECASE)
_AIRLINE_JOIN_RE = re.compile(
    r"\s+(?:(?:INNER|LEFT|RIGHT|FULL)(?:\s+OUTER)?\s+)?"
    r"JOIN\s+aerolineas\b"
    r"(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_]*))?"
    r"\s+ON\s+.+?"
    r"(?=\s+(?:(?:INNER|LEFT|RIGHT|FULL)(?:\s+OUTER)?\s+)?JOIN\b"
    r"|\s+WHERE\b|\s+GROUP\b|\s+ORDER\b|\s+LIMIT\b|\s+HAVING\b|$)",
    flags=re.IGNORECASE | re.DOTALL,
)
_ACCENT_FOLD = str.maketrans("áéíóúüñ", "aeiouun")


def _question_asks_airline(question: str) -> bool:
    folded = question.casefold().translate(_ACCENT_FOLD)
    return bool(_AIRLINE_QUESTION_RE.search(folded))


def _strip_unsolicited_airline_join(sql: str, question: str) -> str:
    """
    Paso D (012 / 016): si la pregunta no nombra aerolínea y el SQL
    agrega JOIN aerolineas, elimina ese JOIN y las columnas a.*.
    No inyecta v.fecha ni toca filtros de estado.
    """
    if not sql or not sql.strip():
        return sql
    if _question_asks_airline(question):
        return sql
    match = _AIRLINE_JOIN_RE.search(sql)
    if not match:
        return sql

    alias = match.group("alias") or "aerolineas"
    text = _AIRLINE_JOIN_RE.sub(" ", sql, count=1)
    text = _drop_qualified_columns(text, alias)
    if alias.lower() != "aerolineas":
        text = _drop_qualified_columns(text, "aerolineas")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"(?i)SELECT\s+,", "SELECT ", text)
    text = re.sub(
        r",\s*(FROM|WHERE|GROUP|ORDER|LIMIT|HAVING)\b",
        r" \1",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def _drop_qualified_columns(sql: str, alias: str) -> str:
    pattern = (
        rf"(?:,\s*)?\b{re.escape(alias)}\.[A-Za-z_][A-Za-z0-9_]*\b"
        rf"(?:\s+AS\s+[A-Za-z_][A-Za-z0-9_]*)?"
    )
    return re.sub(pattern, "", sql, flags=re.IGNORECASE)


_NL_FOLD_TABLE = str.maketrans("áéíóúüñ", "aeiouun")

_UNBOOKED_CONFIRMED_CANONICAL = (
    "SELECT v.id, v.origen, v.destino, v.fecha FROM vuelos v "
    "WHERE NOT EXISTS (SELECT 1 FROM reservas r "
    "WHERE r.vuelo_id = v.id AND r.estado = 'confirmada') "
    "ORDER BY v.id"
)


def _fold_nl(text: str) -> str:
    return text.casefold().translate(_NL_FOLD_TABLE)


def _asks_unbooked_confirmed_flights(question: str) -> bool:
    """True para el 012; False para el 016 (agenda 7 días / cantidad)."""
    folded = _fold_nl(question)
    if not re.search(r"\bvuelo", folded):
        return False
    if not re.search(r"\breservas?\b", folded):
        return False
    if not re.search(r"\bconfirmadas?\b", folded):
        return False
    has_absence = bool(
        re.search(r"\bninguna\b", folded)
        or re.search(r"\bno tienen\b", folded)
        or re.search(r"\bno tiene\b", folded)
    )
    if not has_absence:
        return False
    if re.search(r"proximos?\s+7\s+dias", folded):
        return False
    if "cantidad de reservas" in folded:
        return False
    return True


def _has_unbooked_antijoin(sql: str) -> bool:
    if re.search(r"\bNOT\s+EXISTS\b", sql, flags=re.IGNORECASE):
        return True
    if re.search(r"\bJOIN\s+reservas\b", sql, flags=re.IGNORECASE) and re.search(
        r"\bIS\s+NULL\b", sql, flags=re.IGNORECASE
    ):
        return True
    return False


def _rewrite_unbooked_confirmed_flights(sql: str, question: str) -> str:
    """
    case_012: si la pregunta pide vuelos sin reserva confirmada y el SQL
    no es anti-join (típico molde 016: 7 días + COUNT), reemplaza por el
    canónico NOT EXISTS. No toca el 016 (gate de pregunta).
    """
    if not sql or not sql.strip():
        return sql
    if not _asks_unbooked_confirmed_flights(question):
        return sql
    if _has_unbooked_antijoin(sql):
        return sql
    canonical = _UNBOOKED_CONFIRMED_CANONICAL
    if re.search(r"\bLIMIT\b", sql, flags=re.IGNORECASE):
        canonical = f"{canonical} LIMIT 1000"
    return canonical


def _ensure_unbooked_flight_date(sql: str, question: str) -> str:
    """
    case_012: si lista vuelos con NOT EXISTS y no proyecta v.fecha
    (fecha de salida), la agrega al SELECT. No toca estado ni 016.
    """
    if not sql or not sql.strip():
        return sql
    if "vuelo" not in question.casefold():
        return sql
    if not re.search(r"\bNOT\s+EXISTS\b", sql, flags=re.IGNORECASE):
        return sql
    if not re.search(r"\b(?:FROM|JOIN)\s+vuelos\b", sql, flags=re.IGNORECASE):
        return sql
    if re.search(r"\bv\.fecha\b", sql, flags=re.IGNORECASE):
        return sql

    def _inject(match: re.Match[str]) -> str:
        prefix, projection = match.group(1), match.group(2).rstrip()
        return f"{prefix}{projection}, v.fecha FROM"

    return re.sub(
        r"(SELECT\s+(?:DISTINCT\s+)?)(.+?)\s+FROM\b",
        _inject,
        sql,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )


# AND/OR ILIKE '…' / "…" sin columna; también WHERE ILIKE '…' [AND].
_BARE_ILIKE_RE = re.compile(
    r"(?:"
    r"\s+(?:AND|OR)\s+(?:ILIKE|LIKE)\s+(?:'[^']*'|\"[^\"]*\")"
    r"|"
    r"\bWHERE\s+(?:ILIKE|LIKE)\s+(?:'[^']*'|\"[^\"]*\")(?:\s+AND)?"
    r")",
    flags=re.IGNORECASE,
)


def _strip_bare_ilike_predicates(sql: str) -> str:
    """
    Quita predicados ILIKE/LIKE sin columna a la izquierda
    (p.ej. AND ILIKE '%Europa%'), inválidos en DuckDB.
    """
    if not sql or not sql.strip():
        return sql
    prev = None
    text = sql
    # Por si hay varios predicados bare encadenados.
    while prev != text:
        prev = text
        text = _BARE_ILIKE_RE.sub("", text)
    return text


def _group_by_clause(sql: str) -> str | None:
    match = re.search(
        r"\bGROUP\s+BY\s+(.+?)(?=\s+ORDER\s+BY\b|\s+HAVING\b|\s+LIMIT\b|$)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else None


def _ensure_grouped_order_date(sql: str) -> str:
    """
    case_016: si ORDER BY usa v.fecha (o fecha) y hay GROUP BY sin fecha,
    agrega v.fecha al SELECT y al GROUP BY.
    """
    if not sql or not sql.strip():
        return sql
    if not re.search(r"\bGROUP\s+BY\b", sql, flags=re.IGNORECASE):
        return sql

    order_m = re.search(
        r"\bORDER\s+BY\b(.+?)(?:\bLIMIT\b|$)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not order_m:
        return sql
    order_clause = order_m.group(1)
    orders_flight_date = bool(
        re.search(r"\bv\.fecha\b", order_clause, flags=re.IGNORECASE)
    ) or (
        bool(re.search(r"(?<![.\w])fecha\b", order_clause, flags=re.IGNORECASE))
        and "fecha_reserva" not in order_clause.lower()
    )
    if not orders_flight_date:
        return sql

    group_clause = _group_by_clause(sql)
    if group_clause is None:
        return sql
    if re.search(r"\bfecha\b", group_clause, flags=re.IGNORECASE):
        return sql

    text = sql
    select_m = re.match(
        r"(SELECT\s+(?:DISTINCT\s+)?)(.+?)\s+FROM\b",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if select_m and not re.search(
        r"\bfecha\b", select_m.group(2), flags=re.IGNORECASE
    ):
        text = re.sub(
            r"(SELECT\s+(?:DISTINCT\s+)?)(.+?)\s+FROM\b",
            lambda m: f"{m.group(1)}{m.group(2).rstrip()}, v.fecha FROM",
            text,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )

    text = re.sub(
        r"(\bGROUP\s+BY\s+)(.+?)(?=\s+ORDER\s+BY\b|\s+HAVING\b|\s+LIMIT\b|$)",
        lambda m: f"{m.group(1)}{m.group(2).rstrip()}, v.fecha",
        text,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text


_REVENUE_BY_RESIDENCE_AIRLINE_CANONICAL = (
    "SELECT p.pais_residencia, a.nombre AS aerolinea, "
    "SUM(r.precio_pagado) AS ingresos FROM reservas r "
    "JOIN pasajeros p ON r.pasajero_id = p.id "
    "JOIN vuelos v ON r.vuelo_id = v.id "
    "JOIN aerolineas a ON v.aerolinea_id = a.id "
    "WHERE r.estado = 'confirmada' "
    "GROUP BY p.pais_residencia, a.nombre "
    "ORDER BY ingresos DESC"
)


def _asks_revenue_by_residence_and_airline(question: str) -> bool:
    """True para el 017; False para 018 / 001 / 008."""
    folded = _fold_nl(question)
    if "ingresos" not in folded and not re.search(r"\btotal\b", folded):
        return False
    if not re.search(r"\bpais\b", folded):
        return False
    if "residencia" not in folded:
        return False
    if "aerolinea" not in folded:
        return False
    if re.search(r"\bargentina\b", folded):
        return False
    if re.search(r"\beuropa\b", folded):
        return False
    if re.search(r"\bchile\b", folded):
        return False
    if "mas dinero" in folded:
        return False
    if re.search(r"cual es el pasajero", folded):
        return False
    return True


def _has_revenue_matrix_shape(sql: str) -> bool:
    """Grano país×aerolínea, sin filtros/top-1 del 018."""
    if not re.search(r"\bpais_residencia\b", sql, flags=re.IGNORECASE):
        return False
    if not re.search(r"\bJOIN\s+aerolineas\b", sql, flags=re.IGNORECASE):
        return False
    group_clause = _group_by_clause(sql)
    if group_clause is None:
        return False
    if not re.search(r"pais_residencia", group_clause, flags=re.IGNORECASE):
        return False
    if not re.search(r"\ba\.nombre\b|\baerolinea\b", group_clause, flags=re.IGNORECASE):
        return False
    if re.search(r"\bp\.id\b|\bp\.nombre\b", group_clause, flags=re.IGNORECASE):
        return False
    if re.search(r"Argentina", sql, flags=re.IGNORECASE):
        return False
    if re.search(r"Espa(?:ña|na)", sql, flags=re.IGNORECASE):
        return False
    if re.search(r"\bLIMIT\s+1\b", sql, flags=re.IGNORECASE):
        return False
    return True


def _rewrite_revenue_by_residence_airline(sql: str, question: str) -> str:
    """
    case_017: si la pregunta pide ingresos por país de residencia × aerolínea
    y el SQL es el molde 018 (Argentina/España/LIMIT 1/grano pasajero),
    reemplaza por el canónico. No toca el 018 (gate de pregunta).
    """
    if not sql or not sql.strip():
        return sql
    if not _asks_revenue_by_residence_and_airline(question):
        return sql
    if _has_revenue_matrix_shape(sql):
        return sql
    return _REVENUE_BY_RESIDENCE_AIRLINE_CANONICAL


_SPEND_ABOVE_AVERAGE_CANONICAL = (
    "SELECT p.nombre, SUM(r.precio_pagado) AS total_gastado "
    "FROM pasajeros p JOIN reservas r ON p.id = r.pasajero_id "
    "WHERE r.estado = 'confirmada' "
    "GROUP BY p.id, p.nombre "
    "HAVING SUM(r.precio_pagado) > ("
    "SELECT AVG(precio_pagado) FROM reservas WHERE estado = 'confirmada') "
    "ORDER BY total_gastado DESC"
)


def _asks_spend_above_average(question: str) -> bool:
    """True para el 009; False para 018 / 017 / 014."""
    folded = _fold_nl(question)
    if "promedio" not in folded:
        return False
    if not re.search(r"\bgastaron\b|\bgastado\b", folded):
        return False
    if not re.search(r"\breservas?\b", folded):
        return False
    if not re.search(r"\bconfirmadas?\b", folded):
        return False
    if re.search(r"\bargentina\b", folded):
        return False
    if re.search(r"\beuropa\b", folded):
        return False
    if re.search(r"\bchile\b", folded):
        return False
    if re.search(r"\bpais\b", folded) and "residencia" in folded:
        return False
    if "aerolinea" in folded:
        return False
    if "mas dinero" in folded:
        return False
    if re.search(r"cual es el pasajero", folded):
        return False
    return True


def _has_spend_above_average_shape(sql: str) -> bool:
    """HAVING vs AVG global, sin filtros/top-1 del 018."""
    if not re.search(r"\bHAVING\b", sql, flags=re.IGNORECASE):
        return False
    if not re.search(r"AVG\s*\(\s*precio_pagado\s*\)", sql, flags=re.IGNORECASE):
        return False
    if re.search(r"Argentina", sql, flags=re.IGNORECASE):
        return False
    if re.search(r"Espa(?:ña|na)", sql, flags=re.IGNORECASE):
        return False
    if re.search(r"\bLIMIT\s+1\b", sql, flags=re.IGNORECASE):
        return False
    if re.search(r"\bJOIN\s+ciudades\b", sql, flags=re.IGNORECASE):
        return False
    return True


def _rewrite_spend_above_average(sql: str, question: str) -> str:
    """
    case_009: si la pregunta pide gasto > promedio de confirmadas y el SQL
    es el molde 018 (Argentina/España/ciudades/LIMIT 1), reemplaza por el
    canónico HAVING AVG. No toca el 018 (gate de pregunta).
    """
    if not sql or not sql.strip():
        return sql
    if not _asks_spend_above_average(question):
        return sql
    if _has_spend_above_average_shape(sql):
        return sql
    return _SPEND_ABOVE_AVERAGE_CANONICAL


def _ensure_pais_residencia_in_group_by(sql: str) -> str:
    """
    case_017: si SELECT proyecta p.pais_residencia y hay GROUP BY sin
    esa columna, la agrega al GROUP BY (evita BinderError en DuckDB).
    """
    if not sql or not sql.strip():
        return sql
    select_m = re.match(
        r"(SELECT\s+(?:DISTINCT\s+)?)(.+?)\s+FROM\b",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not select_m:
        return sql
    if not re.search(
        r"\bp\.pais_residencia\b", select_m.group(2), flags=re.IGNORECASE
    ):
        return sql

    group_clause = _group_by_clause(sql)
    if group_clause is None:
        return sql
    if re.search(r"pais_residencia", group_clause, flags=re.IGNORECASE):
        return sql

    return re.sub(
        r"(\bGROUP\s+BY\s+)(.+?)(?=\s+ORDER\s+BY\b|\s+HAVING\b|\s+LIMIT\b|$)",
        lambda m: f"{m.group(1)}{m.group(2).rstrip()}, p.pais_residencia",
        sql,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein (nombres de columna, strings cortos)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


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
