from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tool_reuse.exact.ingest import entry_from_record, ingest_exact_records
from tool_reuse.exact.matcher import match_exact
from tool_reuse.exact.normalize import normalize_exact_call
from tool_reuse.exact.policy import origin_status, response_status
from tool_reuse.exact.redact import redact_tool_input
from tool_reuse.exact.store import ExactStore
from tool_reuse.hook_query import deny_with_cache


def _curl(command: str):
    result = normalize_exact_call(
        "terminal",
        {"command": command, "kind": "TerminalAction"},
    )
    assert result is not None
    return result


class ExactV2NormalizeTests(unittest.TestCase):
    def test_runtime_flags_are_not_exact_supported(self) -> None:
        for flag in ("--max-time 15", "--connect-timeout 2", "--retry 3"):
            with self.subTest(flag=flag):
                self.assertIsNone(
                    normalize_exact_call(
                        "terminal",
                        {
                            "kind": "TerminalAction",
                            "command": (
                                f"curl {flag} 'https://example.com/search?q=runtime'"
                            ),
                        },
                    )
                )

    def test_output_transform_changes_exact_key(self) -> None:
        left = _curl("curl 'https://example.com/search?q=cache' | head -20")
        right = _curl("curl 'https://example.com/search?q=cache' | head -50")

        self.assertNotEqual(left.exact_key, right.exact_key)

    def test_file_output_is_match_only(self) -> None:
        call = _curl("curl 'https://example.com/search?q=report' -o /tmp/report.txt")

        self.assertFalse(call.replayable)
        self.assertEqual(call.replay_policy, "match_only")

    def test_browser_url_is_canonicalized(self) -> None:
        left = normalize_exact_call(
            "browser_navigate",
            {
                "kind": "BrowserNavigateAction",
                "new_tab": True,
                "url": "HTTPS://Example.COM:443/search?a=1&b=2#section",
            },
        )
        right = normalize_exact_call(
            "browser_navigate",
            {
                "kind": "BrowserNavigateAction",
                "new_tab": True,
                "url": "https://example.com/search?a=1&b=2",
            },
        )

        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertEqual(left.exact_key, right.exact_key)
        self.assertFalse(left.replayable)

    def test_browser_tab_mode_changes_exact_key(self) -> None:
        current_tab = normalize_exact_call(
            "browser_navigate",
            {
                "kind": "BrowserNavigateAction",
                "new_tab": False,
                "url": "https://example.com/search?q=test",
            },
        )
        new_tab = normalize_exact_call(
            "browser_navigate",
            {
                "kind": "BrowserNavigateAction",
                "new_tab": True,
                "url": "https://example.com/search?q=test",
            },
        )

        self.assertIsNotNone(current_tab)
        self.assertIsNotNone(new_tab)
        self.assertNotEqual(current_tab.exact_key, new_tab.exact_key)

    def test_state_dependent_browser_action_is_unsupported(self) -> None:
        result = normalize_exact_call(
            "browser_get_state",
            {"kind": "BrowserGetStateAction", "include_screenshot": False},
        )

        self.assertIsNone(result)

    def test_non_search_curl_and_browser_calls_are_unsupported(self) -> None:
        curl = normalize_exact_call(
            "terminal",
            {"kind": "TerminalAction", "command": "curl https://example.com/docs"},
        )
        browser = normalize_exact_call(
            "browser_navigate",
            {
                "kind": "BrowserNavigateAction",
                "url": "https://example.com/docs",
            },
        )

        self.assertIsNone(curl)
        self.assertIsNone(browser)

    def test_unknown_curl_options_are_unsupported(self) -> None:
        result = normalize_exact_call(
            "terminal",
            {
                "kind": "TerminalAction",
                "command": "curl --range 0-99 'https://example.com/search?q=test'",
            },
        )

        self.assertIsNone(result)

    def test_multiple_urls_and_shell_prefix_are_unsupported(self) -> None:
        for command in (
            "curl https://example.com/search?q=a https://example.com/search?q=b",
            "cd /tmp && curl https://example.com/search?q=a",
        ):
            with self.subTest(command=command):
                self.assertIsNone(
                    normalize_exact_call(
                        "terminal",
                        {"kind": "TerminalAction", "command": command},
                    )
                )

    def test_stateful_search_curl_is_not_replayable(self) -> None:
        for command in (
            "curl 'https://example.com/search?q=test' | tee output.txt",
            "curl 'https://example.com/search?q=test' && touch done",
            "curl -X DELETE 'https://example.com/search?q=test'",
        ):
            with self.subTest(command=command):
                self.assertFalse(_curl(command).replayable)

    def test_authenticated_search_calls_are_unsupported(self) -> None:
        for command in (
            "curl -H 'Authorization: Bearer token' 'https://example.com/search?q=test'",
            "curl 'https://example.com/search?q=test&api_key=secret'",
            "curl -u user:pass 'https://example.com/search?q=test'",
        ):
            with self.subTest(command=command):
                self.assertIsNone(
                    normalize_exact_call(
                        "terminal", {"kind": "TerminalAction", "command": command}
                    )
                )

    def test_only_explicit_search_tools_are_supported(self) -> None:
        self.assertIsNone(
            normalize_exact_call(
                "database_search_and_delete", {"query": "expired sessions"}
            )
        )

    def test_web_search_is_replayable_and_normalized(self) -> None:
        left = normalize_exact_call(
            "web_search",
            {
                "kind": "SearchAction",
                "query": "  Today   Zhengzhou weather ",
                "domains": ["weather.gov", "example.com"],
                "max_results": 5,
            },
        )
        right = normalize_exact_call(
            "web_search",
            {
                "kind": "SearchAction",
                "query": "  Today   Zhengzhou weather ",
                "domains": ["weather.gov", "example.com"],
                "max_results": 5,
            },
        )

        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertEqual(left.exact_key, right.exact_key)
        self.assertTrue(left.replayable)
        self.assertEqual(left.operation_kind, "web_search")

    def test_web_search_case_and_list_order_change_exact_key(self) -> None:
        base = normalize_exact_call(
            "web_search", {"query": "US", "domains": ["a.example", "b.example"]}
        )
        changed_case = normalize_exact_call(
            "web_search", {"query": "us", "domains": ["a.example", "b.example"]}
        )
        changed_order = normalize_exact_call(
            "web_search", {"query": "US", "domains": ["b.example", "a.example"]}
        )

        self.assertIsNotNone(base)
        self.assertIsNotNone(changed_case)
        self.assertIsNotNone(changed_order)
        self.assertNotEqual(base.exact_key, changed_case.exact_key)
        self.assertNotEqual(base.exact_key, changed_order.exact_key)

    def test_web_search_response_parameters_change_exact_key(self) -> None:
        small = normalize_exact_call(
            "web_search", {"query": "Zhengzhou weather", "max_results": 3}
        )
        large = normalize_exact_call(
            "web_search", {"query": "Zhengzhou weather", "max_results": 10}
        )

        self.assertIsNotNone(small)
        self.assertIsNotNone(large)
        self.assertNotEqual(small.exact_key, large.exact_key)

    def test_cache_scope_changes_exact_key(self) -> None:
        left = normalize_exact_call(
            "web_search", {"query": "OpenHands"}, cache_scope="tenant-a/provider-a/v1"
        )
        right = normalize_exact_call(
            "web_search", {"query": "OpenHands"}, cache_scope="tenant-b/provider-a/v1"
        )

        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertNotEqual(left.exact_key, right.exact_key)

    def test_curl_credentials_are_redacted_from_stored_input(self) -> None:
        redacted = redact_tool_input(
            {
                "kind": "TerminalAction",
                "command": (
                    "curl -u user:secret -H 'Authorization: Bearer secret-token' "
                    "https://name:password@example.com/private"
                ),
            }
        )

        command = redacted["command"]
        self.assertNotIn("user:secret", command)
        self.assertNotIn("secret-token", command)
        self.assertNotIn("name:password", command)
        self.assertIn("<redacted>", command)

    def test_structured_api_key_is_redacted(self) -> None:
        redacted = redact_tool_input(
            {"query": "test", "headers": {"X-API-Key": "secret"}}
        )
        call = normalize_exact_call(
            "web_search",
            {"query": "test", "headers": {"X-API-Key": "secret"}},
        )

        self.assertEqual(redacted["headers"]["X-API-Key"], "<redacted>")
        self.assertIsNone(call)

    def test_structured_secret_values_are_unsupported_and_redacted(self) -> None:
        secret = "synthetic-secret-value"
        inputs = (
            {"query": "test", "target": f"https://user:{secret}@example.com/"},
            {"query": "test", "target": f"https://example.com/?token={secret}"},
            {"query": "test", "custom": f"Authorization: Bearer {secret}"},
        )
        for tool_input in inputs:
            with self.subTest(tool_input=tool_input):
                self.assertIsNone(normalize_exact_call("web_search", tool_input))
                self.assertNotIn(secret, str(redact_tool_input(tool_input)))


class ExactV2MatcherTests(unittest.TestCase):
    def test_read_only_lookup_does_not_create_a_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.sqlite"
            with self.assertRaises(sqlite3.OperationalError):
                match_exact(
                    str(db_path),
                    "web_search",
                    {"kind": "SearchAction", "query": "missing"},
                )

            self.assertFalse(db_path.exists())

    def test_reader_remains_available_during_uncommitted_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "cache.sqlite"
            writer = ExactStore(db_path)
            reader = ExactStore(db_path, read_only=True)
            try:
                writer.conn.execute("BEGIN IMMEDIATE")
                self.assertEqual(reader.stats()["total"], 0)
                writer.conn.rollback()
            finally:
                reader.close()
                writer.close()

    def test_response_status_requires_explicit_success_shape(self) -> None:
        hard_negatives = (
            {},
            {"kind": "TerminalObservation"},
            {"kind": "Unknown", "is_error": False},
            {"kind": "MadeUpObservation", "is_error": False},
            {"kind": "TerminalObservation", "is_error": False, "exit_code": "0"},
        )
        for response in hard_negatives:
            with self.subTest(response=response):
                self.assertFalse(response_status(response)[0])

        self.assertFalse(
            response_status(
                {"kind": "BrowserObservation", "is_error": False}, "terminal"
            )[0]
        )

        self.assertTrue(
            response_status(
                {"kind": "TerminalObservation", "is_error": False, "exit_code": 0},
                "terminal",
            )[0]
        )

    def test_cache_replacement_is_not_a_trusted_origin(self) -> None:
        trusted, reason = origin_status(
            {
                "execution_source": "hook_replacement",
                "record_key": "cache-hit",
                "started_at": "2026-07-08T08:00:00+00:00",
                "ended_at": "2026-07-08T08:00:01+00:00",
            }
        )

        self.assertFalse(trusted)
        self.assertIn("not an origin", reason)

    def test_legacy_origin_requires_explicit_trust(self) -> None:
        record = {
            "record_key": "legacy",
            "started_at": "2026-07-08T08:00:00+00:00",
            "ended_at": "2026-07-08T08:00:01+00:00",
        }

        self.assertFalse(origin_status(record)[0])
        self.assertTrue(origin_status(record, trust_legacy=True)[0])

    def test_ingest_rebuild_removes_old_unsupported_source_entries(self) -> None:
        search_record = {
            "record_key": "same-record",
            "execution_source": "tool",
            "origin_record_key": "same-record",
            "tool_name": "web_search",
            "started_at": "2026-07-08T08:00:00+00:00",
            "ended_at": "2026-07-08T08:00:01+00:00",
            "tool_input": {"kind": "SearchAction", "query": "OpenHands"},
            "tool_response": {
                "kind": "ToolObservation",
                "is_error": False,
                "content": [{"type": "text", "text": "cached"}],
            },
        }
        unsupported_record = {
            **search_record,
            "tool_name": "terminal",
            "tool_input": {
                "kind": "TerminalAction",
                "command": "curl https://example.com/docs",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            records_path = Path(tmpdir) / "tool_calls.jsonl"
            db_path = Path(tmpdir) / "exact.sqlite"
            records_path.write_text(json.dumps(search_record) + "\n", encoding="utf-8")
            first = ingest_exact_records(
                records_path, db_path, trust_legacy_origins=True
            )
            records_path.write_text(
                json.dumps(unsupported_record) + "\n", encoding="utf-8"
            )
            second = ingest_exact_records(
                records_path, db_path, trust_legacy_origins=True
            )

            self.assertEqual(first["imported"], 1)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(second["database"]["total"], 0)

    def test_response_containing_a_secret_is_not_persisted(self) -> None:
        source = {
            "record_key": "secret-response",
            "origin_record_key": "secret-response",
            "execution_source": "tool",
            "tool_name": "web_search",
            "started_at": "2026-07-08T08:00:00+00:00",
            "ended_at": "2026-07-08T08:00:01+00:00",
            "tool_input": {"kind": "SearchAction", "query": "OpenHands"},
            "tool_response": {
                "kind": "ToolObservation",
                "is_error": False,
                "content": [
                    {"type": "text", "text": "Cookie: sessionid=synthetic-secret"}
                ],
            },
        }

        self.assertIsNone(entry_from_record(source, "/tmp/tool_calls.jsonl"))

    def test_hook_returns_cached_response_as_tool_replacement(self) -> None:
        response = {
            "kind": "TerminalObservation",
            "content": [{"type": "text", "text": "cached response"}],
            "command": "curl 'https://example.com/search?q=cache'",
            "exit_code": 0,
        }
        result = {
            "exact_key": "key",
            "selected": {
                "record_key": "record",
                "ended_at": "2026-07-08T08:00:01+00:00",
                "freshness_class": "search",
                "ttl_seconds": 600,
            },
            "tool_response": response,
        }

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = deny_with_cache(result)

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["decision"], "deny")
        self.assertEqual(payload["toolResponse"], response)
        self.assertEqual(
            payload["toolResponseMetadata"],
            {
                "cacheHitType": "exact",
                "cacheRecordKey": "record",
                "cacheObservedAt": "2026-07-08T08:00:01+00:00",
            },
        )

    def test_match_reports_reusable_and_stale_separately(self) -> None:
        source = {
            "record_key": "record-1",
            "execution_source": "tool",
            "origin_record_key": "record-1",
            "tool_name": "terminal",
            "started_at": "2026-07-08T08:00:00+00:00",
            "ended_at": "2026-07-08T08:00:01+00:00",
            "tool_input": {
                "command": "curl -s 'https://example.com/search?q=cache' | head -20",
                "kind": "TerminalAction",
            },
            "tool_response": {
                "kind": "TerminalObservation",
                "is_error": False,
                "timeout": False,
                "exit_code": 0,
                "content": [{"type": "text", "text": "cached response"}],
            },
        }
        entry = entry_from_record(source, "/tmp/tool_calls.jsonl")
        self.assertIsNotNone(entry)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "exact.sqlite")
            store = ExactStore(db_path)
            store.upsert(entry)
            store.commit()
            store.close()

            fresh = match_exact(
                db_path,
                "terminal",
                source["tool_input"],
                now_epoch=entry.observed_at_epoch + 60,
            )
            stale = match_exact(
                db_path,
                "terminal",
                source["tool_input"],
                now_epoch=entry.expires_at_epoch + 1,
            )
            isolated = match_exact(
                db_path,
                "terminal",
                source["tool_input"],
                cache_scope="another-tenant/provider/v1",
                now_epoch=entry.observed_at_epoch + 60,
            )

        self.assertTrue(fresh["matched"])
        self.assertTrue(fresh["reusable"])
        self.assertIn("tool_response", fresh)
        self.assertTrue(stale["matched"])
        self.assertFalse(stale["reusable"])
        self.assertEqual(
            stale["reason"],
            "exact history exists but the selected observation is stale",
        )
        self.assertFalse(isolated["matched"])


if __name__ == "__main__":
    unittest.main()
