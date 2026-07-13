from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedToolCall:
    tool_name: str
    canonical_key: str
    intent_text: str
    fingerprint: dict[str, Any]
    freshness_class: str
    ttl_seconds: int
    cacheable: bool
    reason: str | None = None

