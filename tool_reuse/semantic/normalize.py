from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..exact.normalize import normalize_exact_call
from ..exact.policy import freshness_for_url, is_web_search_url
from ..exact.redact import redact_semantic_text
from ..normalize import canonicalize_url, normalize_tool_call
from .models import SEMANTIC_VERSION, SemanticCall


QUERY_FIELDS = ("query", "search_query", "q", "keywords", "text")
MAX_BROWSER_CONTENT_CHARS = 12_000
URL_BLOCK_RE = re.compile(r"<url>\s*(.*?)\s*</url>", re.IGNORECASE | re.DOTALL)
WEBPAGE_BLOCK_RE = re.compile(
    r"<webpage_content>\s*(.*?)\s*</webpage_content>",
    re.IGNORECASE | re.DOTALL,
)


def normalize_semantic_call(
    tool_name: str, tool_input: dict[str, Any]
) -> SemanticCall | None:
    if tool_name == "browser_navigate":
        url = tool_input.get("url")
        if isinstance(url, str):
            try:
                normalized_url, _, auth_scope = canonicalize_url(url)
            except ValueError:
                return None
            if auth_scope:
                return None
            return _from_browser_url(tool_name, tool_input, normalized_url)
    exact = normalize_exact_call(tool_name, tool_input)
    if exact and exact.operation_kind == "web_search_curl":
        return _from_curl(tool_name, tool_input)
    if exact and exact.operation_kind == "web_search_browser":
        return _from_browser_url(tool_name, tool_input, exact.canonical["url"])
    if exact and exact.operation_kind == "web_search":
        query = _find_query(tool_input)
        if query:
            return _from_search(tool_name, tool_input, query)
    if tool_name == "terminal":
        normalized = normalize_tool_call(tool_name, tool_input)
        fingerprint = normalized.fingerprint
        if _semantic_curl_supported(fingerprint):
            return _from_curl(tool_name, tool_input)
    return None


def normalize_semantic_record(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_response: dict[str, Any],
) -> SemanticCall | None:
    """Normalize a stored call using observations when the action lacks a URL."""
    call = normalize_semantic_call(tool_name, tool_input)
    if call is not None:
        return call
    if tool_name not in {"browser_get_content", "browser_get_state"}:
        return None

    response_text = _response_text(tool_response)
    if tool_name == "browser_get_state":
        try:
            state = json.loads(response_text)
        except json.JSONDecodeError:
            return None
        url = state.get("url") if isinstance(state, dict) else None
        if not isinstance(url, str) or not url:
            return None
        try:
            return _from_browser_content(
                tool_name, tool_input, url, response_text, source_action=tool_name
            )
        except ValueError:
            return None

    url_match = URL_BLOCK_RE.search(response_text)
    content_match = WEBPAGE_BLOCK_RE.search(response_text)
    if not url_match or not content_match:
        return None
    url = url_match.group(1).strip()
    content = content_match.group(1).strip()
    if not url or not content:
        return None
    try:
        return _from_browser_content(
            tool_name, tool_input, url, content, source_action=tool_name
        )
    except ValueError:
        return None


def _from_curl(tool_name: str, tool_input: dict[str, Any]) -> SemanticCall:
    normalized = normalize_tool_call(tool_name, tool_input)
    fp = normalized.fingerprint
    url = str(fp["url"])
    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    postprocess = str(fp.get("postprocess_signature") or "")
    semantic_text = redact_semantic_text(
        _clean(
            " ".join(
                [
                    "web fetch http",
                    str(fp.get("method") or "GET"),
                    parts.hostname or "",
                    _path_words(parts.path),
                    " ".join(f"{key} {value}" for key, value in query_pairs),
                    postprocess,
                ]
            )
        )
    )
    freshness_class, ttl_seconds = freshness_for_url(url)
    return SemanticCall(
        semantic_version=SEMANTIC_VERSION,
        tool_name=tool_name,
        action_kind=str(tool_input.get("kind") or "TerminalAction"),
        operation_kind=(
            "web_search_curl" if is_web_search_url(url) else "web_fetch_curl"
        ),
        semantic_text=semantic_text,
        metadata={
            "url": url,
            "host": (parts.hostname or "").lower(),
            "path": parts.path or "/",
            "method": fp.get("method"),
            "query": fp.get("query", {}),
            "postprocess": postprocess,
        },
        freshness_class=freshness_class,
        ttl_seconds=ttl_seconds,
    )


def _from_browser_url(
    tool_name: str, tool_input: dict[str, Any], url: str
) -> SemanticCall:
    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    semantic_text = redact_semantic_text(
        _clean(
            " ".join(
                [
                    "browser navigate web page",
                    parts.hostname or "",
                    _path_words(parts.path),
                    " ".join(f"{key} {value}" for key, value in query_pairs),
                ]
            )
        )
    )
    freshness_class, ttl_seconds = freshness_for_url(url)
    return SemanticCall(
        semantic_version=SEMANTIC_VERSION,
        tool_name=tool_name,
        action_kind=str(tool_input.get("kind") or "BrowserNavigateAction"),
        operation_kind=(
            "web_search_browser" if is_web_search_url(url) else "browser_page"
        ),
        semantic_text=semantic_text,
        metadata={
            "url": url,
            "host": (parts.hostname or "").lower(),
            "path": parts.path or "/",
            "new_tab": bool(tool_input.get("new_tab", False)),
        },
        freshness_class=freshness_class,
        ttl_seconds=ttl_seconds,
    )


def _from_browser_content(
    tool_name: str,
    tool_input: dict[str, Any],
    url: str,
    content: str,
    *,
    source_action: str,
) -> SemanticCall:
    normalized_url, parts, auth_scope = canonicalize_url(url)
    if auth_scope:
        raise ValueError(
            "authenticated browser content requires an explicit auth scope"
        )
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    indexed_content = content[:MAX_BROWSER_CONTENT_CHARS]
    semantic_text = redact_semantic_text(
        _clean(
            " ".join(
                [
                    "browser web page content",
                    parts.hostname,
                    _path_words(parts.path),
                    " ".join(f"{key} {value}" for key, value in query_pairs),
                    indexed_content,
                ]
            )
        )
    )
    freshness_class, ttl_seconds = freshness_for_url(normalized_url)
    return SemanticCall(
        semantic_version=SEMANTIC_VERSION,
        tool_name=tool_name,
        action_kind=str(tool_input.get("kind") or "BrowserGetContentAction"),
        operation_kind=(
            "web_search_browser" if is_web_search_url(url) else "browser_page"
        ),
        semantic_text=semantic_text,
        metadata={
            "url": normalized_url,
            "host": parts.hostname.lower(),
            "path": parts.path or "/",
            "source_action": source_action,
            "content_chars": len(content),
            "indexed_content_chars": len(indexed_content),
            "start_from_char": tool_input.get("start_from_char", 0),
            "extract_links": bool(tool_input.get("extract_links", False)),
        },
        freshness_class=freshness_class,
        ttl_seconds=ttl_seconds,
    )


def _semantic_curl_supported(fingerprint: dict[str, Any]) -> bool:
    return bool(
        fingerprint.get("kind") == "curl"
        and fingerprint.get("exact_supported") is True
        and fingerprint.get("replay_safe") is True
        and fingerprint.get("method") in {"GET", "HEAD"}
        and not fingerprint.get("body_parts")
        and not fingerprint.get("auth_scope")
        and not fingerprint.get("secret_headers")
        and not fingerprint.get("side_effects")
    )


def _from_search(
    tool_name: str, tool_input: dict[str, Any], query: str
) -> SemanticCall:
    normalized_query = _clean(query)
    return SemanticCall(
        semantic_version=SEMANTIC_VERSION,
        tool_name=tool_name,
        action_kind=str(tool_input.get("kind") or "SearchAction"),
        operation_kind="web_search",
        semantic_text=redact_semantic_text(f"web search query {normalized_query}"),
        metadata={
            "query": normalized_query,
            "domains": sorted(map(str, tool_input.get("domains", [])))
            if isinstance(tool_input.get("domains"), list)
            else [],
            "recency": tool_input.get("recency") or tool_input.get("date_range"),
        },
        freshness_class="search",
        ttl_seconds=10 * 60,
    )


def _find_query(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in QUERY_FIELDS:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item
        for item in value.values():
            found = _find_query(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_query(item)
            if found:
                return found
    return None


def _path_words(path: str) -> str:
    return path.replace("/", " ").replace("-", " ").replace("_", " ").replace(".", " ")


def _clean(text: str) -> str:
    return " ".join(text.lower().split())


def _response_text(tool_response: dict[str, Any]) -> str:
    content = tool_response.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )
