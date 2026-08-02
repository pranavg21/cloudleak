"""API key authentication.

Keys are presented as `Authorization: Bearer <key>` or `X-API-Key: <key>` and
compared as SHA-256 digests using a constant-time comparison, so a timing
oracle cannot be used to recover a key byte by byte.

In development, if no keys are configured, the app mints one at startup and
prints it. That key lives only in this process's memory. In production, an
empty key set is a startup failure rather than an open door -- the failure mode
of "we forgot to set the env var" must not be "the API is public".
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from core.config import Settings, get_settings, hash_key

logger = logging.getLogger("cloudleak.auth")

# Populated at startup when running unauthenticated in development.
_EPHEMERAL_KEY_HASHES: set[str] = set()


def register_ephemeral_key(key_hash: str) -> None:
    _EPHEMERAL_KEY_HASHES.add(key_hash)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. `key_id` is what rate limits are keyed on."""

    key_id: str

    @property
    def is_anonymous(self) -> bool:
        return self.key_id == "anonymous"


def _extract(authorization: Optional[str], x_api_key: Optional[str]) -> Optional[str]:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer" and credentials.strip():
            return credentials.strip()
    return None


def _matches(candidate_hash: str, settings: Settings) -> bool:
    known = list(settings.api_key_hashes) + list(_EPHEMERAL_KEY_HASHES)
    # compare_digest against every key, without short-circuiting, so the time
    # taken does not reveal how many keys matched or which one.
    matched = False
    for known_hash in known:
        if hmac.compare_digest(candidate_hash, known_hash):
            matched = True
    return matched


async def require_api_key(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """FastAPI dependency. Returns the caller or raises 401."""
    configured = settings.auth_configured or bool(_EPHEMERAL_KEY_HASHES)

    if not configured:
        if settings.is_production:
            # Should be unreachable: startup refuses to boot in this state.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="This service is not configured to accept requests.",
            )
        return Principal(key_id="anonymous")

    presented = _extract(authorization, x_api_key)
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Provide an API key as 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    candidate = hash_key(presented)
    if not _matches(candidate, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That API key is not valid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # The digest identifies the key without holding the key itself. Truncated
    # for log-safety; collisions at 16 hex chars are not a practical concern
    # for a rate-limit bucket.
    return Principal(key_id=candidate[:16])
