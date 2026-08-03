"""
judge.py

Agente 2: Juez / verificador. Ver docs/SPECS.md sección 3.

Modelo: llama3.2:3b o qwen2.5:3b (pendiente de confirmar cuál se usa,
ver docs/SPECS.md sección 10), vía Ollama, local.

Su tarea: dada una consulta SQL, inferir qué pregunta responde, y comparar
esa pregunta inferida contra la pregunta original del usuario para detectar
desalineación (posible alucinación del generador).

TODO (siguiente paso de desarrollo, ver docs/HISTORY.md):
    - Implementar JudgeVerdict (Pydantic model)
    - Implementar judge_sql(original_question: str, sql: str) -> JudgeVerdict
"""

from pydantic import BaseModel


class JudgeVerdict(BaseModel):
    inferred_question: str
    alignment_score: int  # 1-5
    concerns: list[str]


def judge_sql(original_question: str, sql: str) -> JudgeVerdict:
    """
    Evalúa si la SQL generada responde realmente a la pregunta original.
    """
    raise NotImplementedError("Pendiente de implementación")
