from __future__ import annotations

import shlex
from typing import Any
from urllib.parse import urlsplit, urlunsplit


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


def redact_tool_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    redacted = _redact_value(tool_input)
    command = redacted.get("command")
    if isinstance(command, str):
        redacted["command"] = _redact_curl_command(command)
    return redacted


def source_metadata(source: dict[str, Any], safe_tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in source.items()
        if key not in {"tool_input", "tool_response"}
    } | {"tool_input": safe_tool_input}


def _redact_value(value: Any, key: str | None = None) -> Any:
    if key and key.lower() in SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, dict):
        return {item_key: _redact_value(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _redact_curl_command(command: str) -> str:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return command

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
    if not parts.username and not parts.password:
        return token
    hostname = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
