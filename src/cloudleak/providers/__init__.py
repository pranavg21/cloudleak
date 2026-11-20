"""Provider detection. Each adapter scores a header set; the best score wins."""

from __future__ import annotations

from cloudleak.providers import aws, azure, focus, gcp
from cloudleak.providers.base import Columns, Mapping

ADAPTERS = {"focus": focus, "aws": aws, "azure": azure, "gcp": gcp}


def detect(cols: Columns) -> tuple[str | None, float]:
    scores = {name: mod.score(cols) for name, mod in ADAPTERS.items()}
    best = max(scores, key=lambda k: scores[k])
    if scores[best] <= 0:
        return None, 0.0
    return best, round(scores[best], 3)


__all__ = ["ADAPTERS", "Columns", "Mapping", "detect"]
