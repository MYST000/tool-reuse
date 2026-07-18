from __future__ import annotations

from dataclasses import dataclass
from typing import Any


KEY_VERSION = "exact-v5"


@dataclass(frozen=True)
class ExactCall:
    exact_key: str
    key_version: str
    tool_name: str
    action_kind: str
    operation_kind: str
    canonical: dict[str, Any]
    freshness_class: str
    ttl_seconds: int
    replayable: bool
    replay_policy: str
    reason: str


@dataclass(frozen=True)
class ExactEntry:
    record_key: str
    source_path: str
    exact_call: ExactCall
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
    source_record: dict[str, Any]
