from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .embedder import Embedder
from .models import SemanticEntry
from .normalize import normalize_semantic_call
from .reranker import Reranker
from .store import SemanticStore
from .text import bm25_scores, cosine_similarity


def match_semantic(
    db_path: str,
    tool_name: str,
    tool_input: dict[str, Any],
    embedder: Embedder,
    *,
    top_k: int = 5,
    candidate_k: int = 50,
    min_score: float = 0.65,
    dense_weight: float = 0.8,
    lexical_weight: float = 0.2,
    fresh_only: bool = True,
    include_response: bool = False,
    reranker: Reranker | None = None,
    rerank_top_n: int = 10,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    call = normalize_semantic_call(tool_name, tool_input)
    if call is None:
        return {
            "supported": False,
            "matched": False,
            "reusable": False,
            "reason": "tool/action is not supported by semantic-v1",
            "candidates": [],
        }
    if dense_weight < 0 or lexical_weight < 0 or dense_weight + lexical_weight <= 0:
        raise ValueError("dense and lexical weights must be non-negative with a positive sum")
    if now_epoch is None:
        now_epoch = int(datetime.now(timezone.utc).timestamp())

    query_embedding = embedder.embed_query(call.semantic_text)
    store = SemanticStore(db_path)
    try:
        entries = store.candidates(
            embedder.provider_name,
            embedder.model_id,
            call.operation_kind,
            successful_only=True,
        )
    finally:
        store.close()
    if fresh_only:
        entries = [entry for entry in entries if _is_fresh(entry, now_epoch)]

    dense_scores = [cosine_similarity(query_embedding, entry.embedding) for entry in entries]
    lexical_scores = bm25_scores(call.semantic_text, [entry.call.semantic_text for entry in entries])
    total_weight = dense_weight + lexical_weight
    scored = []
    for entry, dense, lexical in zip(entries, dense_scores, lexical_scores):
        dense_unit = max(0.0, min(1.0, dense))
        hybrid = (dense_weight * dense_unit + lexical_weight * lexical) / total_weight
        metadata_boost = _metadata_boost(call.metadata, entry.call.metadata)
        scored.append(
            {
                "entry": entry,
                "dense_score": dense,
                "dense_unit_score": dense_unit,
                "lexical_score": lexical,
                "metadata_boost": metadata_boost,
                "hybrid_score": min(1.0, hybrid + metadata_boost),
                "rerank_score": None,
            }
        )
    scored.sort(key=lambda item: item["hybrid_score"], reverse=True)
    scored = scored[:candidate_k]

    if reranker and scored:
        rerank_items = scored[:rerank_top_n]
        rerank_scores = reranker.score(
            call.semantic_text,
            [item["entry"].call.semantic_text for item in rerank_items],
        )
        if len(rerank_scores) != len(rerank_items):
            raise ValueError("Reranker returned the wrong number of scores")
        for item, score in zip(rerank_items, rerank_scores):
            item["rerank_score"] = score
            item["final_score"] = score
        for item in scored[rerank_top_n:]:
            item["final_score"] = item["hybrid_score"]
        scored.sort(key=lambda item: item["final_score"], reverse=True)
    else:
        for item in scored:
            item["final_score"] = item["hybrid_score"]

    accepted = [item for item in scored if item["final_score"] >= min_score][:top_k]
    return {
        "supported": True,
        "matched": bool(accepted),
        "reusable": False,
        "reason": (
            "semantic candidates require an equivalence decision before reuse"
            if accepted
            else "no semantic candidate reached the score threshold"
        ),
        "semantic_version": call.semantic_version,
        "embedding_provider": embedder.provider_name,
        "embedding_model": embedder.model_id,
        "operation_kind": call.operation_kind,
        "semantic_text": call.semantic_text,
        "weights": {"dense": dense_weight, "lexical": lexical_weight},
        "min_score": min_score,
        "reranker_model": reranker.model_id if reranker else None,
        "candidate_count": len(entries),
        "candidates": [
            _candidate_json(item, now_epoch, include_response)
            for item in accepted
        ],
    }


def _metadata_boost(query: dict[str, Any], candidate: dict[str, Any]) -> float:
    boost = 0.0
    query_url = query.get("url")
    if query_url and query_url == candidate.get("url"):
        # URL identity is stronger than embedding similarity, especially when a
        # full page body makes the document vector much broader than the query.
        boost += 0.7
    query_host = query.get("host")
    if query_host and query_host == candidate.get("host"):
        boost += 0.03
    query_method = query.get("method")
    if query_method and query_method == candidate.get("method"):
        boost += 0.01
    return boost


def _is_fresh(entry: SemanticEntry, now_epoch: int) -> bool:
    return entry.expires_at_epoch is not None and entry.expires_at_epoch > now_epoch


def _candidate_json(item: dict[str, Any], now_epoch: int, include_response: bool) -> dict[str, Any]:
    entry: SemanticEntry = item["entry"]
    result = {
        "record_key": entry.record_key,
        "source_path": entry.source_path,
        "tool_name": entry.call.tool_name,
        "operation_kind": entry.call.operation_kind,
        "semantic_text": entry.call.semantic_text,
        "metadata": entry.call.metadata,
        "dense_score": round(item["dense_score"], 6),
        "lexical_score": round(item["lexical_score"], 6),
        "metadata_boost": round(item["metadata_boost"], 6),
        "hybrid_score": round(item["hybrid_score"], 6),
        "rerank_score": round(item["rerank_score"], 6)
        if item["rerank_score"] is not None
        else None,
        "final_score": round(item["final_score"], 6),
        "fresh": _is_fresh(entry, now_epoch),
        "ended_at": entry.ended_at,
        "response_sha256": entry.response_sha256,
        "response_preview": entry.response_text[:1200],
    }
    if include_response:
        result["tool_response"] = entry.tool_response
    return result
