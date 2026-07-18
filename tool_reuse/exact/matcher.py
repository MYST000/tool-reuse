from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .models import ExactEntry
from .normalize import normalize_exact_call
from .store import ExactStore


def match_exact(
    db_path: str,
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    now_epoch: int | None = None,
    include_response: bool = True,
    limit: int = 20,
    cache_scope: str = "local",
) -> dict[str, Any]:
    call = normalize_exact_call(tool_name, tool_input, cache_scope=cache_scope)
    if call is None:
        return {
            "supported": False,
            "matched": False,
            "reusable": False,
            "reason": "tool/action is not supported by exact-v5",
        }
    if now_epoch is None:
        now_epoch = int(datetime.now(UTC).timestamp())

    store = ExactStore(db_path, read_only=True)
    try:
        entries = store.find(call.exact_key, limit=limit)
    finally:
        store.close()

    selected = _select_entry(entries, now_epoch)
    reusable = bool(
        selected
        and call.replayable
        and selected.success
        and selected.exact_call.replayable
        and _is_fresh(selected, now_epoch)
    )
    if not entries:
        reason = "no exact key found"
    elif reusable:
        reason = "fresh successful replayable exact match"
    elif selected and not selected.success:
        reason = (
            "exact history exists but selected observation failed: "
            f"{selected.status_reason}"
        )
    elif selected and not selected.exact_call.replayable:
        reason = selected.exact_call.reason
    else:
        reason = "exact history exists but the selected observation is stale"

    result: dict[str, Any] = {
        "supported": True,
        "matched": bool(entries),
        "reusable": reusable,
        "reason": reason,
        "exact_key": call.exact_key,
        "key_version": call.key_version,
        "operation_kind": call.operation_kind,
        "canonical": call.canonical,
        "query_policy": {
            "freshness_class": call.freshness_class,
            "ttl_seconds": call.ttl_seconds,
            "replayable": call.replayable,
            "replay_policy": call.replay_policy,
        },
        "match_count": len(entries),
        "selected": _entry_json(selected, now_epoch, include_response)
        if selected
        else None,
        "matches": [_entry_json(entry, now_epoch, False) for entry in entries],
    }
    if reusable and include_response and selected:
        result["tool_response"] = selected.tool_response
    return result


def _select_entry(entries: list[ExactEntry], now_epoch: int) -> ExactEntry | None:
    if not entries:
        return None
    for entry in entries:
        if (
            entry.success
            and entry.exact_call.replayable
            and _is_fresh(entry, now_epoch)
        ):
            return entry
    for entry in entries:
        if entry.success:
            return entry
    return entries[0]


def _is_fresh(entry: ExactEntry, now_epoch: int) -> bool:
    return entry.expires_at_epoch is not None and entry.expires_at_epoch > now_epoch


def _entry_json(
    entry: ExactEntry, now_epoch: int, include_response: bool
) -> dict[str, Any]:
    result = {
        "record_key": entry.record_key,
        "source_path": entry.source_path,
        "tool_name": entry.exact_call.tool_name,
        "action_kind": entry.exact_call.action_kind,
        "operation_kind": entry.exact_call.operation_kind,
        "success": entry.success,
        "status_reason": entry.status_reason,
        "replayable": entry.exact_call.replayable,
        "replay_policy": entry.exact_call.replay_policy,
        "fresh": _is_fresh(entry, now_epoch),
        "freshness_class": entry.exact_call.freshness_class,
        "ttl_seconds": entry.exact_call.ttl_seconds,
        "ended_at": entry.ended_at,
        "expires_at_epoch": entry.expires_at_epoch,
        "response_sha256": entry.response_sha256,
        "response_preview": entry.response_text[:1200],
    }
    if include_response:
        result["tool_input"] = entry.tool_input
        result["tool_response"] = entry.tool_response
    return result
