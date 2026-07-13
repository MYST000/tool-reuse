from __future__ import annotations

import shlex
from typing import Any

from ..jsonutil import sha256_json
from ..normalize import canonicalize_url, normalize_tool_call
from .models import KEY_VERSION, ExactCall
from .policy import freshness_for_url


def normalize_exact_call(tool_name: str, tool_input: dict[str, Any]) -> ExactCall | None:
    if tool_name == "terminal":
        return _normalize_terminal_curl(tool_name, tool_input)
    if tool_name == "browser_navigate":
        return _normalize_browser_navigate(tool_name, tool_input)
    return None


def _normalize_terminal_curl(tool_name: str, tool_input: dict[str, Any]) -> ExactCall | None:
    command = tool_input.get("command")
    if not isinstance(command, str) or not _contains_curl(command):
        return None

    normalized = normalize_tool_call(tool_name, tool_input)
    fingerprint = normalized.fingerprint
    if fingerprint.get("kind") != "curl":
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
    output = {
        "postprocess": fingerprint.get("postprocess_signature", ""),
        "side_effects": fingerprint.get("side_effects", []),
    }
    canonical = {
        "key_version": KEY_VERSION,
        "tool_name": tool_name,
        "action_kind": str(tool_input.get("kind") or "TerminalAction"),
        "operation_kind": "curl_http",
        "request": request,
        "output": output,
    }
    replayable = bool(fingerprint.get("replay_safe", False))
    freshness_class, ttl_seconds = freshness_for_url(str(request.get("url") or ""))
    reason = "pure curl response" if replayable else "curl command has filesystem side effects"
    return ExactCall(
        exact_key=sha256_json(canonical),
        key_version=KEY_VERSION,
        tool_name=tool_name,
        action_kind=canonical["action_kind"],
        operation_kind="curl_http",
        canonical=canonical,
        freshness_class=freshness_class,
        ttl_seconds=ttl_seconds,
        replayable=replayable,
        replay_policy="response" if replayable else "match_only",
        reason=reason,
    )


def _normalize_browser_navigate(tool_name: str, tool_input: dict[str, Any]) -> ExactCall | None:
    url = tool_input.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        normalized_url, _, auth_scope = canonicalize_url(url)
    except ValueError:
        return None

    canonical = {
        "key_version": KEY_VERSION,
        "tool_name": tool_name,
        "action_kind": str(tool_input.get("kind") or "BrowserNavigateAction"),
        "operation_kind": "browser_navigate_url",
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
        operation_kind="browser_navigate_url",
        canonical=canonical,
        freshness_class=freshness_class,
        ttl_seconds=ttl_seconds,
        replayable=False,
        replay_policy="match_only",
        reason="browser_navigate changes tab state and its observation does not contain page content",
    )


def _contains_curl(command: str) -> bool:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    return any(token == "curl" or token.endswith("/curl") for token in tokens)
