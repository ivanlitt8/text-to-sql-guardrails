"""
guardrails.py

Capa de seguridad determinística (SIN LLM). Ver docs/SPECS.md sección 7
para la lista completa de reglas.

TODO (siguiente paso de desarrollo, ver docs/HISTORY.md):
    - Implementar GuardrailResult (Pydantic model)
    - Implementar check_query(sql: str) -> GuardrailResult
    - Implementar enforce_limit(sql: str) -> str (agrega LIMIT 1000 si falta)
"""

from pydantic import BaseModel

BLOCKED_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "GRANT",
]


class GuardrailResult(BaseModel):
    is_safe: bool
    blocked_reason: str | None
    query_type: str


def check_query(sql: str) -> GuardrailResult:
    """
    Verifica que la consulta no contenga operaciones destructivas ni
    viole las reglas de seguridad definidas en SPECS.md sección 7.
    """
    raise NotImplementedError("Pendiente de implementación")
