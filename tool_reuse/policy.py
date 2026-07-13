from __future__ import annotations

import re
from datetime import datetime, timezone


VOLATILE_RE = re.compile(
    r"\b("
    r"latest|current|today|tonight|tomorrow|yesterday|now|recent|news|breaking|"
    r"price|stock|weather|sports|score|schedule|leaderboard|trending|release"
    r")\b",
    re.IGNORECASE,
)

SEARCH_RE = re.compile(
    r"\b(search|query|google|bing|duckduckgo|arxiv\.org/search|github\.com/search)\b",
    re.IGNORECASE,
)

STATIC_URL_RE = re.compile(
    r"(raw\.githubusercontent\.com|/raw/|\.md$|\.txt$|\.json$|\.yaml$|\.yml$)",
    re.IGNORECASE,
)


def classify_freshness(intent_text: str, fingerprint: dict[str, object]) -> tuple[str, int]:
    haystack = " ".join(
        str(v)
        for v in [
            intent_text,
            fingerprint.get("url"),
            fingerprint.get("host"),
            fingerprint.get("path"),
            fingerprint.get("query"),
        ]
        if v
    )
    if VOLATILE_RE.search(haystack):
        return "volatile", 5 * 60
    if SEARCH_RE.search(haystack):
        return "search", 10 * 60
    if STATIC_URL_RE.search(haystack):
        return "static_web", 24 * 60 * 60
    if fingerprint.get("kind") in {"curl", "web_search"}:
        return "web", 60 * 60
    return "generic", 24 * 60 * 60


def parse_iso_epoch(value: str | None) -> int | None:
    if not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def expires_at_epoch(ended_at: str | None, ttl_seconds: int) -> int | None:
    base = parse_iso_epoch(ended_at)
    if base is None:
        return None
    return base + ttl_seconds


def is_fresh(expires_at: int | None, now_epoch: int | None = None) -> bool:
    if expires_at is None:
        return False
    if now_epoch is None:
        now_epoch = int(datetime.now(timezone.utc).timestamp())
    return expires_at > now_epoch
