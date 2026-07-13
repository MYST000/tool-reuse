from __future__ import annotations

import re
from typing import Any


VOLATILE_RE = re.compile(
    r"\b(latest|current|today|now|recent|news|price|stock|weather|score|trending|release)\b",
    re.IGNORECASE,
)
SEARCH_RE = re.compile(
    r"(?:/search(?:/|\?|$)|[?&](?:q|query|search_query)=|google\.|bing\.|duckduckgo\.)",
    re.IGNORECASE,
)
IMMUTABLE_RE = re.compile(
    r"(?:/commit/[0-9a-f]{7,40}(?:/|$)|/raw/[0-9a-f]{40}/|arxiv\.org/(?:abs|pdf)/\d{4}\.\d+)",
    re.IGNORECASE,
)
STATIC_RE = re.compile(
    r"(?:raw\.githubusercontent\.com|github\.com/.+/(?:blob|raw)/|\.md$|\.txt$|\.json$|\.ya?ml$|\.pdf$|/docs?/|readme)",
    re.IGNORECASE,
)


def freshness_for_url(url: str) -> tuple[str, int]:
    if VOLATILE_RE.search(url):
        return "volatile", 5 * 60
    if SEARCH_RE.search(url):
        return "search", 10 * 60
    if IMMUTABLE_RE.search(url):
        return "immutable", 30 * 24 * 60 * 60
    if STATIC_RE.search(url):
        return "static", 7 * 24 * 60 * 60
    return "web", 6 * 60 * 60


def response_status(tool_response: dict[str, Any]) -> tuple[bool, str]:
    if tool_response.get("is_error") is True:
        return False, "tool_response.is_error=true"
    if tool_response.get("timeout") is True:
        return False, "tool_response.timeout=true"
    exit_code = tool_response.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return False, f"exit_code={exit_code}"
    return True, "success"
