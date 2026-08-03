"""
sql_generator.py

Agente 1: Generador de SQL. Ver docs/SPECS.md sección 3.

Modelo: sqlcoder, vía Ollama, local.
Salida estructurada con instructor + Pydantic (contrato en SPECS.md sección 6).

TODO (siguiente paso de desarrollo, ver docs/HISTORY.md):
    - Implementar SQLGenerationResult (Pydantic model)
    - Implementar generate_sql(question: str, schema: str) -> SQLGenerationResult
    - Conectar con cliente de Ollama vía instructor
"""

from pydantic import BaseModel


class SQLGenerationResult(BaseModel):
    sql: str
    explanation: str
    tables_used: list[str]
    columns_used: list[str]
    confidence_self_reported: int  # 1-5


def generate_sql(question: str, schema: str) -> SQLGenerationResult:
    """
    Genera una consulta SQL a partir de una pregunta en lenguaje natural
    y el schema relevante de la base de datos.
    """
    raise NotImplementedError("Pendiente de implementación")
