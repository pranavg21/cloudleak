"""CloudLeak API entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.config import generate_dev_key, get_settings
from core.ratelimit import build_rate_limiter
from core.security import register_ephemeral_key
from schemas.job_schema import QueueHealth
from services.job_queue import AuditQueue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cloudleak")


class UnsafeConfigurationError(RuntimeError):
    """Raised when production is asked to boot in an unsafe configuration."""


@asynccontextmanager
async def lifespan(app: FastAPI):
    current = get_settings()

    # Refuse to serve production traffic without authentication. A missing env
    # var must not degrade into a public endpoint.
    if current.is_production and not current.auth_configured:
        raise UnsafeConfigurationError(
            "CLOUDLEAK_ENV=production requires CLOUDLEAK_API_KEY_HASHES. "
            "Generate one with: python keygen.py"
        )

    for warning in current.production_warnings():
        logger.warning("Configuration: %s", warning)

    if not current.auth_configured and not current.is_production:
        raw, digest = generate_dev_key()
        register_ephemeral_key(digest)
        logger.warning(
            "No API keys configured. Minted a development key for this process only: %s", raw
        )
        app.state.dev_api_key = raw

    app.state.rate_limiter = await build_rate_limiter(
        limit=current.rate_limit_requests,
        window_seconds=current.rate_limit_window_seconds,
        redis_url=current.redis_url or None,
    )

    app.state.queue = AuditQueue(
        worker_count=current.worker_count,
        max_size=current.queue_max_size,
        job_timeout_seconds=current.job_timeout_seconds,
        result_ttl_seconds=current.job_result_ttl_seconds,
        max_in_flight_per_key=current.max_jobs_in_flight_per_key,
    )
    await app.state.queue.start()

    try:
        yield
    finally:
        await app.state.queue.stop()
        await app.state.rate_limiter.close()


settings = get_settings()

app = FastAPI(
    title="CloudLeak",
    version="1.1.0",
    description=(
        "Zero-IAM multi-cloud cost leak detection. Upload a billing export, get a "
        "normalized waste audit and reviewable cleanup commands. "
        "Audits run asynchronously: POST an export, then poll the returned job."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
    expose_headers=["Location", "Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
)

from routers.audit import router as audit_router  # noqa: E402

app.include_router(audit_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never return a stack trace to a caller."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong handling that request."},
    )


@app.get("/health", response_model=QueueHealth, tags=["Meta"], summary="Health check")
def health_check(request: Request) -> QueueHealth:
    queue = getattr(request.app.state, "queue", None)
    current = get_settings()
    return QueueHealth(
        status="ok",
        schema_version="cloudleak-internal-v1",
        queue_depth=queue.depth() if queue else 0,
        workers=current.worker_count,
        auth_required=current.auth_configured or hasattr(request.app.state, "dev_api_key"),
    )
