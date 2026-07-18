from __future__ import annotations

import re
import shlex
from typing import Any

from ..jsonutil import sha256_json
from ..normalize import canonicalize_url, normalize_tool_call
from .models import KEY_VERSION, ExactCall
from .policy import freshness_for_url, is_web_search_url
from .redact import contains_secret_text


def normalize_exact_call(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    cache_scope: str = "local",
) -> ExactCall | None:
    if not cache_scope.strip():
        return None
    if tool_name == "terminal":
        return _normalize_terminal_curl(tool_name, tool_input, cache_scope)
    if tool_name == "browser_navigate":
        return _normalize_browser_navigate(tool_name, tool_input, cache_scope)
    if tool_name.lower() in READ_ONLY_SEARCH_TOOLS:
        return _normalize_web_search(tool_name, tool_input, cache_scope)
    return None


_QUERY_FIELDS = ("query", "search_query", "q", "keywords", "text")
READ_ONLY_SEARCH_TOOLS = frozenset({"web_search", "browser_search", "search"})
_SECRET_FIELD_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)


def _normalize_web_search(
    tool_name: str, tool_input: dict[str, Any], cache_scope: str
) -> ExactCall | None:
    if _contains_secret_field(tool_input):
        return None
    query_field, query = _find_query(tool_input)
    if query_field is None or query is None:
        return None
    normalized_input = _canonical_search_value(tool_input)
    canonical = {
        "key_version": KEY_VERSION,
        "cache_scope": cache_scope,
        "tool_name": tool_name,
        "action_kind": str(tool_input.get("kind") or "SearchAction"),
        "operation_kind": "web_search",
        "input": normalized_input,
    }
    return ExactCall(
        exact_key=sha256_json(canonical),
        key_version=KEY_VERSION,
        tool_name=tool_name,
        action_kind=canonical["action_kind"],
        operation_kind="web_search",
        canonical=canonical,
        freshness_class="search",
        ttl_seconds=10 * 60,
        replayable=True,
        replay_policy="response",
        reason="read-only web search response",
    )


def _find_query(value: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in _QUERY_FIELDS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return key, item
    return None, None


def _canonical_search_value(value: Any, field_name: str = "") -> Any:
    lowered = re.sub(r"[^a-z0-9]+", "_", field_name.lower()).strip("_")
    if any(part in lowered for part in _SECRET_FIELD_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            key: _canonical_search_value(item, key)
            for key, item in sorted(value.items())
            if key != "kind"
        }
    if isinstance(value, list):
        return [_canonical_search_value(item) for item in value]
    return value


def _normalize_terminal_curl(
    tool_name: str, tool_input: dict[str, Any], cache_scope: str
) -> ExactCall | None:
    command = tool_input.get("command")
    if not isinstance(command, str) or not _contains_curl(command):
        return None

    normalized = normalize_tool_call(tool_name, tool_input)
    fingerprint = normalized.fingerprint
    if fingerprint.get("kind") != "curl" or not fingerprint.get(
        "exact_supported", False
    ):
        return None

    request = {
        "method": fingerprint.get("method"),
        "url": fingerprint.get("url"),
        "headers": fingerprint.get("headers", []),
        "secret_headers": fingerprint.get("secret_headers", []),
        "body_hash": fingerprint.get("body_hash"),
        "auth_scope": fingerprint.get("auth_scope", []),
        "options": fingerprint.get("options", {}),
    }
    if not is_web_search_url(str(request.get("url") or "")):
        return None
    output = {
        "postprocess": fingerprint.get("postprocess_signature", ""),
        "side_effects": fingerprint.get("side_effects", []),
    }
    canonical = {
        "key_version": KEY_VERSION,
        "cache_scope": cache_scope,
        "tool_name": tool_name,
        "action_kind": str(tool_input.get("kind") or "TerminalAction"),
        "operation_kind": "web_search_curl",
        "request": request,
        "output": output,
    }
    replayable = _curl_replayable(fingerprint)
    freshness_class, ttl_seconds = freshness_for_url(str(request.get("url") or ""))
    reason = (
        "pure curl response"
        if replayable
        else "curl call is stateful, dynamic, authenticated, or has side effects"
    )
    return ExactCall(
        exact_key=sha256_json(canonical),
        key_version=KEY_VERSION,
        tool_name=tool_name,
        action_kind=canonical["action_kind"],
        operation_kind="web_search_curl",
        canonical=canonical,
        freshness_class=freshness_class,
        ttl_seconds=ttl_seconds,
        replayable=replayable,
        replay_policy="response" if replayable else "match_only",
        reason=reason,
    )


def _normalize_browser_navigate(
    tool_name: str, tool_input: dict[str, Any], cache_scope: str
) -> ExactCall | None:
    url = tool_input.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        normalized_url, _, auth_scope = canonicalize_url(url)
    except ValueError:
        return None

    if not is_web_search_url(normalized_url):
        return None
    canonical = {
        "key_version": KEY_VERSION,
        "cache_scope": cache_scope,
        "tool_name": tool_name,
        "action_kind": str(tool_input.get("kind") or "BrowserNavigateAction"),
        "operation_kind": "web_search_browser",
        "url": normalized_url,
        "new_tab": bool(tool_input.get("new_tab", False)),
        "auth_scope": auth_scope,
    }
    freshness_class, ttl_seconds = freshness_for_url(normalized_url)
    return ExactCall(
        exact_key=sha256_json(canonical),
        key_version=KEY_VERSION,
        tool_name=tool_name,
        action_kind=canonical["action_kind"],
        operation_kind="web_search_browser",
        canonical=canonical,
        freshness_class=freshness_class,
        ttl_seconds=ttl_seconds,
        replayable=False,
        replay_policy="match_only",
        reason=(
            "browser_navigate changes tab state and its observation does not "
            "contain page content"
        ),
    )


def _contains_curl(command: str) -> bool:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    return any(token == "curl" or token.endswith("/curl") for token in tokens)


def _curl_replayable(fingerprint: dict[str, Any]) -> bool:
    return bool(
        fingerprint.get("replay_safe", False)
        and fingerprint.get("method") in {"GET", "HEAD"}
        and is_web_search_url(str(fingerprint.get("url") or ""))
        and not fingerprint.get("body_parts")
        and not fingerprint.get("auth_scope")
        and not fingerprint.get("secret_headers")
    )


def _contains_secret_field(value: Any, field_name: str = "") -> bool:
    lowered = re.sub(r"[^a-z0-9]+", "_", field_name.lower()).strip("_")
    if any(part in lowered for part in _SECRET_FIELD_PARTS):
        return True
    if isinstance(value, dict):
        return any(_contains_secret_field(item, key) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    if isinstance(value, str):
        return contains_secret_text(value)
    return False
