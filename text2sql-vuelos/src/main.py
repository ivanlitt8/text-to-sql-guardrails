"""
main.py

Punto de entrada / orquestador del pipeline. Solo coordina, sin lógica
de negocio propia (ver docs/SPECS.md sección 9, convenciones de código).

Flujo esperado (v1):
    1. Conectar a DuckDB
    2. Extraer schema (schema_extractor.py)
    3. Generar SQL a partir de la pregunta (sql_generator.py)
    4. Chequear guardrails (guardrails.py)
    5. Ejecutar en modo solo-lectura si pasa los guardrails
    6. Verificar con el juez (judge.py)
    7. Devolver FinalResponse (ver SPECS.md sección 6)

TODO: implementar recién después de validar que schema_extractor,
sql_generator, judge y guardrails funcionan individualmente.
"""


def main():
    raise NotImplementedError("Pendiente de implementación")


if __name__ == "__main__":
    main()
