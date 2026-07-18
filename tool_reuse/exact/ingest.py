from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..jsonutil import sha256_text
from ..policy import parse_iso_epoch
from ..response import extract_response_text
from .models import ExactEntry
from .normalize import normalize_exact_call
from .policy import origin_status, response_status
from .redact import redact_tool_input, source_metadata
from .store import ExactStore


def iter_records(records_path: str | Path) -> tuple[Path, Iterator[dict[str, Any]]]:
    path = Path(records_path)
    jsonl = path / "tool_calls.jsonl" if path.is_dir() else path

    def _iterator() -> Iterator[dict[str, Any]]:
        with jsonl.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON at {jsonl}:{line_no}: {exc}"
                    ) from exc
                if isinstance(record, dict):
                    yield record

    return jsonl, _iterator()


def entry_from_record(
    source: dict[str, Any],
    source_path: str,
    *,
    cache_scope: str = "local",
    trust_legacy_origins: bool = False,
) -> ExactEntry | None:
    tool_name = source.get("tool_name")
    tool_input = source.get("tool_input")
    tool_response = source.get("tool_response")
    if (
        not isinstance(tool_name, str)
        or not isinstance(tool_input, dict)
        or not isinstance(tool_response, dict)
    ):
        return None
    origin, _ = origin_status(source, trust_legacy=trust_legacy_origins)
    if not origin:
        return None
    exact_call = normalize_exact_call(tool_name, tool_input, cache_scope=cache_scope)
    if exact_call is None:
        return None

    safe_tool_response = redact_tool_input(tool_response)
    if safe_tool_response != tool_response:
        return None
    success, status_reason = response_status(tool_response, tool_name)
    safe_tool_input = redact_tool_input(tool_input)
    ended_at = (
        source.get("ended_at") if isinstance(source.get("ended_at"), str) else None
    )
    observed_at_epoch = parse_iso_epoch(ended_at)
    expires_at_epoch = (
        observed_at_epoch + exact_call.ttl_seconds
        if observed_at_epoch is not None
        else None
    )
    response_text = extract_response_text(tool_response)
    return ExactEntry(
        record_key=str(
            source.get("record_key") or sha256_text(json.dumps(source, sort_keys=True))
        ),
        source_path=source_path,
        exact_call=exact_call,
        started_at=source.get("started_at")
        if isinstance(source.get("started_at"), str)
        else None,
        ended_at=ended_at,
        observed_at_epoch=observed_at_epoch,
        expires_at_epoch=expires_at_epoch,
        success=success,
        status_reason=status_reason,
        tool_input=safe_tool_input,
        tool_response=safe_tool_response,
        response_text=response_text,
        response_sha256=sha256_text(response_text),
        source_record=source_metadata(source, safe_tool_input)
        | {
            "execution_source": "tool",
            "origin_record_key": str(
                source.get("origin_record_key") or source.get("record_key") or ""
            ),
        },
    )


def ingest_exact_records(
    records_path: str | Path,
    db_path: str | Path,
    *,
    cache_scope: str = "local",
    trust_legacy_origins: bool = False,
) -> dict[str, Any]:
    jsonl, records = iter_records(records_path)
    store = ExactStore(db_path)
    seen = 0
    imported = 0
    unsupported = 0
    tool_counts: Counter[str] = Counter()
    imported_tool_counts: Counter[str] = Counter()
    unsupported_tool_counts: Counter[str] = Counter()
    operation_counts: Counter[str] = Counter()
    success_counts: Counter[str] = Counter()
    provenance_counts: Counter[str] = Counter()
    try:
        store.delete_source(str(jsonl))
        for source in records:
            seen += 1
            tool_name = source.get("tool_name")
            tool_counts[
                str(tool_name) if isinstance(tool_name, str) else "invalid_record"
            ] += 1
            origin, origin_reason = origin_status(
                source, trust_legacy=trust_legacy_origins
            )
            provenance_counts[origin_reason] += 1
            entry = entry_from_record(
                source,
                str(jsonl),
                cache_scope=cache_scope,
                trust_legacy_origins=trust_legacy_origins,
            )
            if entry is None:
                unsupported += 1
                unsupported_tool_counts[
                    str(tool_name) if isinstance(tool_name, str) else "invalid_record"
                ] += 1
                continue
            store.upsert(entry)
            imported += 1
            imported_tool_counts[entry.exact_call.tool_name] += 1
            operation_counts[entry.exact_call.operation_kind] += 1
            success_counts["success" if entry.success else "failed"] += 1
        store.commit()
        return {
            "source": str(jsonl),
            "seen": seen,
            "imported": imported,
            "unsupported": unsupported,
            "tool_counts": dict(tool_counts),
            "imported_tool_counts": dict(imported_tool_counts),
            "unsupported_tool_counts": dict(unsupported_tool_counts),
            "operation_counts": dict(operation_counts),
            "status_counts": dict(success_counts),
            "provenance_counts": dict(provenance_counts),
            "cache_scope": cache_scope,
            "trust_legacy_origins": trust_legacy_origins,
            "database": store.stats(),
        }
    finally:
        store.close()
