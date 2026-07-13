from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from tool_reuse.semantic.embedder import HashingEmbedder
from tool_reuse.semantic.ingest import ingest_semantic_records
from tool_reuse.semantic.matcher import match_semantic
from tool_reuse.semantic.normalize import MAX_BROWSER_CONTENT_CHARS, normalize_semantic_call
from tool_reuse.semantic.text import bm25_scores


def _record(
    record_key: str,
    tool_name: str,
    tool_input: dict,
    response: str,
) -> dict:
    return {
        "record_key": record_key,
        "tool_name": tool_name,
        "started_at": "2026-07-08T08:00:00+00:00",
        "ended_at": "2026-07-08T08:00:01+00:00",
        "tool_input": tool_input,
        "tool_response": {
            "kind": "ToolObservation",
            "is_error": False,
            "timeout": False,
            "exit_code": 0 if tool_name == "terminal" else None,
            "content": [{"type": "text", "text": response}],
        },
    }


class SemanticNormalizeTests(unittest.TestCase):
    def test_web_search_query_is_supported(self) -> None:
        call = normalize_semantic_call(
            "web_search",
            {"kind": "SearchAction", "query": "中文 embedding 模型", "domains": ["example.com"]},
        )

        self.assertIsNotNone(call)
        self.assertEqual(call.operation_kind, "web_search")
        self.assertIn("中文 embedding 模型", call.semantic_text)

    def test_hashing_vectors_are_deterministic_and_normalized(self) -> None:
        embedder = HashingEmbedder(dimensions=64)
        left = embedder.embed_query("semantic matching model")
        right = embedder.embed_query("semantic matching model")

        self.assertEqual(left, right)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in left)), 1.0)

    def test_bm25_prefers_matching_document(self) -> None:
        scores = bm25_scores(
            "semantic embedding search",
            ["semantic embedding retrieval", "weather forecast temperature"],
        )

        self.assertGreater(scores[0], scores[1])


class SemanticPipelineTests(unittest.TestCase):
    def test_browser_content_observation_is_indexed_for_url_retrieval(self) -> None:
        records = [
            _record(
                "browser-content",
                "browser_get_content",
                {
                    "kind": "BrowserGetContentAction",
                    "extract_links": False,
                    "start_from_char": 0,
                },
                (
                    "<url>https://example.com/docs/semantic-search</url>"
                    "<content><webpage_content>Dense retrieval with bilingual embeddings "
                    "and cross encoder reranking.</webpage_content></content>"
                ),
            ),
            _record(
                "empty-browser-content",
                "browser_get_content",
                {"kind": "BrowserGetContentAction", "start_from_char": 0},
                "<url>https://example.com/empty</url><webpage_content></webpage_content>",
            ),
        ]
        embedder = HashingEmbedder(dimensions=128)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            ingest_result = ingest_semantic_records(records_path, db_path, embedder)
            result = match_semantic(
                str(db_path),
                "browser_navigate",
                {
                    "kind": "BrowserNavigateAction",
                    "new_tab": False,
                    "url": "https://example.com/docs/semantic-search",
                },
                embedder,
                min_score=0.0,
                fresh_only=False,
            )

        self.assertEqual(ingest_result["imported"], 1)
        self.assertEqual(ingest_result["imported_tool_counts"], {"browser_get_content": 1})
        self.assertEqual(ingest_result["unsupported_tool_counts"], {"browser_get_content": 1})
        self.assertTrue(result["matched"])
        self.assertEqual(result["candidates"][0]["record_key"], "browser-content")
        self.assertGreaterEqual(result["candidates"][0]["metadata_boost"], 0.7)
        self.assertEqual(
            result["candidates"][0]["metadata"]["source_action"],
            "browser_get_content",
        )

    def test_browser_content_text_is_bounded(self) -> None:
        records = [
            _record(
                "large-browser-content",
                "browser_get_content",
                {"kind": "BrowserGetContentAction", "start_from_char": 0},
                "<url>https://example.com/large</url><webpage_content>"
                + "x" * (MAX_BROWSER_CONTENT_CHARS + 500)
                + "</webpage_content>",
            )
        ]
        embedder = HashingEmbedder(dimensions=64)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
            ingest_semantic_records(records_path, db_path, embedder)
            result = match_semantic(
                str(db_path),
                "browser_navigate",
                {"kind": "BrowserNavigateAction", "url": "https://example.com/large"},
                embedder,
                min_score=0.0,
                fresh_only=False,
            )

        metadata = result["candidates"][0]["metadata"]
        self.assertEqual(metadata["content_chars"], MAX_BROWSER_CONTENT_CHARS + 500)
        self.assertEqual(metadata["indexed_content_chars"], MAX_BROWSER_CONTENT_CHARS)

    def test_ingest_and_hybrid_match(self) -> None:
        records = [
            _record(
                "semantic-doc",
                "terminal",
                {
                    "kind": "TerminalAction",
                    "command": (
                        "curl -s https://www.sbert.net/examples/applications/semantic-search/README.html "
                        "| grep -E 'semantic|encoder|retrieval' | head -50"
                    ),
                },
                "semantic search documentation",
            ),
            _record(
                "weather-doc",
                "terminal",
                {
                    "kind": "TerminalAction",
                    "command": "curl -s https://weather.example.com/current.json | head -50",
                },
                "weather response",
            ),
            _record(
                "browser-doc",
                "browser_navigate",
                {
                    "kind": "BrowserNavigateAction",
                    "new_tab": True,
                    "url": "https://www.sbert.net/examples/applications/semantic-search/README.html",
                },
                "Opened new tab",
            ),
        ]
        embedder = HashingEmbedder(dimensions=128)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(
                "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                encoding="utf-8",
            )
            ingest_result = ingest_semantic_records(records_path, db_path, embedder)
            query = {
                "kind": "TerminalAction",
                "command": (
                    "curl https://www.sbert.net/examples/applications/semantic-retrieval/README.html "
                    "| grep -E 'semantic|encoder|retrieval' | head -50"
                ),
            }
            result = match_semantic(
                str(db_path),
                "terminal",
                query,
                embedder,
                min_score=0.2,
                now_epoch=1783497601 + 60,
            )
            isolated = match_semantic(
                str(db_path),
                "terminal",
                query,
                HashingEmbedder(dimensions=64),
                min_score=0.0,
                now_epoch=1783497601 + 60,
            )

        self.assertEqual(ingest_result["imported"], 3)
        self.assertTrue(result["matched"])
        self.assertFalse(result["reusable"])
        self.assertEqual(result["candidates"][0]["record_key"], "semantic-doc")
        self.assertTrue(all(item["operation_kind"] == "curl_http" for item in result["candidates"]))
        self.assertEqual(isolated["candidate_count"], 0)
        self.assertFalse(isolated["matched"])


if __name__ == "__main__":
    unittest.main()
