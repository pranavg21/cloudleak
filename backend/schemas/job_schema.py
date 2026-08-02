"""Response models for the asynchronous audit flow."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from schemas.focus_schema import AuditReportResponse

JobStatusLiteral = Literal["queued", "running", "succeeded", "failed"]


class JobAccepted(BaseModel):
    """Returned by POST /upload. The audit has not run yet."""

    job_id: str
    status: JobStatusLiteral = "queued"
    status_url: str = Field(..., description="Poll this until status is succeeded or failed")
    queue_depth: int = Field(0, description="Jobs waiting ahead of this one")
    poll_after_ms: int = Field(700, description="Suggested delay before the first poll")


class JobState(BaseModel):
    """Returned by GET /jobs/{job_id}."""

    job_id: str
    status: JobStatusLiteral
    filename: str
    queued_ms: int = Field(0, description="Time spent waiting for a worker")
    duration_ms: Optional[int] = Field(None, description="Processing time once started")
    report: Optional[AuditReportResponse] = Field(None, description="Present when succeeded")
    error: Optional[str] = Field(None, description="Present when failed")


class QueueHealth(BaseModel):
    status: str
    schema_version: str
    queue_depth: int
    workers: int
    auth_required: bool
