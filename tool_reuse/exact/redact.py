from __future__ import annotations

import re
import shlex
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "password",
    "passwd",
    "token",
    "access_token",
    "api_key",
    "apikey",
    "x_api_key",
}
SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key", "api-key"}
SENSITIVE_CURL_OPTIONS = {"-u", "--user", "-b", "--cookie"}
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|cookie\s*[:=]\s*|"
    r"(?:api[_-]?key|access[_-]?token|password|secret|token)\s*[:=]\s*)"
    r"[^\s,;\"'}]+"
)
AUTH_VALUE_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=-]+")
TOKEN_VALUE_RE = re.compile(
    r"(?:\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b|"
    r"\b(?:sk|ghp|github_pat)_[a-zA-Z0-9_-]{8,}\b)"
)
SENSITIVE_QUERY_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "auth",
    "cookie",
    "password",
    "secret",
    "token",
}


def redact_tool_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    redacted = _redact_value(tool_input)
    command = redacted.get("command")
    if isinstance(command, str):
        redacted["command"] = _redact_curl_command(command)
    return redacted


def source_metadata(
    source: dict[str, Any], safe_tool_input: dict[str, Any]
) -> dict[str, Any]:
    return {
        key: value
        for key, value in source.items()
        if key not in {"tool_input", "tool_response"}
    } | {"tool_input": safe_tool_input}


def redact_semantic_text(text: str) -> str:
    """Remove common credential assignments before local or remote embedding."""
    redacted = SENSITIVE_TEXT_RE.sub(r"\1<redacted>", text)
    redacted = AUTH_VALUE_RE.sub("<redacted>", redacted)
    return TOKEN_VALUE_RE.sub("<redacted>", redacted)


def contains_secret_text(text: str) -> bool:
    if SENSITIVE_TEXT_RE.search(text) or AUTH_VALUE_RE.search(text):
        return True
    if TOKEN_VALUE_RE.search(text):
        return True
    if not text.startswith(("http://", "https://")):
        return False
    parts = urlsplit(text)
    if parts.username or parts.password:
        return True
    return any(_is_sensitive_query_key(key) for key, _ in parse_qsl(parts.query))


def _redact_value(value: Any, key: str | None = None) -> Any:
    normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") if key else None
    if normalized_key in SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, dict):
        return {
            item_key: _redact_value(item_value, item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return _redact_url_userinfo(value)
        return redact_semantic_text(value)
    return value


def _redact_curl_command(command: str) -> str:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "<redacted: unparseable shell command>"

    result: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"-H", "--header"} and i + 1 < len(tokens):
            result.extend([token, _redact_header(tokens[i + 1])])
            i += 2
            continue
        if token.startswith("--header="):
            result.append("--header=" + _redact_header(token.split("=", 1)[1]))
            i += 1
            continue
        if token.startswith("-H") and token != "-H":
            result.append("-H" + _redact_header(token[2:]))
            i += 1
            continue
        if token in SENSITIVE_CURL_OPTIONS and i + 1 < len(tokens):
            result.extend([token, "<redacted>"])
            i += 2
            continue
        if token.startswith("--user=") or token.startswith("--cookie="):
            result.append(token.split("=", 1)[0] + "=<redacted>")
            i += 1
            continue
        if token.startswith("-u") and token != "-u":
            result.append("-u<redacted>")
            i += 1
            continue
        if token.startswith("-b") and token != "-b":
            result.append("-b<redacted>")
            i += 1
            continue
        result.append(_redact_url_userinfo(token))
        i += 1
    return shlex.join(result)


def _redact_header(header: str) -> str:
    if ":" not in header:
        return header
    name, value = header.split(":", 1)
    if name.strip().lower() in SENSITIVE_HEADERS:
        return f"{name}: <redacted>"
    return f"{name}:{value}"


def _redact_url_userinfo(token: str) -> str:
    if not token.startswith(("http://", "https://")):
        return token
    parts = urlsplit(token)
    hostname = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = hostname if port is None else f"{hostname}:{port}"
    query = urlencode(
        [
            (key, "<redacted>" if _is_sensitive_query_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _is_sensitive_query_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized in SENSITIVE_QUERY_KEYS
