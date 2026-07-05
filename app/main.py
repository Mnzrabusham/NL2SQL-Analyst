"""
API layer.

`create_app()` is a factory so tests can build isolated instances (temp
database, fake SQL generator) without touching module globals. The
module-level `app` at the bottom keeps `uvicorn app.main:app` working.
"""
from __future__ import annotations
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import QueryExecutionError, ensure_db, execute_readonly, get_schema_text
from .schemas import AskRequest, AskResponse, SchemaResponse
from .sql_generation import (
    ClaudeSqlGenerator,
    GenerationConfigError,
    GenerationUpstreamError,
    SqlGenerator,
)
from .validation import check_sql, normalize_sql

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, generator: SqlGenerator | None = None) -> FastAPI:
    settings = settings or Settings()
    generator = generator or ClaudeSqlGenerator(model=settings.model)

    ensure_db(settings.db_path)
    schema_text = get_schema_text(settings.db_path)

    app = FastAPI(
        title="NL2SQL Analyst API",
        description="Natural-language questions over a SQLite database, "
                    "with engine-enforced read-only execution.",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.settings = settings
    app.state.generator = generator

    @app.exception_handler(GenerationConfigError)
    async def config_error_handler(request: Request, exc: GenerationConfigError):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(GenerationUpstreamError)
    async def upstream_error_handler(request: Request, exc: GenerationUpstreamError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    def validate_and_execute(sql: str):
        """Returns (columns, rows, truncated) or raises ValueError(reason)."""
        ok, reason = check_sql(sql)
        if not ok:
            raise ValueError(reason)
        try:
            return execute_readonly(settings.db_path, sql, max_rows=settings.max_rows)
        except QueryExecutionError as exc:
            raise ValueError(str(exc)) from exc

    @app.post("/ask", response_model=AskResponse)
    def ask(req: AskRequest, request: Request):
        generator = request.app.state.generator
        sql = normalize_sql(generator.generate(req.question, schema_text))
        logger.info("Question %r -> SQL %r", req.question, sql)

        repaired = False
        try:
            columns, rows, truncated = validate_and_execute(sql)
        except ValueError as first_failure:
            # One-shot self-correction: feed the failure back to the model.
            sql = normalize_sql(
                generator.repair(req.question, schema_text, sql, str(first_failure))
            )
            logger.info("Repair attempt -> SQL %r", sql)
            try:
                columns, rows, truncated = validate_and_execute(sql)
                repaired = True
            except ValueError as second_failure:
                # Include the SQL so the UI can show what was rejected and why.
                raise HTTPException(400, {"reason": str(second_failure), "sql": sql})

        return AskResponse(
            sql=sql, columns=columns, rows=rows, row_count=len(rows),
            truncated=truncated, repaired=repaired,
        )

    @app.get("/schema", response_model=SchemaResponse)
    def schema():
        return SchemaResponse(schema_text=schema_text)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # Serve the reference UI at / so the demo is one command:
    # uvicorn app.main:app --port 8001 — API and frontend on the same origin.
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app


app = create_app()
