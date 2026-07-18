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


def is_web_search_url(url: str) -> bool:
    """Return whether a URL represents a web search request."""
    return bool(SEARCH_RE.search(url))


KNOWN_OBSERVATION_KINDS = frozenset(
    {
        "BrowserObservation",
        "FileEditorObservation",
        "FinishObservation",
        "SearchObservation",
        "TerminalObservation",
        "ThinkObservation",
        "ToolObservation",
    }
)


def response_status(
    tool_response: dict[str, Any], tool_name: str | None = None
) -> tuple[bool, str]:
    kind = tool_response.get("kind")
    if kind not in KNOWN_OBSERVATION_KINDS:
        return False, "missing or unrecognized observation kind"
    expected_kind = _expected_observation_kind(tool_name)
    if expected_kind is not None and kind not in expected_kind:
        return False, f"observation kind {kind!r} does not match tool {tool_name!r}"
    if tool_response.get("is_error") is not False:
        return False, "tool_response.is_error is not explicitly false"
    if tool_response.get("timeout") is True:
        return False, "tool_response.timeout=true"
    exit_code = tool_response.get("exit_code")
    if exit_code is not None:
        if not isinstance(exit_code, int):
            return False, "tool_response.exit_code is not an integer"
        if exit_code != 0:
            return False, f"exit_code={exit_code}"
    return True, "success"


def origin_status(
    record: dict[str, Any], *, trust_legacy: bool = False
) -> tuple[bool, str]:
    """Accept real executions and only explicitly trusted legacy traces."""
    execution_source = record.get("execution_source")
    if execution_source == "tool":
        return True, "trusted tool origin"
    if execution_source is not None:
        return False, f"execution_source={execution_source!r} is not an origin"
    required_legacy_fields = ("record_key", "started_at", "ended_at")
    if trust_legacy and all(
        isinstance(record.get(field), str) for field in required_legacy_fields
    ):
        return True, "trusted legacy tool origin"
    return False, "missing trusted execution provenance"


def _expected_observation_kind(tool_name: str | None) -> frozenset[str] | None:
    if tool_name is None:
        return None
    if tool_name == "terminal":
        return frozenset({"TerminalObservation"})
    if tool_name.startswith("browser_"):
        return frozenset({"BrowserObservation"})
    if tool_name in {"web_search", "browser_search", "search"}:
        return frozenset({"SearchObservation", "ToolObservation"})
    return None
