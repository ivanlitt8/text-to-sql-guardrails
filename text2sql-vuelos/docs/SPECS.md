# SPECS.md — Fuente de verdad técnica del proyecto

> Este documento es la referencia técnica principal. Cualquier decisión de
> arquitectura, tecnología o funcionalidad debe alinearse con lo que está
> escrito acá. Si algo no está definido acá, se define primero acá y después
> se implementa.

---

## 1. Objetivo del proyecto

Construir una interfaz en lenguaje natural que traduce preguntas en español
o inglés a consultas SQL ejecutables contra una base de datos de vuelos,
con guardrails de seguridad y un segundo modelo que verifica que la consulta
generada realmente responda la pregunta original (detección de alucinaciones).

Este es un proyecto de portafolio para transición a AI Engineering. La
prioridad es demostrar buenas decisiones de arquitectura, no solo que
"funcione".

---

## 2. Alcance del MVP (v1)

Incluye:
- Base de datos de vuelos en DuckDB (4 tablas, datos sintéticos)
- Generación de SQL a partir de lenguaje natural (1 modelo local)
- Guardrails: bloqueo de operaciones destructivas (INSERT/UPDATE/DELETE/DROP/ALTER)
- Verificación con un segundo modelo ("juez") que detecta desalineación
  entre la pregunta y el SQL generado
- Golden dataset inicial de 15-20 preguntas con SQL esperado
- Ejecución vía script de línea de comandos (sin API ni UI todavía)

No incluye todavía (fases futuras, no tocar sin actualizar este doc primero):
- FastAPI / endpoints HTTP
- Frontend (Streamlit/React)
- Multi-query validation (dos approaches distintos comparados)
- Esquemas de bases de datos más grandes / filtrado por relevancia semántica
- Docker

---

## 3. Arquitectura: agentes del sistema

El sistema tiene **dos agentes**, ambos modelos de lenguaje corriendo
localmente vía Ollama. No hay llamadas a APIs de pago en el MVP.

### Agente 1 — Generador de SQL
- **Modelo:** `sqlcoder` (Defog AI, vía Ollama)
- **Rol:** recibe la pregunta en lenguaje natural + el esquema relevante,
  devuelve una consulta SQL + explicación en lenguaje natural + lista de
  tablas/columnas usadas.
- **Por qué este modelo:** está fine-tuneado específicamente para Text-to-SQL,
  a diferencia de un modelo generalista.
- **Salida estructurada:** vía `instructor` + Pydantic (ver sección 5).

### Agente 2 — Juez / verificador
<!-- - **Modelo:** `llama3.2:3b` (o `qwen2.5:3b`, evaluar cuál rinde mejor en
  pruebas locales — documentar la decisión en HISTORY.md cuando se elija) -->
- **Modelo:** `qwen2.5:3b` (decisión tomada tras benchmark manual, ver
  HISTORY.md sección "Benchmark manual: elección del modelo juez")
- **Rol:** recibe la SQL generada (sin el resultado de ejecución) y responde
  "¿qué pregunta responde esta consulta?". Se compara esa respuesta contra
  la pregunta original para detectar desalineación.
- **Por qué un modelo distinto al generador:** evita que el mismo modelo
  valide su propio trabajo con el mismo sesgo. Un modelo generalista chico
  alcanza para esta tarea de comprensión, no necesita ser experto en SQL.

### Lo que NO es un agente
Los guardrails (bloqueo de queries destructivas, límite de filas, etc.) son
código determinístico, sin LLM de por medio. No confundir con un "agente".

---

## 4. Stack tecnológico

| Componente | Elección | Motivo |
|---|---|---|
| Lenguaje | Python 3.11+ | Estándar del ecosistema |
| Base de datos | DuckDB | Embebida, sin servidor, soporta SQL estándar |
| Runtime de modelos | Ollama | Gratis, local, fácil de instalar |
| Generador SQL | `sqlcoder` | Especializado en Text-to-SQL |
| Juez | `llama3.2:3b` o `qwen2.5:3b` | Liviano, generalista, alcanza para verificación |
| Salida estructurada | `instructor` + `pydantic` | Fuerza schema tipado en la salida del LLM |
| Validación SQL | `sqlparse` | Parseo sintáctico antes de ejecutar |
| Testing | `pytest` | Estándar |

---

## 5. Esquema de datos (DuckDB)

Ver `data/schema.sql` para el DDL completo y `data/seed_data.sql` para los
datos de ejemplo. Resumen de tablas:

- `aerolineas(id, nombre, pais)`
- `vuelos(id, aerolinea_id, origen, destino, fecha, duracion_minutos, precio, asientos_disponibles)`
- `pasajeros(id, nombre, email, pais_residencia)`
- `reservas(id, pasajero_id, vuelo_id, fecha_reserva, precio_pagado, estado)`

---

## 6. Modelos de datos (Pydantic) — contratos esperados

```python
class SQLGenerationResult(BaseModel):
    sql: str
    explanation: str
    tables_used: list[str]
    columns_used: list[str]
    confidence_self_reported: int  # 1-5

class JudgeVerdict(BaseModel):
    inferred_question: str
    alignment_score: int  # 1-5
    concerns: list[str]

class GuardrailResult(BaseModel):
    is_safe: bool
    blocked_reason: str | None
    query_type: str  # SELECT, INSERT, DELETE, etc.

class FinalResponse(BaseModel):
    question: str
    sql: str
    executed: bool
    results: list[dict] | None
    confidence_final: float  # 0-1, compuesto
    guardrail_status: GuardrailResult
    judge_verdict: JudgeVerdict
```

Estos son los contratos de referencia. Si se modifican, actualizar esta
sección antes de tocar el código.

---

## 7. Reglas de guardrails (v1)

Bloquear si la consulta contiene, en cualquier forma (case-insensitive):
- `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `GRANT`

Forzar:
- Si no hay `LIMIT`, agregar `LIMIT 1000` por defecto
- Rechazar si hay subqueries anidadas de más de 3 niveles (v1: chequeo simple
  por conteo de palabras clave `SELECT`, ajustar si da falsos positivos)

Ejecutar siempre en modo solo-lectura (transacción con rollback automático).

---

## 8. Golden dataset — formato

Archivo: `eval/golden_dataset.json`. Cada caso:

```json
{
  "id": "case_001",
  "question": "¿Cuáles fueron los 5 destinos más reservados el último trimestre?",
  "expected_sql": "SELECT ...",
  "difficulty": "moderate",
  "notes": "Requiere JOIN + filtro de fecha + agregación + LIMIT"
}
```

Niveles de dificultad válidos: `simple`, `moderate`, `hard`, `adversarial`
(este último para probar guardrails, no precisión de SQL).

---

## 9. Convenciones de código

- Todo el código en `src/`, sin lógica de negocio en `main.py` (solo
  orquestación)
- Tipado con Pydantic en todas las fronteras de datos (entrada/salida de LLM)
- Variables de entorno en `.env` (no versionar), ver `.env.example`
- Nombres de archivos y funciones en inglés, comentarios y docstrings en
  español (consistencia con el resto de la documentación del proyecto)

---

## 10. Decisiones tomadas

- [x] **Modelo juez: `qwen2.5:3b`.** Elegido sobre `llama3.2:3b` por
  mejor latencia (~25-30% más rápido, tanto en frío como en caliente) y
  mayor precisión en la inferencia de la pregunta original a partir del
  SQL. Ver benchmark completo en `docs/HISTORY.md`, entrada
  "Benchmark manual: elección del modelo juez".

- [x] **Límite de responsabilidad del juez.** El juez detecta
  desalineación semántica (¿el SQL responde la pregunta correcta?), pero
  NO detecta tablas o columnas inexistentes en el schema real — eso es
  responsabilidad de `guardrails.py`. Confirmado empíricamente: ni Llama
  ni Qwen señalaron una tabla inventada (`sqlite.vuelos`) sin que se les
  pidiera explícitamente revisarla.
---

*Última actualización: ver HISTORY.md para el registro cronológico de cambios
a este documento.*
