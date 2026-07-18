from __future__ import annotations

import unittest

from tool_reuse.normalize import normalize_tool_call


def _norm(command: str):
    return normalize_tool_call(
        "terminal", {"command": command, "kind": "TerminalAction"}
    )


class CurlNormalizeTests(unittest.TestCase):
    def test_multiple_urls_and_command_prefix_are_not_exact_supported(self) -> None:
        for command in (
            "curl https://example.com/a https://example.com/b",
            "cd /tmp && curl https://example.com/search?q=test",
            "FOO=bar curl https://example.com/search?q=test",
        ):
            with self.subTest(command=command):
                call = normalize_tool_call(
                    "terminal", {"kind": "TerminalAction", "command": command}
                )
                self.assertFalse(call.fingerprint.get("exact_supported"))

    def test_non_whitelisted_search_name_is_generic(self) -> None:
        result = normalize_tool_call(
            "database_search_and_delete", {"query": "expired sessions"}
        )

        self.assertEqual(result.fingerprint["kind"], "generic")

    def test_query_order_changes_exact_key(self) -> None:
        left = _norm('curl -s "https://example.com/api?b=2&a=1"')
        right = _norm("curl https://example.com/api?a=1&b=2 -sS")

        self.assertNotEqual(left.canonical_key, right.canonical_key)
        self.assertEqual(left.fingerprint["url"], "https://example.com/api?b=2&a=1")

    def test_duplicate_query_params_are_preserved(self) -> None:
        result = _norm("curl 'https://example.com/search?tag=b&tag=a&q=x'")

        self.assertEqual(
            result.fingerprint["url"], "https://example.com/search?tag=b&tag=a&q=x"
        )
        self.assertEqual(result.fingerprint["query"]["tag"], ["b", "a"])

        reversed_result = _norm("curl 'https://example.com/search?tag=a&tag=b&q=x'")
        self.assertNotEqual(result.canonical_key, reversed_result.canonical_key)

    def test_header_names_and_spaces_are_normalized(self) -> None:
        left = _norm("curl -H 'Accept:   application/json' https://example.com/api")
        right = _norm(
            "curl --header='accept: application/json' https://example.com/api"
        )

        self.assertEqual(left.canonical_key, right.canonical_key)
        self.assertEqual(
            left.fingerprint["headers"],
            [{"name": "accept", "value": "application/json"}],
        )

    def test_repeated_header_order_changes_exact_key(self) -> None:
        left = _norm(
            "curl -H 'X-Mode: first' -H 'X-Mode: second' https://example.com/api"
        )
        right = _norm(
            "curl -H 'X-Mode: second' -H 'X-Mode: first' https://example.com/api"
        )

        self.assertNotEqual(left.canonical_key, right.canonical_key)

    def test_secret_headers_are_redacted_and_disable_exact_support(self) -> None:
        left = _norm("curl -H 'Authorization: Bearer one' https://example.com/api")
        same = _norm("curl -H 'authorization: Bearer one' https://example.com/api")
        different = _norm("curl -H 'Authorization: Bearer two' https://example.com/api")

        self.assertEqual(left.canonical_key, same.canonical_key)
        self.assertEqual(left.canonical_key, different.canonical_key)
        self.assertFalse(left.fingerprint["exact_supported"])
        self.assertFalse(different.fingerprint["exact_supported"])
        self.assertNotIn("Bearer one", str(left.fingerprint))
        self.assertEqual(left.fingerprint["secret_headers"][0]["name"], "authorization")

    def test_secret_url_query_is_redacted_and_not_exact_supported(self) -> None:
        call = _norm("curl 'https://example.com/search?q=test&api_key=secret'")

        self.assertFalse(call.fingerprint["exact_supported"])
        self.assertNotIn("api_key=secret", str(call.fingerprint))

    def test_get_data_params_match_url_query(self) -> None:
        left = _norm("curl -G -d q=openai -d page=1 https://example.com/search")
        right = _norm("curl 'https://example.com/search?q=openai&page=1'")
        late_get = _norm("curl -d q=openai -d page=1 --get https://example.com/search")

        self.assertEqual(left.canonical_key, right.canonical_key)
        self.assertEqual(late_get.canonical_key, right.canonical_key)
        self.assertEqual(left.fingerprint["method"], "GET")
        self.assertIsNone(left.fingerprint["body_hash"])

    def test_post_body_affects_exact_key(self) -> None:
        left = _norm("curl -d q=openai https://example.com/search")
        right = _norm("curl -d q=codex https://example.com/search")

        self.assertNotEqual(left.canonical_key, right.canonical_key)
        self.assertEqual(left.fingerprint["method"], "POST")
        self.assertIsNotNone(left.fingerprint["body_hash"])

    def test_follow_redirect_and_head_affect_exact_key(self) -> None:
        plain = _norm("curl https://example.com")
        follow = _norm("curl -L https://example.com")
        head = _norm("curl -I https://example.com")

        self.assertNotEqual(plain.canonical_key, follow.canonical_key)
        self.assertNotEqual(plain.canonical_key, head.canonical_key)
        self.assertTrue(follow.fingerprint["options"]["follow_redirects"])
        self.assertEqual(head.fingerprint["method"], "HEAD")

    def test_output_side_effect_marks_not_replay_safe(self) -> None:
        result = _norm(
            "curl -s https://example.com/file.txt > file.txt && head file.txt"
        )

        self.assertFalse(result.fingerprint["replay_safe"])
        self.assertEqual(
            result.fingerprint["side_effects"],
            [{"kind": "shell_redirect", "target": "file.txt"}],
        )
        self.assertEqual(result.fingerprint["postprocess_signature"], "head file.txt")

    def test_pipe_postprocess_is_replay_safe_and_part_of_key(self) -> None:
        left = _norm("curl -s https://example.com/file.txt | head -5")
        right = _norm("curl -s https://example.com/file.txt | head -10")

        self.assertTrue(left.fingerprint["replay_safe"])
        self.assertEqual(left.fingerprint["postprocess_signature"], "head -5")
        self.assertNotEqual(left.canonical_key, right.canonical_key)


if __name__ == "__main__":
    unittest.main()
