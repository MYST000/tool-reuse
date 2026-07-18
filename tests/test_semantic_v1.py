from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tool_reuse.exact.redact import redact_semantic_text
from tool_reuse.semantic.embedder import HashingEmbedder, OpenAICompatibleEmbedder
from tool_reuse.semantic.ingest import ingest_semantic_records
from tool_reuse.semantic.matcher import match_semantic
from tool_reuse.semantic.normalize import (
    MAX_BROWSER_CONTENT_CHARS,
    normalize_semantic_call,
)
from tool_reuse.semantic.store import SemanticStore
from tool_reuse.semantic.text import bm25_scores


TEST_SCOPE = "tenant-a/provider-a/tools-v1"


def _record(
    record_key: str,
    tool_name: str,
    tool_input: dict,
    response: str,
) -> dict:
    observation_kind = (
        "TerminalObservation"
        if tool_name == "terminal"
        else "BrowserObservation"
        if tool_name.startswith("browser_")
        else "ToolObservation"
    )
    return {
        "record_key": record_key,
        "execution_source": "tool",
        "origin_record_key": record_key,
        "tool_name": tool_name,
        "started_at": "2026-07-08T08:00:00+00:00",
        "ended_at": "2026-07-08T08:00:01+00:00",
        "tool_input": tool_input,
        "tool_response": {
            "kind": observation_kind,
            "is_error": False,
            "timeout": False,
            "exit_code": 0 if tool_name == "terminal" else None,
            "content": [{"type": "text", "text": response}],
        },
    }


class SemanticNormalizeTests(unittest.TestCase):
    def test_semantic_text_redacts_common_credentials(self) -> None:
        text = redact_semantic_text(
            "Authorization: Bearer abc123 api_key=secret password: hunter2"
        )

        self.assertNotIn("abc123", text)
        self.assertNotIn("api_key=secret", text)
        self.assertNotIn("hunter2", text)

    def test_generic_read_only_web_calls_are_semantic_candidates(self) -> None:
        curl = normalize_semantic_call(
            "terminal",
            {
                "kind": "TerminalAction",
                "command": "curl https://docs.example.com/guide | head -20",
            },
        )
        browser = normalize_semantic_call(
            "browser_navigate",
            {
                "kind": "BrowserNavigateAction",
                "url": "https://docs.example.com/guide",
            },
        )

        self.assertIsNotNone(curl)
        self.assertIsNotNone(browser)
        self.assertEqual(curl.operation_kind, "web_fetch_curl")
        self.assertEqual(browser.operation_kind, "browser_page")

    def test_web_search_query_is_supported(self) -> None:
        call = normalize_semantic_call(
            "web_search",
            {
                "kind": "SearchAction",
                "query": "中文 embedding 模型",
                "domains": ["example.com"],
            },
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

    def test_embedding_runtime_config_isolates_indexes(self) -> None:
        small = OpenAICompatibleEmbedder(
            "embedding-model", base_url="http://localhost:8000/v1", dimensions=128
        )
        large = OpenAICompatibleEmbedder(
            "embedding-model", base_url="http://localhost:8000/v1", dimensions=1024
        )
        other_server = OpenAICompatibleEmbedder(
            "embedding-model", base_url="http://localhost:9000/v1", dimensions=128
        )
        prefixed = OpenAICompatibleEmbedder(
            "embedding-model",
            base_url="http://localhost:8000/v1",
            dimensions=128,
            query_prefix="query: ",
        )

        self.assertNotEqual(small.index_id, large.index_id)
        self.assertNotEqual(small.index_id, other_server.index_id)
        self.assertNotEqual(small.index_id, prefixed.index_id)

    def test_bm25_prefers_matching_document(self) -> None:
        scores = bm25_scores(
            "semantic embedding search",
            ["semantic embedding retrieval", "weather forecast temperature"],
        )

        self.assertGreater(scores[0], scores[1])


class SemanticPipelineTests(unittest.TestCase):
    def test_unscoped_v2_database_requires_or_performs_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "semantic.sqlite"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE semantic_entries (record_key TEXT PRIMARY KEY)"
            )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(ValueError, "rebuild"):
                SemanticStore(db_path, read_only=True)

            store = SemanticStore(db_path)
            try:
                columns = {
                    row[1]
                    for row in store.conn.execute("PRAGMA table_info(semantic_entries)")
                }
                self.assertIn("cache_scope", columns)
                self.assertEqual(store.stats(TEST_SCOPE)["total"], 0)
            finally:
                store.close()

    def test_secrets_are_redacted_before_embedding_storage_and_output(self) -> None:
        secret = "synthetic-secret-value"
        record = _record(
            "secret-content",
            "browser_get_content",
            {"kind": "BrowserGetContentAction", "start_from_char": 0},
            (
                "<url>https://docs.example.com/guide</url>"
                "<webpage_content>Cookie: sessionid="
                f"{secret} Authorization: Bearer {secret}</webpage_content>"
            ),
        )

        class CapturingEmbedder(HashingEmbedder):
            embedded_documents: list[str]

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                self.embedded_documents = texts
                return super().embed_documents(texts)

        embedder = CapturingEmbedder(dimensions=64)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            ingest_semantic_records(
                records_path, db_path, embedder, cache_scope=TEST_SCOPE
            )
            result = match_semantic(
                str(db_path),
                "browser_navigate",
                {
                    "kind": "BrowserNavigateAction",
                    "url": "https://docs.example.com/guide",
                },
                embedder,
                cache_scope=TEST_SCOPE,
                min_score=0.0,
                fresh_only=False,
                include_response=True,
            )

            database_bytes = db_path.read_bytes()

        self.assertNotIn(secret, " ".join(embedder.embedded_documents))
        self.assertNotIn(secret.encode(), database_bytes)
        self.assertNotIn(secret, json.dumps(result))

    def test_authenticated_browser_content_is_not_indexed(self) -> None:
        secret = "synthetic-secret-value"
        record = _record(
            "authenticated-content",
            "browser_get_content",
            {"kind": "BrowserGetContentAction", "start_from_char": 0},
            (
                f"<url>https://example.com/guide?token={secret}</url>"
                "<webpage_content>private page</webpage_content>"
            ),
        )
        embedder = HashingEmbedder(dimensions=64)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            result = ingest_semantic_records(
                records_path, db_path, embedder, cache_scope=TEST_SCOPE
            )

        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["unsupported"], 1)

    def test_candidate_scan_is_bounded(self) -> None:
        records = [
            _record(
                f"record-{index}",
                "web_search",
                {"kind": "SearchAction", "query": f"query {index}"},
                f"response {index}",
            )
            for index in range(8)
        ]
        embedder = HashingEmbedder(dimensions=64)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            ingest_semantic_records(
                records_path, db_path, embedder, cache_scope=TEST_SCOPE
            )
            result = match_semantic(
                str(db_path),
                "web_search",
                {"kind": "SearchAction", "query": "query"},
                embedder,
                cache_scope=TEST_SCOPE,
                candidate_k=3,
                min_score=0.0,
                fresh_only=False,
            )

        self.assertEqual(result["candidate_count"], 3)

    def test_cache_replacement_record_is_not_indexed(self) -> None:
        record = _record(
            "cache-hit",
            "web_search",
            {"kind": "SearchAction", "query": "do not reingest"},
            "cached response",
        )
        record["execution_source"] = "hook_replacement"
        embedder = HashingEmbedder(dimensions=64)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            result = ingest_semantic_records(
                records_path, db_path, embedder, cache_scope=TEST_SCOPE
            )

        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["unsupported"], 1)

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
                    "<url>https://example.com/search?q=semantic-search</url>"
                    "<content><webpage_content>Dense retrieval with bilingual "
                    "embeddings "
                    "and cross encoder reranking.</webpage_content></content>"
                ),
            ),
            _record(
                "empty-browser-content",
                "browser_get_content",
                {"kind": "BrowserGetContentAction", "start_from_char": 0},
                "<url>https://example.com/search?q=empty</url>"
                "<webpage_content></webpage_content>",
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
            ingest_result = ingest_semantic_records(
                records_path, db_path, embedder, cache_scope=TEST_SCOPE
            )
            result = match_semantic(
                str(db_path),
                "browser_navigate",
                {
                    "kind": "BrowserNavigateAction",
                    "new_tab": False,
                    "url": "https://example.com/search?q=semantic-search",
                },
                embedder,
                cache_scope=TEST_SCOPE,
                min_score=0.0,
                fresh_only=False,
            )

        self.assertEqual(ingest_result["imported"], 1)
        self.assertEqual(
            ingest_result["imported_tool_counts"], {"browser_get_content": 1}
        )
        self.assertEqual(
            ingest_result["unsupported_tool_counts"], {"browser_get_content": 1}
        )
        self.assertTrue(result["matched"])
        self.assertEqual(result["candidates"][0]["record_key"], "browser-content")
        self.assertGreaterEqual(result["candidates"][0]["metadata_boost"], 0.7)
        self.assertEqual(
            result["candidates"][0]["metadata"]["source_action"],
            "browser_get_content",
        )

    def test_generic_browser_content_is_indexed_but_never_reusable(self) -> None:
        records = [
            _record(
                "browser-doc",
                "browser_get_content",
                {"kind": "BrowserGetContentAction", "start_from_char": 0},
                "<url>https://docs.example.com/guide</url>"
                "<webpage_content>Tool reuse provenance guide.</webpage_content>",
            )
        ]
        embedder = HashingEmbedder(dimensions=64)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
            ingest = ingest_semantic_records(
                records_path, db_path, embedder, cache_scope=TEST_SCOPE
            )
            result = match_semantic(
                str(db_path),
                "browser_navigate",
                {
                    "kind": "BrowserNavigateAction",
                    "url": "https://docs.example.com/guide",
                },
                embedder,
                cache_scope=TEST_SCOPE,
                min_score=0.0,
                fresh_only=False,
            )

        self.assertEqual(ingest["imported"], 1)
        self.assertTrue(result["matched"])
        self.assertFalse(result["reusable"])

    def test_browser_content_text_is_bounded(self) -> None:
        records = [
            _record(
                "large-browser-content",
                "browser_get_content",
                {"kind": "BrowserGetContentAction", "start_from_char": 0},
                "<url>https://example.com/search?q=large</url><webpage_content>"
                + "x" * (MAX_BROWSER_CONTENT_CHARS + 500)
                + "</webpage_content>",
            )
        ]
        embedder = HashingEmbedder(dimensions=64)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
            ingest_semantic_records(
                records_path, db_path, embedder, cache_scope=TEST_SCOPE
            )
            result = match_semantic(
                str(db_path),
                "browser_navigate",
                {
                    "kind": "BrowserNavigateAction",
                    "url": "https://example.com/search?q=large",
                },
                embedder,
                cache_scope=TEST_SCOPE,
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
                        "curl -s 'https://www.sbert.net/search?q=semantic+embedding' "
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
                    "command": (
                        "curl -s 'https://weather.example.com/search?"
                        "q=current+weather' "
                        "| head -50"
                    ),
                },
                "weather response",
            ),
            _record(
                "browser-doc",
                "browser_navigate",
                {
                    "kind": "BrowserNavigateAction",
                    "new_tab": True,
                    "url": "https://www.sbert.net/search?q=semantic+embedding",
                },
                "Opened new tab",
            ),
        ]
        embedder = HashingEmbedder(dimensions=128)
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "semantic.sqlite"
            records_path.write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False) + "\n" for record in records
                ),
                encoding="utf-8",
            )
            ingest_result = ingest_semantic_records(
                records_path, db_path, embedder, cache_scope=TEST_SCOPE
            )
            query = {
                "kind": "TerminalAction",
                "command": (
                    "curl 'https://www.sbert.net/search?q=semantic+retrieval' "
                    "| grep -E 'semantic|encoder|retrieval' | head -50"
                ),
            }
            result = match_semantic(
                str(db_path),
                "terminal",
                query,
                embedder,
                cache_scope=TEST_SCOPE,
                min_score=0.2,
                now_epoch=1783497601 + 60,
            )
            isolated = match_semantic(
                str(db_path),
                "terminal",
                query,
                HashingEmbedder(dimensions=64),
                cache_scope=TEST_SCOPE,
                min_score=0.0,
                now_epoch=1783497601 + 60,
            )
            other_scope = match_semantic(
                str(db_path),
                "terminal",
                query,
                embedder,
                cache_scope="tenant-b/provider-a/tools-v1",
                min_score=0.0,
                now_epoch=1783497601 + 60,
            )

        self.assertEqual(ingest_result["imported"], 3)
        self.assertTrue(result["matched"])
        self.assertFalse(result["reusable"])
        self.assertEqual(result["candidates"][0]["record_key"], "semantic-doc")
        self.assertTrue(
            all(
                item["operation_kind"] == "web_search_curl"
                for item in result["candidates"]
            )
        )
        self.assertEqual(isolated["candidate_count"], 0)
        self.assertFalse(isolated["matched"])
        self.assertEqual(other_scope["candidate_count"], 0)
        self.assertFalse(other_scope["matched"])


if __name__ == "__main__":
    unittest.main()
