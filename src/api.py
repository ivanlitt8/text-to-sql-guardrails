"""
api.py

Adaptador HTTP (FastAPI) sobre run_pipeline. Ver docs/SPECS.md §2 / §6.
No reorquesta: POST /v1/ask delega en el pipeline existente.
"""

from __future__ import annotations

import os
from typing import Any

import ollama
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from executor import open_connection_for_schema
from pipeline import FinalResponse, run_pipeline
from schema_catalog import SchemaResponse, SchemaUnavailableError, load_schema_catalog

load_dotenv()

_DEFAULT_FRONTEND_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]


def parse_frontend_origins(raw: str | None = None) -> list[str]:
    """Orígenes CORS. FRONTEND_ORIGIN admite una lista separada por comas."""
    value = raw if raw is not None else os.getenv("FRONTEND_ORIGIN")
    if value is None or not value.strip():
        return list(_DEFAULT_FRONTEND_ORIGINS)
    origins = [part.strip() for part in value.split(",") if part.strip()]
    return origins or list(_DEFAULT_FRONTEND_ORIGINS)


app = FastAPI(
    title="Text-to-SQL Vuelos",
    version="0.2.0",
    description=(
        "Pregunta libre en lenguaje natural → SQL + guardrails + juez. "
        "GET /v1/schema expone el catálogo para el frontend. "
        "La salida de POST /v1/ask es FinalResponse."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_frontend_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str = Field(min_length=1)

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question no puede estar vacía")
        return stripped


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    ready: bool
    checks: dict[str, bool]
    details: dict[str, str] | None = None


def _ollama_host() -> str:
    return os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def _generator_model() -> str:
    return os.getenv("GENERATOR_MODEL", "sqlcoder")


def _judge_model() -> str:
    return os.getenv("JUDGE_MODEL", "qwen2.5:3b")


def _model_base_name(name: str) -> str:
    return name.split(":")[0].lower()


def _listed_model_names(listed: Any) -> list[str]:
    models = (
        listed.get("models")
        if isinstance(listed, dict)
        else getattr(listed, "models", None)
    )
    names: list[str] = []
    for item in models or []:
        if isinstance(item, dict):
            raw = item.get("name") or item.get("model") or ""
        else:
            raw = getattr(item, "model", None) or getattr(item, "name", "") or ""
        if raw:
            names.append(str(raw))
    return names


def _model_is_present(names: list[str], wanted: str) -> bool:
    wanted_base = _model_base_name(wanted)
    for name in names:
        if _model_base_name(name) == wanted_base:
            return True
    return False


def check_readiness() -> ReadyResponse:
    """Chequeos baratos: DuckDB abre, Ollama responde, modelos listados."""
    checks = {
        "duckdb": False,
        "ollama": False,
        "generator_model": False,
        "judge_model": False,
    }
    details: dict[str, str] = {}

    try:
        con = open_connection_for_schema()
        try:
            con.execute("SELECT 1")
        finally:
            con.close()
        checks["duckdb"] = True
    except Exception as exc:
        details["duckdb"] = f"{type(exc).__name__}: {exc}"

    generator = _generator_model()
    judge = _judge_model()
    try:
        client = ollama.Client(host=_ollama_host(), timeout=5.0)
        names = _listed_model_names(client.list())
        checks["ollama"] = True
        checks["generator_model"] = _model_is_present(names, generator)
        checks["judge_model"] = _model_is_present(names, judge)
        if not checks["generator_model"]:
            details["generator_model"] = f"no listado: {generator}"
        if not checks["judge_model"]:
            details["judge_model"] = f"no listado: {judge}"
    except Exception as exc:
        details["ollama"] = f"{type(exc).__name__}: {exc}"

    return ReadyResponse(
        ready=all(checks.values()),
        checks=checks,
        details=details or None,
    )


@app.get("/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness: el proceso está vivo. No toca DuckDB ni Ollama."""
    return HealthResponse(status="ok")


@app.get("/v1/ready", response_model=ReadyResponse)
def ready() -> JSONResponse:
    """Readiness: ¿se puede preguntar ahora?"""
    payload = check_readiness()
    status_code = 200 if payload.ready else 503
    return JSONResponse(status_code=status_code, content=payload.model_dump())


@app.get("/v1/schema", response_model=SchemaResponse)
def schema() -> SchemaResponse | JSONResponse:
    """Catálogo para el frontend: introspección DuckDB + hints semánticos."""
    try:
        return load_schema_catalog()
    except SchemaUnavailableError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.post("/v1/ask", response_model=FinalResponse)
def ask(body: AskRequest) -> FinalResponse:
    """Pregunta libre → pipeline completo (FinalResponse)."""
    return run_pipeline(body.question)
