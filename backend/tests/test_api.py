"""Tests for authentication, rate limiting and the job queue."""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.config import Settings, get_settings, hash_key  # noqa: E402
from core.ratelimit import InMemoryRateLimiter  # noqa: E402
from services.job_queue import (  # noqa: E402
    AuditQueue,
    JobStatus,
    QueueFullError,
    TooManyJobsError,
)

SAMPLES = pathlib.Path(__file__).resolve().parents[2] / "samples"
GOOD_CSV = (SAMPLES / "gcp_billing_export.csv").read_bytes()

RAW_KEY = "cl_test_primary_key"
OTHER_KEY = "cl_test_other_key"


@pytest.fixture()
def client(monkeypatch):
    """A TestClient with auth on, a tiny rate limit, and the queue running."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CLOUDLEAK_API_KEY_HASHES", f"{hash_key(RAW_KEY)},{hash_key(OTHER_KEY)}")
    monkeypatch.setenv("CLOUDLEAK_RATE_LIMIT", "3")
    monkeypatch.setenv("CLOUDLEAK_RATE_LIMIT_WINDOW", "60")
    monkeypatch.setenv("CLOUDLEAK_MAX_JOBS_PER_KEY", "5")
    get_settings.cache_clear()

    import main

    with TestClient(main.app) as test_client:
        yield test_client

    get_settings.cache_clear()


def upload(client, key: str | None = RAW_KEY, filename: str = "export.csv", body: bytes = GOOD_CSV):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.post(
        "/api/v1/audit/upload",
        files={"file": (filename, body, "text/csv")},
        headers=headers,
    )


def poll(client, job_id: str, key: str = RAW_KEY, attempts: int = 60):
    """Poll until the job reaches a terminal state."""
    for _ in range(attempts):
        response = client.get(
            f"/api/v1/audit/jobs/{job_id}", headers={"Authorization": f"Bearer {key}"}
        )
        if response.status_code != 200:
            return response
        if response.json()["status"] in ("succeeded", "failed"):
            return response
    raise AssertionError("job never reached a terminal state")


# --- authentication -----------------------------------------------------------


def test_upload_requires_a_key(client):
    assert upload(client, key=None).status_code == 401


def test_upload_rejects_a_wrong_key(client):
    assert upload(client, key="cl_not_a_real_key").status_code == 401


def test_x_api_key_header_is_accepted(client):
    response = client.post(
        "/api/v1/audit/upload",
        files={"file": ("export.csv", GOOD_CSV, "text/csv")},
        headers={"X-API-Key": RAW_KEY},
    )
    assert response.status_code == 202


def test_job_polling_requires_a_key(client):
    job_id = upload(client).json()["job_id"]
    assert client.get(f"/api/v1/audit/jobs/{job_id}").status_code == 401


# --- ownership ----------------------------------------------------------------


def test_a_key_cannot_read_another_keys_job(client):
    """Cross-tenant read must 404, not 403 -- 403 confirms the id exists."""
    job_id = upload(client, key=RAW_KEY).json()["job_id"]
    response = client.get(
        f"/api/v1/audit/jobs/{job_id}", headers={"Authorization": f"Bearer {OTHER_KEY}"}
    )
    assert response.status_code == 404


def test_unknown_job_is_404(client):
    response = client.get(
        "/api/v1/audit/jobs/does-not-exist", headers={"Authorization": f"Bearer {RAW_KEY}"}
    )
    assert response.status_code == 404


# --- the async flow -----------------------------------------------------------


def test_upload_returns_202_with_a_job(client):
    response = upload(client)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["status_url"].endswith(body["job_id"])
    assert response.headers["Location"].endswith(body["job_id"])


def test_job_completes_and_carries_the_report(client):
    job_id = upload(client).json()["job_id"]
    body = poll(client, job_id).json()
    assert body["status"] == "succeeded"
    assert body["report"]["detected_provider"] == "GCP"
    assert body["report"]["metrics"]["identified_waste"] > 0
    assert body["duration_ms"] is not None


def test_a_bad_file_fails_the_job_without_a_500(client):
    job_id = upload(client, body=b"nothing,useful\n1,2\n").json()["job_id"]
    body = poll(client, job_id).json()
    assert body["status"] == "failed"
    assert body["error"]
    assert "Traceback" not in body["error"]


def test_empty_and_wrong_extension_fail_fast(client):
    assert upload(client, body=b"").status_code == 400
    assert upload(client, filename="report.pdf").status_code == 400


# --- rate limiting ------------------------------------------------------------


def test_rate_limit_returns_429_with_retry_after(client):
    statuses = [upload(client).status_code for _ in range(5)]
    assert statuses.count(202) == 3, statuses
    assert 429 in statuses

    limited = upload(client)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    assert limited.headers["X-RateLimit-Remaining"] == "0"


def test_rate_limit_is_per_key(client):
    for _ in range(3):
        upload(client, key=RAW_KEY)
    assert upload(client, key=RAW_KEY).status_code == 429
    # A different key has its own budget.
    assert upload(client, key=OTHER_KEY).status_code == 202


def test_successful_response_advertises_the_budget(client):
    response = upload(client)
    assert int(response.headers["X-RateLimit-Limit"]) == 3
    assert int(response.headers["X-RateLimit-Remaining"]) == 2


# --- limiter unit -------------------------------------------------------------


def test_sliding_window_allows_exactly_the_limit():
    async def scenario():
        limiter = InMemoryRateLimiter(limit=2, window_seconds=60)
        return [(await limiter.check("k")).allowed for _ in range(4)]

    assert asyncio.run(scenario()) == [True, True, False, False]


def test_limiter_window_expiry_restores_budget():
    async def scenario():
        limiter = InMemoryRateLimiter(limit=1, window_seconds=1)
        first = (await limiter.check("k")).allowed
        blocked = (await limiter.check("k")).allowed
        await asyncio.sleep(1.05)
        recovered = (await limiter.check("k")).allowed
        return first, blocked, recovered

    assert asyncio.run(scenario()) == (True, False, True)


# --- queue unit ---------------------------------------------------------------


def test_queue_rejects_when_full():
    async def scenario():
        queue = AuditQueue(worker_count=0, max_size=1, max_in_flight_per_key=99)
        await queue.submit("key", "a.csv", GOOD_CSV)
        with pytest.raises(QueueFullError):
            await queue.submit("key", "b.csv", GOOD_CSV)

    asyncio.run(scenario())


def test_queue_caps_jobs_in_flight_per_key():
    async def scenario():
        queue = AuditQueue(worker_count=0, max_size=99, max_in_flight_per_key=2)
        await queue.submit("key", "a.csv", GOOD_CSV)
        await queue.submit("key", "b.csv", GOOD_CSV)
        with pytest.raises(TooManyJobsError):
            await queue.submit("key", "c.csv", GOOD_CSV)
        # A different key is unaffected.
        await queue.submit("other", "d.csv", GOOD_CSV)

    asyncio.run(scenario())


def test_worker_drops_the_upload_after_processing():
    """A finished job must not keep the customer's billing file in memory."""

    async def scenario():
        queue = AuditQueue(worker_count=1, max_size=4)
        await queue.start()
        try:
            job = await queue.submit("key", "a.csv", GOOD_CSV)
            for _ in range(200):
                if job.is_terminal:
                    break
                await asyncio.sleep(0.05)
            assert job.status is JobStatus.SUCCEEDED
            assert job.payload is None
        finally:
            await queue.stop()

    asyncio.run(scenario())


# --- configuration safety -----------------------------------------------------


def test_production_without_keys_refuses_to_start(monkeypatch):
    monkeypatch.setenv("CLOUDLEAK_ENV", "production")
    monkeypatch.delenv("CLOUDLEAK_API_KEY_HASHES", raising=False)
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    import main

    with pytest.raises(main.UnsafeConfigurationError):
        with TestClient(main.app):
            pass

    get_settings.cache_clear()


def test_settings_flag_unsafe_production_config():
    problems = Settings(
        environment="production", allowed_origins=["*"], api_key_hashes=[]
    ).production_warnings()
    assert any("API_KEY_HASHES" in problem for problem in problems)
    assert any("wildcard" in problem for problem in problems)
