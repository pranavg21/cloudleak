"""Audit submission and job polling."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status

from core.config import Settings, get_settings
from core.security import Principal, require_api_key
from schemas.focus_schema import ErrorResponse
from schemas.job_schema import JobAccepted, JobState
from services.job_queue import AuditQueue, JobStatus, QueueFullError, TooManyJobsError

logger = logging.getLogger("cloudleak.audit")

router = APIRouter(prefix="/api/v1/audit", tags=["Audit engine"])

ACCEPTED_SUFFIXES = (".csv", ".txt")


def get_queue(request: Request) -> AuditQueue:
    queue = getattr(request.app.state, "queue", None)
    if queue is None:  # pragma: no cover - only if lifespan did not run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The audit service is still starting. Try again shortly.",
        )
    return queue


async def enforce_rate_limit(request: Request, response: Response, principal: Principal) -> None:
    """Apply the per-key limit and always report the budget back to the client."""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return

    verdict = await limiter.check(principal.key_id)
    response.headers["X-RateLimit-Limit"] = str(verdict.limit)
    response.headers["X-RateLimit-Remaining"] = str(verdict.remaining)

    if not verdict.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit reached: {verdict.limit} audits per window. "
                f"Try again in {verdict.retry_after_seconds}s."
            ),
            headers={
                "Retry-After": str(verdict.retry_after_seconds),
                "X-RateLimit-Limit": str(verdict.limit),
                "X-RateLimit-Remaining": "0",
            },
        )


@router.post(
    "/upload",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Queue a billing export for audit",
)
async def upload_cost_export(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    principal: Principal = Depends(require_api_key),
    settings: Settings = Depends(get_settings),
    queue: AuditQueue = Depends(get_queue),
) -> JobAccepted:
    """Accept an export and queue it. Returns 202 with a job to poll.

    The file is validated synchronously so obvious mistakes fail immediately,
    then handed to a worker. It is held in memory only until parsing finishes.
    """
    await enforce_rate_limit(request, response, principal)

    filename = file.filename or "upload"
    if not filename.lower().endswith(ACCEPTED_SUFFIXES):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "CloudLeak reads .csv billing exports. Export the raw file from your "
                "billing console and upload that."
            ),
        )

    # Read with a hard ceiling rather than trusting Content-Length, which a
    # client controls. Stop at one byte over the limit instead of buffering
    # an arbitrarily large body first.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"That file is larger than the "
                    f"{settings.max_upload_bytes // (1024 * 1024)} MB limit. "
                    "Export a single billing period and try again."
                ),
            )
        chunks.append(chunk)

    payload = b"".join(chunks)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That file is empty.",
        )

    try:
        job = await queue.submit(principal.key_id, filename, payload)
    except TooManyJobsError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": "10"},
        ) from exc
    except QueueFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The audit queue is full. Try again in a moment.",
            headers={"Retry-After": "15"},
        ) from exc

    response.headers["Location"] = f"/api/v1/audit/jobs/{job.id}"
    return JobAccepted(
        job_id=job.id,
        status="queued",
        status_url=f"/api/v1/audit/jobs/{job.id}",
        queue_depth=queue.depth(),
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobState,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Check an audit job",
)
async def get_job(
    job_id: str,
    principal: Principal = Depends(require_api_key),
    queue: AuditQueue = Depends(get_queue),
) -> JobState:
    """Return job state, including the report once it succeeds.

    Only the key that submitted a job can read it. An unknown id and someone
    else's id both return 404, so this cannot be used to probe for job ids.
    """
    job = queue.get(job_id, principal.key_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such audit. It may have expired -- reports are kept briefly, then dropped.",
        )

    now = time.time()
    queued_ms = int(((job.started_at or now) - job.submitted_at) * 1000)
    duration_ms = (
        int(((job.finished_at or now) - job.started_at) * 1000) if job.started_at else None
    )

    return JobState(
        job_id=job.id,
        status=job.status.value if isinstance(job.status, JobStatus) else str(job.status),
        filename=job.filename,
        queued_ms=max(0, queued_ms),
        duration_ms=duration_ms,
        report=job.report,
        error=job.error,
    )
