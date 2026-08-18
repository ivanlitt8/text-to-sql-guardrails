"""
schema_catalog.py

GET /v1/schema: introspección DuckDB + metadata semántica (SPECS.md §5 / §10.4).
La estructura (tablas, columnas, PK, FK) sale de schema_extractor; este
módulo solo enriquece descripciones, hints, sugerencias y limitaciones.
"""

from __future__ import annotations

import duckdb
from pydantic import BaseModel

from executor import open_connection_for_schema
from schema_extractor import introspect_schema


class SchemaUnavailableError(Exception):
    """DuckDB no está accesible o el schema no se pudo leer."""


class SchemaForeignKey(BaseModel):
    table: str
    column: str


class SchemaColumnResponse(BaseModel):
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool
    foreign_key: SchemaForeignKey | None = None
    description: str | None = None


class SchemaTableResponse(BaseModel):
    name: str
    description: str | None = None
    columns: list[SchemaColumnResponse]


class SchemaHintResponse(BaseModel):
    title: str
    body: str


class SchemaResponse(BaseModel):
    tables: list[SchemaTableResponse]
    hints: list[SchemaHintResponse]
    prompt_suggestions: list[str]
    limitations: list[str]


# Metadata semántica (§5). Si cambia el significado de una columna, actualizar
# acá; si solo cambia el DDL, la introspección ya refleja la DB real.
_TABLE_DESCRIPTIONS: dict[str, str] = {
    "aerolineas": (
        "Compañías aéreas. `pais` es el país de la aerolínea, no el del "
        "destino del vuelo."
    ),
    "ciudades": (
        "Catálogo ciudad → país. `ciudad` es PRIMARY KEY y se usa para "
        "resolver el país de origen o destino de un vuelo."
    ),
    "vuelos": (
        "`origen` y `destino` son nombres de ciudad (VARCHAR), no países."
    ),
    "pasajeros": (
        "Pasajeros registrados. `pais_residencia` usa los mismos valores "
        "de país que `ciudades.pais`."
    ),
    "reservas": (
        "Reservas de un pasajero en un vuelo. `estado` es confirmada, "
        "cancelada o pendiente."
    ),
}

_COLUMN_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("aerolineas", "pais"): (
        "País de la aerolínea. No usar para filtrar país de origen o destino."
    ),
    ("ciudades", "ciudad"): (
        "Nombre de ciudad. Relación por igualdad de strings con "
        "vuelos.origen / vuelos.destino (no hay FK formal)."
    ),
    ("ciudades", "pais"): (
        "País de esa ciudad. Para país de destino: "
        "JOIN ciudades ON vuelos.destino = ciudades.ciudad."
    ),
    ("vuelos", "origen"): "Nombre de ciudad de origen, no país.",
    ("vuelos", "destino"): "Nombre de ciudad de destino, no país.",
    ("vuelos", "aerolinea_id"): "FK a aerolineas.id.",
    ("pasajeros", "pais_residencia"): (
        "País de residencia. Valores alineados a ciudades.pais / "
        "aerolineas.pais (p.ej. 'Estados Unidos', no 'EEUU')."
    ),
    ("reservas", "pasajero_id"): "FK a pasajeros.id.",
    ("reservas", "vuelo_id"): "FK a vuelos.id.",
    ("reservas", "estado"): "Uno de: confirmada, cancelada, pendiente.",
    ("reservas", "precio_pagado"): (
        "Monto pagado en la reserva (puede diferir del precio de lista)."
    ),
}

_HINTS: list[SchemaHintResponse] = [
    SchemaHintResponse(
        title="Ciudad vs país en vuelos",
        body=(
            "`origen` y `destino` son nombres de ciudad. No hay FK formal "
            "hacia `ciudades`: la relación es `vuelos.destino = ciudades.ciudad` "
            "(o `.origen`). Para filtrar por país de destino u origen hay que "
            "hacer `JOIN ciudades`."
        ),
    ),
    SchemaHintResponse(
        title="aerolineas.pais no es el país del vuelo",
        body=(
            "`aerolineas.pais` es el país de la compañía. No usarlo para el "
            "país de origen o destino."
        ),
    ),
    SchemaHintResponse(
        title="Valores de país alineados",
        body=(
            "`ciudades.pais`, `pasajeros.pais_residencia` y `aerolineas.pais` "
            "usan los mismos nombres (p.ej. Estados Unidos, no EEUU)."
        ),
    ),
]

# Plantillas genéricas para el frontend. No copiar preguntas del golden.
_PROMPT_SUGGESTIONS: list[str] = [
    "¿Cuántos vuelos hay con destino a …?",
    "¿Cuál es el precio promedio de vuelos hacia …?",
    "¿Qué pasajeros tienen reservas confirmadas?",
    "¿Cuántas reservas hay en estado …?",
    "¿Qué aerolíneas operan vuelos desde …?",
]

_LIMITATIONS: list[str] = [
    "No hay datos de clima, retrasos, cancelaciones operativas ni puntualidad.",
    "No hay equipaje, asiento asignado, clase (economy/business) ni millas.",
    "No hay escalas intermedias: cada fila de vuelos es un tramo origen→destino.",
    "No hay hora de salida/llegada; solo fecha y duración en minutos.",
    "No hay aeropuertos, terminales ni puertas: el grano geográfico es la ciudad.",
    "El modelo es sintético y de solo lectura: la API no inserta, actualiza ni borra.",
]


def build_schema_response(con: duckdb.DuckDBPyConnection) -> SchemaResponse:
    """Arma SchemaResponse a partir de una conexión ya abierta."""
    tables: list[SchemaTableResponse] = []
    for introspected in introspect_schema(con):
        tables.append(
            SchemaTableResponse(
                name=introspected.name,
                description=_TABLE_DESCRIPTIONS.get(introspected.name),
                columns=[
                    SchemaColumnResponse(
                        name=column.name,
                        data_type=column.data_type,
                        nullable=column.nullable,
                        is_primary_key=column.is_primary_key,
                        foreign_key=(
                            SchemaForeignKey(
                                table=column.foreign_key.table,
                                column=column.foreign_key.column,
                            )
                            if column.foreign_key
                            else None
                        ),
                        description=_COLUMN_DESCRIPTIONS.get(
                            (introspected.name, column.name)
                        ),
                    )
                    for column in introspected.columns
                ],
            )
        )
    return SchemaResponse(
        tables=tables,
        hints=list(_HINTS),
        prompt_suggestions=list(_PROMPT_SUGGESTIONS),
        limitations=list(_LIMITATIONS),
    )


def load_schema_catalog(db_path: str | None = None) -> SchemaResponse:
    """
    Abre DuckDB (misma ruta que /v1/ready) e introspecciona el schema.
    Lanza SchemaUnavailableError si no se puede leer.
    """
    try:
        con = open_connection_for_schema(db_path)
    except Exception as exc:
        raise SchemaUnavailableError(f"{type(exc).__name__}: {exc}") from exc
    try:
        return build_schema_response(con)
    except Exception as exc:
        raise SchemaUnavailableError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        con.close()
