"""Background audit queue.

Parsing a large Cost and Usage Report is CPU-bound work measured in seconds.
Doing it inside the request handler holds a connection open, blocks the event
loop for the duration (pandas does not yield), and makes one large upload
enough to stall every other caller. So uploads are accepted, queued, and
answered with 202 plus a job URL the client polls.

Workers are threads, not tasks: `asyncio` gives no concurrency for CPU-bound
code, so the actual parse is dispatched to a thread pool. `worker_count`
therefore behaves like a concurrency limit on simultaneous audits.

The store is in-process. That is correct for a single instance and explicitly
wrong for several: a job submitted to instance A is invisible to instance B.
`Settings.production_warnings` says so at startup. The interface is narrow
enough that swapping in Redis or a real broker is a contained change.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

from schemas.focus_schema import AuditReportResponse
from services.audit_engine import execute_audit_engine
from services.parsers import UnreadableExportError, parse_and_normalize_csv

logger = logging.getLogger("cloudleak.queue")


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    owner_key_id: str
    filename: str
    status: JobStatus = JobStatus.QUEUED
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    report: Optional[AuditReportResponse] = None
    error: Optional[str] = None
    # Upload bytes are dropped the moment parsing finishes, so a completed job
    # never keeps the customer's billing file in memory.
    payload: Optional[bytes] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.SUCCEEDED, JobStatus.FAILED)


class QueueFullError(RuntimeError):
    """The queue is at capacity; the caller should retry later."""


class TooManyJobsError(RuntimeError):
    """This key already has the maximum number of audits in flight."""


def _run_audit(payload: bytes) -> AuditReportResponse:
    """The synchronous unit of work executed on a worker thread."""
    provider, confidence, df, rejected, currency, capabilities = parse_and_normalize_csv(payload)
    return execute_audit_engine(
        provider=provider,
        df=df,
        confidence=confidence,
        rejected_rows=rejected,
        currency=currency,
        capabilities=capabilities,
    )


class AuditQueue:
    """Bounded queue, fixed worker pool, TTL'd result store."""

    def __init__(
        self,
        worker_count: int = 2,
        max_size: int = 64,
        job_timeout_seconds: int = 120,
        result_ttl_seconds: int = 900,
        max_in_flight_per_key: int = 3,
    ) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_size)
        self._jobs: Dict[str, Job] = {}
        self._workers: list[asyncio.Task] = []
        self._reaper: Optional[asyncio.Task] = None
        self._worker_count = worker_count
        self._job_timeout = job_timeout_seconds
        self._result_ttl = result_ttl_seconds
        self._max_in_flight_per_key = max_in_flight_per_key
        self._lock = asyncio.Lock()
        self._running = False

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"cloudleak-worker-{index}")
            for index in range(self._worker_count)
        ]
        self._reaper = asyncio.create_task(self._reap_expired(), name="cloudleak-reaper")
        logger.info("Audit queue started with %d worker(s)", self._worker_count)

    async def stop(self) -> None:
        self._running = False
        for task in [*self._workers, self._reaper]:
            if task:
                task.cancel()
        await asyncio.gather(*[t for t in [*self._workers, self._reaper] if t], return_exceptions=True)
        self._workers.clear()
        self._jobs.clear()
        logger.info("Audit queue stopped")

    # --- submission ----------------------------------------------------------

    async def submit(self, owner_key_id: str, filename: str, payload: bytes) -> Job:
        async with self._lock:
            in_flight = sum(
                1
                for job in self._jobs.values()
                if job.owner_key_id == owner_key_id and not job.is_terminal
            )
            if in_flight >= self._max_in_flight_per_key:
                raise TooManyJobsError(
                    f"You already have {in_flight} audit(s) running. Wait for one to finish."
                )

            job = Job(id=secrets.token_urlsafe(18), owner_key_id=owner_key_id, filename=filename)
            job.payload = payload
            self._jobs[job.id] = job

        try:
            self._queue.put_nowait(job.id)
        except asyncio.QueueFull as exc:
            async with self._lock:
                self._jobs.pop(job.id, None)
            raise QueueFullError("The audit queue is at capacity.") from exc

        return job

    def get(self, job_id: str, owner_key_id: str) -> Optional[Job]:
        """Fetch a job, but only for the key that submitted it.

        Returning None rather than 403 for a mismatch keeps the endpoint from
        confirming that someone else's job id exists.
        """
        job = self._jobs.get(job_id)
        if job is None or job.owner_key_id != owner_key_id:
            return None
        return job

    def depth(self) -> int:
        return self._queue.qsize()

    # --- workers -------------------------------------------------------------

    async def _worker(self, index: int) -> None:
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                return

            job = self._jobs.get(job_id)
            if job is None:
                self._queue.task_done()
                continue

            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            payload = job.payload or b""

            try:
                # to_thread keeps pandas off the event loop; wait_for bounds a
                # pathological file so one upload cannot occupy a worker forever.
                job.report = await asyncio.wait_for(
                    loop.run_in_executor(None, _run_audit, payload),
                    timeout=self._job_timeout,
                )
                job.status = JobStatus.SUCCEEDED
            except asyncio.TimeoutError:
                job.status = JobStatus.FAILED
                job.error = (
                    f"The audit exceeded the {self._job_timeout}s limit. "
                    "Try a single billing period."
                )
            except UnreadableExportError as exc:
                job.status = JobStatus.FAILED
                job.error = str(exc)
            except asyncio.CancelledError:
                job.status = JobStatus.FAILED
                job.error = "The service shut down before this audit finished."
                raise
            except Exception:
                logger.exception("Audit job %s failed", job.id)
                job.status = JobStatus.FAILED
                job.error = "That file could not be parsed as a billing export."
            finally:
                job.finished_at = time.time()
                job.payload = None  # never retain the upload past processing
                self._queue.task_done()

    async def _reap_expired(self) -> None:
        """Drop finished jobs once their TTL passes."""
        interval = max(30, self._result_ttl // 4)
        while self._running:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            cutoff = time.time() - self._result_ttl
            async with self._lock:
                expired = [
                    job_id
                    for job_id, job in self._jobs.items()
                    if job.is_terminal and (job.finished_at or 0) < cutoff
                ]
                for job_id in expired:
                    del self._jobs[job_id]
            if expired:
                logger.info("Reaped %d expired job(s)", len(expired))
