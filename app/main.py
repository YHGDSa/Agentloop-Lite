"""Application factory and the system route: GET /health.

create_app() takes an optional database path so tests can use a throwaway
SQLite file while uvicorn uses the path from settings. Startup also reconciles
runs left RUNNING by a previous process (see recover_orphaned_runs).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .db import connect, init_db
from .executor import recover_orphaned_runs
from .routes import router
from .schemas import HealthResponse

logger = logging.getLogger(__name__)


def create_app(db_path: str | None = None) -> FastAPI:
    """Build the FastAPI app; the schema is initialised on startup."""
    resolved_db_path = db_path or settings.db_path

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(app.state.db_path)
        conn = connect(app.state.db_path)
        try:
            recovered = recover_orphaned_runs(conn)
        finally:
            conn.close()
        logger.info("startup: marked %d orphaned RUNNING run(s) as FAILED", recovered)
        yield

    app = FastAPI(title="AgentLoop-lite", version="0.1.0", lifespan=lifespan)
    app.state.db_path = resolved_db_path

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(ok=True)

    app.include_router(router)

    return app


app = create_app()
