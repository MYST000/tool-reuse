from __future__ import annotations

import argparse
import json
import sys

from .exact.ingest import ingest_exact_records
from .exact.matcher import match_exact
from .exact.store import ExactStore
from .semantic.embedder import create_embedder
from .semantic.ingest import ingest_semantic_records
from .semantic.matcher import match_semantic
from .semantic.reranker import CrossEncoderReranker
from .semantic.store import SemanticStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tool-reuse")
    sub = parser.add_subparsers(dest="cmd", required=True)

    exact_ingest_parser = sub.add_parser(
        "exact-ingest",
        help="import Web Search records into the exact-v5 cache",
    )
    exact_ingest_parser.add_argument("--records", required=True)
    exact_ingest_parser.add_argument("--db", required=True)
    exact_ingest_parser.add_argument("--scope", required=True)
    exact_ingest_parser.add_argument("--trust-legacy-origins", action="store_true")

    exact_match_parser = sub.add_parser(
        "exact-match", help="match an exact-v5 tool call"
    )
    exact_match_parser.add_argument("--db", required=True)
    exact_match_parser.add_argument("--tool", required=True)
    exact_match_parser.add_argument("--input-json", required=True)
    exact_match_parser.add_argument("--scope", required=True)
    exact_match_parser.add_argument("--limit", type=int, default=20)
    exact_match_parser.add_argument(
        "--full-response",
        action="store_true",
        help="include the complete cached tool response",
    )

    exact_stats_parser = sub.add_parser(
        "exact-stats", help="show exact-v5 cache statistics"
    )
    exact_stats_parser.add_argument("--db", required=True)

    semantic_ingest_parser = sub.add_parser(
        "semantic-ingest",
        help="embed and import supported OpenHands tool records",
    )
    semantic_ingest_parser.add_argument("--records", required=True)
    semantic_ingest_parser.add_argument("--db", required=True)
    semantic_ingest_parser.add_argument("--scope", required=True)
    semantic_ingest_parser.add_argument("--trust-legacy-origins", action="store_true")
    semantic_ingest_parser.add_argument("--batch-size", type=int, default=32)
    _add_embedding_arguments(semantic_ingest_parser)

    semantic_match_parser = sub.add_parser(
        "semantic-match",
        help="retrieve hybrid semantic candidates",
    )
    semantic_match_parser.add_argument("--db", required=True)
    semantic_match_parser.add_argument("--tool", required=True)
    semantic_match_parser.add_argument("--input-json", required=True)
    semantic_match_parser.add_argument("--scope", required=True)
    semantic_match_parser.add_argument("--top-k", type=int, default=5)
    semantic_match_parser.add_argument("--candidate-k", type=int, default=50)
    semantic_match_parser.add_argument("--min-score", type=float, default=0.65)
    semantic_match_parser.add_argument("--dense-weight", type=float, default=0.8)
    semantic_match_parser.add_argument("--lexical-weight", type=float, default=0.2)
    semantic_match_parser.add_argument("--include-stale", action="store_true")
    semantic_match_parser.add_argument("--full-response", action="store_true")
    semantic_match_parser.add_argument("--reranker-model")
    semantic_match_parser.add_argument("--rerank-top-n", type=int, default=10)
    _add_embedding_arguments(semantic_match_parser)

    semantic_stats_parser = sub.add_parser(
        "semantic-stats",
        help="show semantic-v3 index statistics",
    )
    semantic_stats_parser.add_argument("--db", required=True)
    semantic_stats_parser.add_argument("--scope", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "exact-ingest":
        result = ingest_exact_records(
            args.records,
            args.db,
            cache_scope=args.scope,
            trust_legacy_origins=args.trust_legacy_origins,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "exact-match":
        tool_input = json.loads(args.input_json)
        if not isinstance(tool_input, dict):
            raise SystemExit("--input-json must decode to an object")
        result = match_exact(
            args.db,
            args.tool,
            tool_input,
            include_response=args.full_response,
            limit=args.limit,
            cache_scope=args.scope,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "exact-stats":
        store = ExactStore(args.db, read_only=True)
        try:
            result = store.stats()
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "semantic-ingest":
        embedder = _embedder_from_args(args)
        result = ingest_semantic_records(
            args.records,
            args.db,
            embedder,
            cache_scope=args.scope,
            trust_legacy_origins=args.trust_legacy_origins,
            batch_size=args.batch_size,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "semantic-match":
        tool_input = json.loads(args.input_json)
        if not isinstance(tool_input, dict):
            raise SystemExit("--input-json must decode to an object")
        embedder = _embedder_from_args(args)
        reranker = (
            CrossEncoderReranker(args.reranker_model, device=args.device)
            if args.reranker_model
            else None
        )
        result = match_semantic(
            args.db,
            args.tool,
            tool_input,
            embedder,
            cache_scope=args.scope,
            top_k=args.top_k,
            candidate_k=args.candidate_k,
            min_score=args.min_score,
            dense_weight=args.dense_weight,
            lexical_weight=args.lexical_weight,
            fresh_only=not args.include_stale,
            include_response=args.full_response,
            reranker=reranker,
            rerank_top_n=args.rerank_top_n,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "semantic-stats":
        store = SemanticStore(args.db, read_only=True)
        try:
            result = store.stats(args.scope)
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2


def _add_embedding_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("sentence-transformers", "openai-compatible", "hashing"),
        default="sentence-transformers",
    )
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env", default="EMBEDDING_API_KEY")
    parser.add_argument("--dimensions", type=int)
    parser.add_argument("--query-prefix", default="")
    parser.add_argument("--document-prefix", default="")
    parser.add_argument("--device")


def _embedder_from_args(args: argparse.Namespace):
    return create_embedder(
        args.provider,
        args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        dimensions=args.dimensions,
        query_prefix=args.query_prefix,
        document_prefix=args.document_prefix,
        device=args.device,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
