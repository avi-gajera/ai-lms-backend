"""FastAPI application entry point: `uvicorn app.main:app`."""

import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from sqlalchemy import text

from app.api import assessments, attempts, tasks, videos
from app.config import PROJECT_ROOT, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, request_id_var, setup_worker_logging
from app.core.middleware import BodySizeLimitMiddleware
from app.db.session import engine
from app.schemas.api import HealthOut

logger = get_logger(__name__)

# A client-supplied X-Request-ID is echoed in headers and written to every log line, so only a short
# id-like value is trusted; anything else (huge, spaces, control characters) gets a fresh uuid.
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.attributes["configure_logger"] = False  # keep the app's logging config
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_worker_logging()
    s = get_settings()
    Path(s.video_storage_dir).mkdir(parents=True, exist_ok=True)
    if s.auto_migrate:
        run_migrations()
    logger.info(
        "api started",
        extra={
            "task_backend": s.task_backend,
            "llm_provider": s.llm_provider,
            "qdrant": s.qdrant_url or s.qdrant_path,
            "db": s.database_url.split("://")[0],
        },
    )
    yield


app = FastAPI(
    title="AI-Powered LMS API",
    version="1.0.0",
    description=(
        "Backend for an AI-powered Learning Management System: video ingestion and transcription, "
        "retrieval over transcripts (RAG), assessment generation, automatic answer evaluation and "
        "learning reports.\n\n"
        "**Typical flow:** `POST /videos` → poll `GET /videos/{id}` until `indexed` → "
        "`POST /videos/{id}/progress` (≥ threshold) → `POST /assessments` → `POST /attempts` → "
        "`GET /attempts/{id}/report`.\n\n"
        'Errors use one envelope: `{"error": {"code", "message", "request_id", "details"}}`.'
    ),
    lifespan=lifespan,
)
register_exception_handlers(app)

# Added before `request_context` so it runs inside it: a 413 still carries a request id and is logged.
# Headroom of 1 MB over MAX_UPLOAD_MB for the multipart framing and form fields around the file.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=lambda: (get_settings().max_upload_mb + 1) * 1024 * 1024)


@app.middleware("http")
async def request_context(request: Request, call_next):
    incoming = request.headers.get("X-Request-ID", "")
    rid = incoming if _REQUEST_ID.fullmatch(incoming) else str(uuid.uuid4())
    token = request_id_var.set(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )
        return response
    finally:
        request_id_var.reset(token)


for r in (videos.router, assessments.router, attempts.router, tasks.router):
    app.include_router(r)


@app.get("/health", response_model=HealthOut, tags=["health"], summary="Liveness + dependency checks")
def health() -> HealthOut:
    s = get_settings()
    checks: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["database"] = f"error: {exc}"
    try:
        from app.services.retrieval import get_client

        get_client().get_collections()
        checks["vector_store"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["vector_store"] = f"error: {exc}"
    if s.task_backend == "celery":
        try:
            import redis

            redis.Redis.from_url(s.redis_url, socket_timeout=2).ping()
            checks["broker"] = "ok"
        except Exception as exc:  # pragma: no cover
            checks["broker"] = f"error: {exc}"
    ok = all(v == "ok" for v in checks.values())
    return HealthOut(
        status="ok" if ok else "degraded", checks=checks, task_backend=s.task_backend, llm_provider=s.llm_provider
    )
