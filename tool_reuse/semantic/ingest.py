from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ..exact.ingest import iter_records
from ..exact.policy import response_status
from ..exact.redact import redact_tool_input
from ..jsonutil import sha256_text
from ..policy import parse_iso_epoch
from ..response import extract_response_text
from .embedder import Embedder
from .models import SemanticEntry
from .normalize import normalize_semantic_record
from .store import SemanticStore


def ingest_semantic_records(
    records_path: str | Path,
    db_path: str | Path,
    embedder: Embedder,
    *,
    batch_size: int = 32,
) -> dict[str, Any]:
    jsonl, records = iter_records(records_path)
    supported: list[tuple[dict[str, Any], Any]] = []
    seen = 0
    unsupported = 0
    tool_counts: Counter[str] = Counter()
    imported_tool_counts: Counter[str] = Counter()
    unsupported_tool_counts: Counter[str] = Counter()
    for source in records:
        seen += 1
        tool_name = source.get("tool_name")
        tool_input = source.get("tool_input")
        tool_response = source.get("tool_response")
        if not isinstance(tool_name, str) or not isinstance(tool_input, dict) or not isinstance(tool_response, dict):
            unsupported += 1
            unsupported_tool_counts["invalid_record"] += 1
            continue
        tool_counts[tool_name] += 1
        call = normalize_semantic_record(tool_name, tool_input, tool_response)
        if call is None:
            unsupported += 1
            unsupported_tool_counts[tool_name] += 1
            continue
        supported.append((source, call))

    store = SemanticStore(db_path)
    imported = 0
    operation_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    try:
        for start in range(0, len(supported), batch_size):
            batch = supported[start : start + batch_size]
            embeddings = embedder.embed_documents([call.semantic_text for _, call in batch])
            if len(embeddings) != len(batch):
                raise ValueError("Embedding provider returned the wrong batch size")
            for (source, call), embedding in zip(batch, embeddings):
                tool_input = source["tool_input"]
                tool_response = source["tool_response"]
                success, status_reason = response_status(tool_response)
                ended_at = source.get("ended_at") if isinstance(source.get("ended_at"), str) else None
                observed_at_epoch = parse_iso_epoch(ended_at)
                expires_at_epoch = (
                    observed_at_epoch + call.ttl_seconds
                    if observed_at_epoch is not None
                    else None
                )
                response_text = extract_response_text(tool_response)
                entry = SemanticEntry(
                    record_key=str(source.get("record_key") or sha256_text(call.semantic_text)),
                    source_path=str(jsonl),
                    call=call,
                    embedding_provider=embedder.provider_name,
                    embedding_model=embedder.model_id,
                    embedding=embedding,
                    started_at=source.get("started_at")
                    if isinstance(source.get("started_at"), str)
                    else None,
                    ended_at=ended_at,
                    observed_at_epoch=observed_at_epoch,
                    expires_at_epoch=expires_at_epoch,
                    success=success,
                    status_reason=status_reason,
                    tool_input=redact_tool_input(tool_input),
                    tool_response=tool_response,
                    response_text=response_text,
                    response_sha256=sha256_text(response_text),
                )
                store.upsert(entry)
                imported += 1
                imported_tool_counts[str(source["tool_name"])] += 1
                operation_counts[call.operation_kind] += 1
                status_counts["success" if success else "failed"] += 1
        store.commit()
        return {
            "source": str(jsonl),
            "seen": seen,
            "imported": imported,
            "unsupported": unsupported,
            "tool_counts": dict(tool_counts),
            "imported_tool_counts": dict(imported_tool_counts),
            "unsupported_tool_counts": dict(unsupported_tool_counts),
            "embedding_provider": embedder.provider_name,
            "embedding_model": embedder.model_id,
            "operation_counts": dict(operation_counts),
            "status_counts": dict(status_counts),
            "database": store.stats(),
        }
    finally:
        store.close()
