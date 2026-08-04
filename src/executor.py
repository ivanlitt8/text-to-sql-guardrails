"""
executor.py

Ejecución segura de SQL en DuckDB (solo lectura). Ver SPECS.md §7.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB = "data/vuelos.duckdb"


def execute_query(
    sql: str,
    db_path: str | None = None,
) -> list[dict]:
    """
    Ejecuta `sql` en DuckDB en modo solo-lectura.

    Si la query falla o la conexión no está disponible, retorna lista vacía
    (no propaga la excepción al orquestador).
    """
    path = _resolve_db_path(db_path)
    try:
        _ensure_database(path)
        con = duckdb.connect(str(path), read_only=True)
        try:
            # Transacción explícita: en read_only no hay commits útiles,
            # pero documenta la intención de no persistir cambios.
            con.execute("BEGIN TRANSACTION")
            cursor = con.execute(sql)
            columns = [col[0] for col in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            con.execute("ROLLBACK")
            return [_row_to_dict(columns, row) for row in rows]
        finally:
            con.close()
    except Exception:
        return []


def list_schema_tables(db_path: str | None = None) -> list[str]:
    """Lista tablas de usuario en el schema main (orden de creación)."""
    path = _resolve_db_path(db_path)
    _ensure_database(path)
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT table_name
            FROM duckdb_tables()
            WHERE NOT internal AND schema_name = 'main'
            ORDER BY table_oid
            """
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        con.close()


def open_connection_for_schema(db_path: str | None = None) -> duckdb.DuckDBPyConnection:
    """
    Abre conexión de lectura para extract_schema().
    El caller debe cerrarla.
    """
    path = _resolve_db_path(db_path)
    _ensure_database(path)
    return duckdb.connect(str(path), read_only=True)


def _resolve_db_path(db_path: str | None) -> Path:
    load_dotenv()
    raw = db_path or os.getenv("DUCKDB_PATH", _DEFAULT_DB)
    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def _ensure_database(path: Path) -> None:
    """Crea la DB desde schema.sql + seed_data.sql si aún no existe."""
    if path.exists():
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    schema_file = _PROJECT_ROOT / "data" / "schema.sql"
    seed_file = _PROJECT_ROOT / "data" / "seed_data.sql"

    con = duckdb.connect(str(path))
    try:
        con.execute(schema_file.read_text(encoding="utf-8"))
        con.execute(seed_file.read_text(encoding="utf-8"))
    finally:
        con.close()


def _row_to_dict(columns: list[str], row: tuple[Any, ...]) -> dict:
    result: dict[str, Any] = {}
    for key, value in zip(columns, row):
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif type(value).__name__ == "Decimal":
            result[key] = float(value)
        else:
            result[key] = value
    return result
