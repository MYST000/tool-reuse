from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SEMANTIC_VERSION = "semantic-v1"


@dataclass(frozen=True)
class SemanticCall:
    semantic_version: str
    tool_name: str
    action_kind: str
    operation_kind: str
    semantic_text: str
    metadata: dict[str, Any]
    freshness_class: str
    ttl_seconds: int


@dataclass(frozen=True)
class SemanticEntry:
    record_key: str
    source_path: str
    call: SemanticCall
    embedding_provider: str
    embedding_model: str
    embedding: list[float]
    started_at: str | None
    ended_at: str | None
    observed_at_epoch: int | None
    expires_at_epoch: int | None
    success: bool
    status_reason: str
    tool_input: dict[str, Any]
    tool_response: dict[str, Any]
    response_text: str
    response_sha256: str
