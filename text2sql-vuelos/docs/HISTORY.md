# HISTORY.md — Registro de cambios

> Este archivo es el historial cronológico del proyecto. Cada vez que se
> hace un cambio relevante (nueva funcionalidad, decisión de arquitectura,
> fix importante, cambio de modelo, etc.), se agrega una entrada nueva
> **arriba de las anteriores** (orden descendente, la más reciente primero).

## [2026-08-03] Benchmark manual: elección del modelo juez

**Qué se hizo:**
Se comparó `llama3.2:3b` vs `qwen2.5:3b` como candidatos a juez, con dos
pruebas manuales vía `ollama run`: (1) inferir la pregunta a partir de una
consulta SQL válida sobre `vuelos`, y (2) el mismo ejercicio sobre una
consulta con una tabla inexistente (`sqlite.vuelos`, alucinada por
`sqlcoder` en una prueba anterior). Se midió tiempo de respuesta en frío
(primera carga del modelo) y en caliente (modelo ya cargado en RAM).

Resultados:
| Criterio | llama3.2:3b | qwen2.5:3b |
|---|---|---|
| Latencia en frío | 9.18s | 6.88s |
| Latencia en caliente | 1.66s | 1.22s |
| Calidad de inferencia | Buena, genérica | Buena, más precisa (captó que se pedían todos los campos, no solo existencia) |
| Detectó la tabla inválida `sqlite.vuelos` sin que se le pidiera | No | No |

**Se define `qwen2.5:3b` como modelo juez para el MVP.**

También se confirmó que la primera corrida de cualquier modelo en Ollama
es notablemente más lenta (carga en RAM desde disco) que las corridas
siguientes con el modelo ya cargado ("cold start" vs "warm"). Esto hay
que tenerlo en cuenta al medir latencia end-to-end del pipeline completo,
no solo latencia de un modelo aislado.

**Por qué:**
Ninguno de los dos modelos detectó el problema de la tabla inexistente,
lo cual confirma un límite de diseño esperado: el juez está pensado para
detectar desalineación semántica (¿la SQL responde la pregunta correcta?),
no errores de sintaxis o referencias a tablas que no existen en el schema.
Esa responsabilidad queda del lado de `guardrails.py` (validación
sintáctica con `sqlparse`) y/o de una validación adicional que compare
`tables_used` del `SQLGenerationResult` contra el schema real — no es
trabajo del juez. Se documenta acá para no reasignarle esa responsabilidad
por error más adelante.

Se eligió Qwen por ganar en los dos criterios donde hubo diferencia
medible (velocidad y precisión de la inferencia), con dos pruebas
consistentes en la misma dirección. Se considera suficiente para una
decisión de MVP; se puede revisar con más datos si en el uso real da
señales de bajo rendimiento.

**Cómo:**
Pruebas manuales vía `ollama run <modelo> "<prompt>"`, cronometradas a
mano. No se automatizó el benchmark todavía (podría formalizarse más
adelante como script en `eval/` si hace falta comparar más modelos).

**Próximos pasos:**
1. Actualizar `.env.example` y `.env` con `JUDGE_MODEL=qwen2.5:3b`
2. Implementar `src/sql_generator.py` (ver entrada anterior de HISTORY.md
   para el detalle del prompt Defog y `temperature=0`)
3. Implementar `src/guardrails.py`, incluyendo validación de que las
   tablas referenciadas en el SQL generado existan en el schema real
   (hallazgo de esta entrada: el juez no cubre ese caso)

## [2026-08-03] Implementación de schema_extractor (DDL vía introspección)

**Qué se hizo:**
Se implementó `extract_schema(con)` en `src/schema_extractor.py`:
introspecta DuckDB con `duckdb_tables()`, `duckdb_columns()` y
`duckdb_constraints()`, y reconstruye el schema como sentencias
`CREATE TABLE` (DDL), incluyendo PRIMARY KEY, FOREIGN KEY inline
(`REFERENCES ...`) y CHECK. Se agregó `tests/test_schema_extractor.py`
(tablas/columnas, FKs, y que el DDL salga de la conexión viva, no de
`data/schema.sql`).

**Por qué:**
Las pruebas manuales con sqlcoder mostraron que el modelo responde bien
cuando el schema se pega como DDL real, no como descripción en prosa.
Generar ese DDL desde la conexión (y no leer el archivo estático) evita
desalineación si el schema en memoria cambia y mantiene una sola fuente
de verdad: la base cargada.

**Cómo:**
- `src/schema_extractor.py`: helpers de introspección y armado de
  `CREATE TABLE` por tabla, en orden de `table_oid`.
- `tests/test_schema_extractor.py`: fixture en memoria con
  `schema.sql` + `seed_data.sql`; import vía `sys.path` a `src/`.
- No se modificaron `guardrails.py`, `sql_generator.py` ni `judge.py`.

**Próximos pasos:**
1. Implementar `src/sql_generator.py` con el prompt oficial Defog
   documentado en la entrada anterior (`### Task` /
   `### Database Schema` / `### Answer` + fence sql), inyectando el DDL
   que produce `extract_schema()`, y con `temperature=0`.
2. Comparar `llama3.2:3b` vs `qwen2.5:3b` como juez y medir tiempos de
   forma sistemática; documentar la elección acá y en SPECS.md §10.

## [2026-08-03] Pruebas iniciales de Ollama y prompt de sqlcoder

**Qué se hizo:**
Se instaló Ollama y se validó que corre en el hardware del desarrollador.
Se probaron generaciones con `sqlcoder` contra la base de vuelos: sin
schema en el prompt, el modelo alucinaba nombres de tabla; con el formato
oficial de Defog (`### Task` / `### Database Schema` / `### Answer`,
cerrando con un fence de código marcado como sql), produce SQL correcto
usando el schema real. También se observó variabilidad entre corridas
(a veces `=`, a veces `ILIKE`, y en una ocasión alucinó el prefijo
`sqlite.vuelos`). Queda pendiente fijar `temperature=0` en
`sql_generator.py` para reducir esa inestabilidad. Aún no se comparó de
forma sistemática `llama3.2:3b` vs `qwen2.5:3b` como juez, ni se midieron
latencias de punta a punta.

**Por qué:**
Antes de implementar el pipeline en código, hacía falta confirmar que
Ollama + `sqlcoder` son usables en la notebook (Ryzen 7 7730U, 16 GB RAM,
sin GPU dedicada) y descubrir qué formato de prompt realmente funciona.
El hallazgo del prompt Defog es una decisión de diseño: no conviene un
prompt genérico si el modelo está fine-tuneado para ese template. La
variabilidad observada justifica forzar `temperature=0` en el generador
desde el primer commit de implementación.

**Cómo:**
Pruebas manuales vía Ollama (sin cambios aún en `src/`). El schema se
inyectó en el formato Defog y se iteró la misma pregunta varias veces
para detectar no-determinismo. No se actualizó `.env` ni se eligió el
modelo juez definitivo (sigue el default `llama3.2:3b` en `.env.example`
como candidato provisional).

**Próximos pasos:**
1. Fijar `temperature=0` al implementar `sql_generator.py`, usando el
   prompt oficial Defog (`### Task` / `### Database Schema` / `### Answer`
   + fence de código sql)
2. Comparar `llama3.2:3b` vs `qwen2.5:3b` como juez y medir tiempos de
   forma sistemática; documentar la elección acá y en SPECS.md §10
3. Implementar `schema_extractor.py` y luego `sql_generator.py` con
   salida estructurada vía `instructor` + Pydantic

## [2026-08-02] Boilerplate inicial del proyecto

**Qué se hizo:**
Se creó la estructura de carpetas base del proyecto (`data/`, `src/`,
`eval/`, `tests/`, `docs/`), junto con los archivos `SPECS.md` (fuente de
verdad técnica), este `HISTORY.md`, `requirements.txt`, `README.md`,
`.gitignore`, y los stubs vacíos de los módulos principales en `src/`.

**Por qué:**
Se decidió separar la documentación de arquitectura (SPECS.md, que no
cambia salvo decisión explícita) del registro histórico de cambios
(HISTORY.md, que crece con cada modificación). Esto le da a Cursor un
punto de verdad estable para consultar reglas del proyecto, y un log
para entender el contexto de por qué el código llegó a su estado actual.

**Cómo:**
Estructura de carpetas creada manualmente. Se definió Python 3.11+,
DuckDB como motor de base de datos, Ollama para correr los modelos
localmente (`sqlcoder` como generador, `llama3.2:3b` o `qwen2.5:3b` como
juez, pendiente de confirmar cuál rinde mejor en el hardware disponible).

**Próximos pasos:**
1. Instalar Ollama y bajar los modelos (`sqlcoder`, `llama3.2:3b`, `qwen2.5:3b`)
2. Correr una prueba de velocidad simple en la notebook del desarrollador
3. Crear el entorno virtual de Python e instalar `requirements.txt`
4. Cargar `schema.sql` + `seed_data.sql` en DuckDB y validar con una query
   manual (sin LLM todavía) que la conexión funciona
5. Recién después de validar el punto anterior, empezar con
   `schema_extractor.py` y `sql_generator.py`
