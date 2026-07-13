from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tool_reuse.exact.ingest import entry_from_record
from tool_reuse.exact.matcher import match_exact
from tool_reuse.exact.normalize import normalize_exact_call
from tool_reuse.exact.redact import redact_tool_input
from tool_reuse.exact.store import ExactStore


def _curl(command: str):
    result = normalize_exact_call(
        "terminal",
        {"command": command, "kind": "TerminalAction"},
    )
    assert result is not None
    return result


class ExactV2NormalizeTests(unittest.TestCase):
    def test_runtime_flags_do_not_change_exact_key(self) -> None:
        left = _curl(
            'curl -s --max-time 15 "https://example.com/api?b=2&a=1" | head -20'
        )
        right = _curl(
            'curl "https://example.com/api?a=1&b=2" --max-time 999 -sS | head -20'
        )

        self.assertEqual(left.exact_key, right.exact_key)

    def test_output_transform_changes_exact_key(self) -> None:
        left = _curl("curl https://example.com/api | head -20")
        right = _curl("curl https://example.com/api | head -50")

        self.assertNotEqual(left.exact_key, right.exact_key)

    def test_file_output_is_match_only(self) -> None:
        call = _curl("curl https://example.com/report.pdf -o /tmp/report.pdf")

        self.assertFalse(call.replayable)
        self.assertEqual(call.replay_policy, "match_only")

    def test_browser_url_is_canonicalized(self) -> None:
        left = normalize_exact_call(
            "browser_navigate",
            {
                "kind": "BrowserNavigateAction",
                "new_tab": True,
                "url": "HTTPS://Example.COM:443/docs?b=2&a=1#section",
            },
        )
        right = normalize_exact_call(
            "browser_navigate",
            {
                "kind": "BrowserNavigateAction",
                "new_tab": True,
                "url": "https://example.com/docs?a=1&b=2",
            },
        )

        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertEqual(left.exact_key, right.exact_key)
        self.assertFalse(left.replayable)

    def test_browser_tab_mode_changes_exact_key(self) -> None:
        current_tab = normalize_exact_call(
            "browser_navigate",
            {"kind": "BrowserNavigateAction", "new_tab": False, "url": "https://example.com"},
        )
        new_tab = normalize_exact_call(
            "browser_navigate",
            {"kind": "BrowserNavigateAction", "new_tab": True, "url": "https://example.com"},
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


class ExactV2MatcherTests(unittest.TestCase):
    def test_match_reports_reusable_and_stale_separately(self) -> None:
        source = {
            "record_key": "record-1",
            "tool_name": "terminal",
            "started_at": "2026-07-08T08:00:00+00:00",
            "ended_at": "2026-07-08T08:00:01+00:00",
            "tool_input": {
                "command": "curl -s https://example.com/docs/readme.md | head -20",
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

        self.assertTrue(fresh["matched"])
        self.assertTrue(fresh["reusable"])
        self.assertIn("tool_response", fresh)
        self.assertTrue(stale["matched"])
        self.assertFalse(stale["reusable"])
        self.assertEqual(stale["reason"], "exact history exists but the selected observation is stale")


if __name__ == "__main__":
    unittest.main()
