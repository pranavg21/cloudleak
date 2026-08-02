"""Runtime configuration, loaded once from the environment.

Defaults are chosen so `uvicorn main:app` works with no environment set up at
all. Every default that would be unsafe in production fails loudly instead of
silently degrading -- see `Settings.production_warnings`.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List


def _env_list(name: str, default: str = "") -> List[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def hash_key(raw_key: str) -> str:
    """Keys are compared as SHA-256 digests so plaintext never sits in memory."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Settings:
    environment: str = field(default_factory=lambda: os.getenv("CLOUDLEAK_ENV", "development"))

    allowed_origins: List[str] = field(
        default_factory=lambda: _env_list("CLOUDLEAK_ALLOWED_ORIGINS", "http://localhost:3000")
    )

    # Comma-separated SHA-256 digests of valid API keys. Plaintext keys are
    # never stored, so a leaked env file does not hand over working keys.
    api_key_hashes: List[str] = field(
        default_factory=lambda: _env_list("CLOUDLEAK_API_KEY_HASHES")
    )

    # Requests per window, per API key.
    rate_limit_requests: int = field(default_factory=lambda: _env_int("CLOUDLEAK_RATE_LIMIT", 10))
    rate_limit_window_seconds: int = field(
        default_factory=lambda: _env_int("CLOUDLEAK_RATE_LIMIT_WINDOW", 60)
    )
    # Concurrent audits one key may have queued or running at once.
    max_jobs_in_flight_per_key: int = field(
        default_factory=lambda: _env_int("CLOUDLEAK_MAX_JOBS_PER_KEY", 3)
    )

    # Queue sizing. Workers are threads because pandas parsing is CPU-bound and
    # would otherwise block the event loop.
    worker_count: int = field(default_factory=lambda: _env_int("CLOUDLEAK_WORKERS", 2))
    queue_max_size: int = field(default_factory=lambda: _env_int("CLOUDLEAK_QUEUE_SIZE", 64))
    job_timeout_seconds: int = field(default_factory=lambda: _env_int("CLOUDLEAK_JOB_TIMEOUT", 120))
    # Completed reports are dropped this long after they finish. Billing data
    # should not sit in memory indefinitely.
    job_result_ttl_seconds: int = field(default_factory=lambda: _env_int("CLOUDLEAK_JOB_TTL", 900))

    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("CLOUDLEAK_MAX_UPLOAD_MB", 150) * 1024 * 1024
    )

    redis_url: str = field(default_factory=lambda: os.getenv("CLOUDLEAK_REDIS_URL", ""))

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def auth_configured(self) -> bool:
        return bool(self.api_key_hashes)

    def production_warnings(self) -> List[str]:
        """Configuration that is fine locally and unacceptable in production."""
        problems: List[str] = []
        if not self.auth_configured:
            problems.append("CLOUDLEAK_API_KEY_HASHES is empty: the API would be unauthenticated.")
        if "*" in self.allowed_origins:
            problems.append("CLOUDLEAK_ALLOWED_ORIGINS contains a wildcard.")
        if not self.redis_url and self.worker_count > 0:
            problems.append(
                "CLOUDLEAK_REDIS_URL is unset: rate limits and job state are per-process "
                "and will not hold across more than one instance."
            )
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def generate_dev_key() -> tuple[str, str]:
    """Mint a development key. Returns (plaintext, sha256)."""
    raw = f"cl_dev_{secrets.token_urlsafe(24)}"
    return raw, hash_key(raw)
