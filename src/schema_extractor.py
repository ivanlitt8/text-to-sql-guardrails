"""
schema_extractor.py

Responsable de introspectar el schema de DuckDB y devolver una
representación en DDL (CREATE TABLE) para inyectar en el prompt del
generador de SQL, en el formato que sqlcoder espera (ver docs/HISTORY.md).

Ver docs/SPECS.md sección 5 para el detalle de tablas.
En v1 no hace falta filtrado por relevancia semántica: el schema
completo (4 tablas) es chico y entra sin problema en el contexto.
"""

from __future__ import annotations

from collections import defaultdict

import duckdb


def extract_schema(con: duckdb.DuckDBPyConnection) -> str:
    """
    Introspecta la conexión de DuckDB y devuelve el schema como
    sentencias CREATE TABLE (DDL), incluyendo PRIMARY KEY, FOREIGN KEY
    y CHECK cuando están disponibles vía duckdb_constraints().
    """
    tables = _list_tables(con)
    columns_by_table = _load_columns(con)
    constraints_by_table = _load_constraints(con)

    statements: list[str] = []
    for table_name in tables:
        columns = columns_by_table.get(table_name, [])
        constraints = constraints_by_table.get(table_name, [])
        statements.append(_build_create_table(table_name, columns, constraints))

    return "\n\n".join(statements)


def _list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Lista tablas de usuario en orden de creación (table_oid)."""
    rows = con.execute(
        """
        SELECT table_name
        FROM duckdb_tables()
        WHERE NOT internal
          AND schema_name = 'main'
        ORDER BY table_oid
        """
    ).fetchall()
    return [row[0] for row in rows]


def _load_columns(
    con: duckdb.DuckDBPyConnection,
) -> dict[str, list[tuple[str, str, bool]]]:
    """
    Devuelve {tabla: [(nombre, tipo, is_nullable), ...]} ordenado
    por column_index.
    """
    rows = con.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM duckdb_columns()
        WHERE NOT internal
          AND schema_name = 'main'
        ORDER BY table_name, column_index
        """
    ).fetchall()

    columns: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    for table_name, column_name, data_type, is_nullable in rows:
        columns[table_name].append((column_name, data_type, bool(is_nullable)))
    return columns


def _load_constraints(
    con: duckdb.DuckDBPyConnection,
) -> dict[str, list[tuple]]:
    """
    Devuelve {tabla: [(constraint_type, constraint_text,
    constraint_column_names, referenced_table,
    referenced_column_names), ...]}.
    """
    rows = con.execute(
        """
        SELECT table_name,
               constraint_type,
               constraint_text,
               constraint_column_names,
               referenced_table,
               referenced_column_names
        FROM duckdb_constraints()
        WHERE schema_name = 'main'
        ORDER BY table_name, constraint_index
        """
    ).fetchall()

    constraints: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        table_name = row[0]
        constraints[table_name].append(row[1:])
    return constraints


def _build_create_table(
    table_name: str,
    columns: list[tuple[str, str, bool]],
    constraints: list[tuple],
) -> str:
    """Arma una sentencia CREATE TABLE a partir de columnas y constraints."""
    pk_columns = _single_column_names(
        [
            cols
            for ctype, _text, cols, _ref_table, _ref_cols in constraints
            if ctype == "PRIMARY KEY"
        ]
    )
    fk_by_column = _inline_foreign_keys(constraints)
    check_clauses = [
        text
        for ctype, text, _cols, _ref_table, _ref_cols in constraints
        if ctype == "CHECK" and text
    ]

    lines: list[str] = []
    for column_name, data_type, is_nullable in columns:
        parts = [f"    {column_name} {data_type}"]

        if column_name in pk_columns:
            parts.append("PRIMARY KEY")
        elif not is_nullable:
            parts.append("NOT NULL")

        if column_name in fk_by_column:
            ref_table, ref_col = fk_by_column[column_name]
            parts.append(f"REFERENCES {ref_table}({ref_col})")

        lines.append(" ".join(parts))

    for check in check_clauses:
        lines.append(f"    {check}")

    body = ",\n".join(lines)
    return f"CREATE TABLE {table_name} (\n{body}\n);"


def _single_column_names(column_lists: list) -> set[str]:
    """Extrae nombres de constraints de una sola columna."""
    names: set[str] = set()
    for cols in column_lists:
        if cols is None:
            continue
        col_list = list(cols)
        if len(col_list) == 1:
            names.add(col_list[0])
    return names


def _inline_foreign_keys(
    constraints: list[tuple],
) -> dict[str, tuple[str, str]]:
    """
    Mapea columna local -> (tabla_referenciada, columna_referenciada)
    solo para FKs de una sola columna (estilo inline REFERENCES).
    """
    result: dict[str, tuple[str, str]] = {}
    for ctype, _text, cols, ref_table, ref_cols in constraints:
        if ctype != "FOREIGN KEY" or not ref_table:
            continue
        col_list = list(cols) if cols is not None else []
        ref_list = list(ref_cols) if ref_cols is not None else []
        if len(col_list) == 1 and len(ref_list) == 1:
            result[col_list[0]] = (ref_table, ref_list[0])
    return result
