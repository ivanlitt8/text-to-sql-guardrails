# Text-to-SQL con Guardrails y Detección de Alucinaciones

Sistema que traduce preguntas en lenguaje natural a consultas SQL sobre una
base de datos de vuelos, con guardrails de seguridad y un segundo modelo que
verifica que la consulta generada realmente responda la pregunta original.

Ver `docs/SPECS.md` para el detalle técnico completo y `docs/HISTORY.md`
para el registro cronológico de decisiones y cambios.

## Requisitos

- Python 3.11+
- [Ollama](https://ollama.com) instalado localmente

## Setup

### 1. Instalar Ollama y bajar los modelos

```bash
ollama pull sqlcoder
ollama pull llama3.2:3b
ollama pull qwen2.5:3b
```

(Se bajan ambos candidatos a "juez" para comparar cuál rinde mejor en tu
hardware — ver docs/SPECS.md sección 10)

### 2. Crear entorno virtual e instalar dependencias

```bash
python3 -m venv .venv
source .venv/bin/activate   # En Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configurar variables de entorno

```bash
cp .env.example .env
```

### 4. Cargar la base de datos de vuelos

```bash
python3 -c "
import duckdb
con = duckdb.connect('data/vuelos.duckdb')
con.execute(open('data/schema.sql').read())
con.execute(open('data/seed_data.sql').read())
print('Base cargada OK')
"
```

Si necesitás regenerar los datos sintéticos (por ejemplo, con más volumen):

```bash
cd data && python3 generate_seed_data.py
```

## Estructura del proyecto

```
text2sql-vuelos/
├── data/                   # Schema, datos sintéticos y generador
├── src/                    # Código fuente (ver docs/SPECS.md sección 9)
├── eval/                   # Golden dataset para evaluación
├── tests/                  # Tests unitarios
├── docs/
│   ├── SPECS.md            # Fuente de verdad técnica del proyecto
│   └── HISTORY.md          # Registro cronológico de cambios
├── requirements.txt
└── .env.example
```

## CLI

```bash
python main.py "¿Cuántos vuelos hay con destino a Madrid?"
```

## API HTTP (FastAPI)

```bash
uvicorn api:app --app-dir src --host 127.0.0.1 --port 8000
```

- `GET /v1/health` — proceso vivo
- `GET /v1/ready` — DuckDB + Ollama + modelos
- `POST /v1/ask` — `{ "question": "…" }` → `FinalResponse` (puede tardar minutos)

```bash
curl http://127.0.0.1:8000/v1/health
curl http://127.0.0.1:8000/v1/ready
curl -X POST http://127.0.0.1:8000/v1/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"¿Cuántos vuelos hay con destino a Madrid?\"}"
```

Docs interactivas: http://127.0.0.1:8000/docs

## Estado actual

Pipeline CLI + API HTTP. Golden ~87.5% (ver `docs/HISTORY.md`).
Frontend todavía fuera de alcance (SPECS §2).
