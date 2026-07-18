from __future__ import annotations

import re
import shlex
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .jsonutil import sha256_json, stable_json
from .models import NormalizedToolCall
from .policy import classify_freshness


URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
SECRET_HEADER_RE = re.compile(r"^(authorization|cookie|x-api-key|api-key)$", re.I)
SECRET_QUERY_RE = re.compile(
    r"^(?:api[_-]?key|access[_-]?token|auth(?:orization)?|cookie|password|secret|token)$",
    re.I,
)
SHELL_CONTROL_TOKENS = {"|", "&&", "||", ";", "&"}
REDIRECT_PREFIXES = (">", ">>", "1>", "1>>", "2>", "2>>", "&>")
NOISE_FLAGS = {
    "-s",
    "--silent",
    "-S",
    "--show-error",
    "--progress-bar",
    "--no-progress-meter",
}
RUNTIME_VALUE_FLAGS = {
    "-m",
    "--max-time",
    "--connect-timeout",
    "--retry",
    "--retry-delay",
    "--retry-max-time",
}
OUTPUT_VALUE_FLAGS = {"-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar"}
DATA_FLAGS = {
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-ascii",
    "--data-urlencode",
    "-F",
    "--form",
    "--form-string",
}


def normalize_tool_call(
    tool_name: str, tool_input: dict[str, Any]
) -> NormalizedToolCall:
    if tool_name == "terminal" and isinstance(tool_input.get("command"), str):
        return _normalize_terminal(tool_name, tool_input)
    if _looks_like_web_search(tool_name, tool_input):
        return _normalize_web_search(tool_name, tool_input)
    return _normalize_generic(tool_name, tool_input)


def _normalize_terminal(
    tool_name: str, tool_input: dict[str, Any]
) -> NormalizedToolCall:
    command = tool_input["command"]
    curl_fp = _extract_curl_fingerprint(command)
    if curl_fp:
        key_payload = {
            "tool": tool_name,
            "kind": "curl",
            "request": _curl_exact_request(curl_fp),
            "postprocess": curl_fp.get("postprocess_signature", ""),
        }
        canonical_key = sha256_json(key_payload)
        intent = _intent_from_curl(curl_fp)
        freshness_class, ttl = classify_freshness(intent, curl_fp)
        return NormalizedToolCall(
            tool_name=tool_name,
            canonical_key=canonical_key,
            intent_text=intent,
            fingerprint=curl_fp | {"postprocess": key_payload["postprocess"]},
            freshness_class=freshness_class,
            ttl_seconds=ttl,
            cacheable=True,
        )

    cleaned = _normalize_shell_text(command)
    fp = {
        "kind": "terminal",
        "command": cleaned,
        "action_kind": tool_input.get("kind"),
        "working_dir": tool_input.get("working_dir"),
    }
    freshness_class, ttl = classify_freshness(cleaned, fp)
    return NormalizedToolCall(
        tool_name=tool_name,
        canonical_key=sha256_json({"tool": tool_name, "terminal": fp}),
        intent_text=f"terminal command: {cleaned}",
        fingerprint=fp,
        freshness_class=freshness_class,
        ttl_seconds=ttl,
        cacheable=True,
    )


def _normalize_web_search(
    tool_name: str, tool_input: dict[str, Any]
) -> NormalizedToolCall:
    query = _first_string(
        tool_input, ("query", "q", "search_query", "keywords", "text")
    ) or stable_json(tool_input)
    normalized_query = _normalize_query_text(query)
    fp = {
        "kind": "web_search",
        "query": normalized_query,
        "raw_query": query,
        "domains": _normalized_domains(tool_input),
        "recency": tool_input.get("recency") or tool_input.get("date_range"),
    }
    intent = f"web search: {normalized_query}"
    freshness_class, ttl = classify_freshness(intent, fp)
    return NormalizedToolCall(
        tool_name=tool_name,
        canonical_key=sha256_json({"tool": tool_name, "web_search": fp}),
        intent_text=intent,
        fingerprint=fp,
        freshness_class=freshness_class,
        ttl_seconds=ttl,
        cacheable=True,
    )


def _normalize_generic(
    tool_name: str, tool_input: dict[str, Any]
) -> NormalizedToolCall:
    fp = {"kind": "generic", "input": tool_input}
    intent = f"{tool_name}: {stable_json(tool_input)}"
    freshness_class, ttl = classify_freshness(intent, fp)
    return NormalizedToolCall(
        tool_name=tool_name,
        canonical_key=sha256_json({"tool": tool_name, "input": tool_input}),
        intent_text=intent,
        fingerprint=fp,
        freshness_class=freshness_class,
        ttl_seconds=ttl,
        cacheable=True,
    )


def _looks_like_web_search(tool_name: str, _tool_input: dict[str, Any]) -> bool:
    lowered = tool_name.lower()
    return lowered in {"web_search", "browser_search", "search"}


def _extract_curl_fingerprint(command: str) -> dict[str, Any] | None:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None

    curl_index = _find_curl_index(tokens)
    if curl_index is None:
        urls = URL_RE.findall(command)
        if not urls:
            return None
        return _curl_fingerprint_from_parts(
            method="GET",
            url=urls[0],
            headers=[],
            secret_headers=[],
            body_parts=[],
            auth_scope=[],
            options={},
            ignored_options=[],
            side_effects=[],
            postprocess_signature="",
        )

    prefix_tokens = tokens[:curl_index]
    curl_tokens = []
    tail_tokens: list[str] = []
    for pos, token in enumerate(tokens[curl_index + 1 :], start=curl_index + 1):
        if _is_shell_boundary(token):
            tail_tokens = tokens[pos:]
            break
        curl_tokens.append(token)

    method = "GET"
    explicit_method = False
    headers: list[tuple[str, str]] = []
    secret_headers: list[tuple[str, str]] = []
    body_parts: list[dict[str, str]] = []
    raw_data_parts: list[dict[str, str]] = []
    auth_scope: list[dict[str, str]] = []
    query_parts_from_flags: list[tuple[str, str]] = []
    urls: list[str] = []
    ignored_options: list[str] = []
    unsupported_arguments: list[str] = []
    side_effects: list[dict[str, str]] = _shell_side_effects(tail_tokens)
    options: dict[str, Any] = {
        "follow_redirects": False,
        "include_response_headers": False,
        "compressed": False,
    }
    get_mode = False
    i = 0
    while i < len(curl_tokens):
        token = curl_tokens[i]
        if token == "--":
            urls.extend(t for t in curl_tokens[i + 1 :] if _looks_like_url(t))
            break
        if token in {"-X", "--request"} and i + 1 < len(curl_tokens):
            method = curl_tokens[i + 1].upper()
            explicit_method = True
            i += 2
            continue
        if token.startswith("--request="):
            method = token.split("=", 1)[1].upper()
            explicit_method = True
            i += 1
            continue
        if token.startswith("-X") and len(token) > 2:
            method = token[2:].upper()
            explicit_method = True
            i += 1
            continue
        header_value = _option_attached_or_next(
            token, curl_tokens, i, {"-H", "--header"}
        )
        if header_value:
            _add_header(header_value[0], headers, secret_headers)
            i += header_value[1]
            continue
        url_value = _option_attached_or_next(token, curl_tokens, i, {"--url"})
        if url_value:
            urls.append(url_value[0])
            i += url_value[1]
            continue
        data_value = _option_attached_or_next(token, curl_tokens, i, DATA_FLAGS)
        if data_value:
            data_kind = _option_name(token)
            raw_data_parts.append({"kind": data_kind, "value": data_value[0]})
            i += data_value[1]
            continue
        json_value = _option_attached_or_next(token, curl_tokens, i, {"--json"})
        if json_value:
            body_parts.append({"kind": "--json", "value": json_value[0]})
            headers.append(("accept", "application/json"))
            headers.append(("content-type", "application/json"))
            if method == "GET" and not explicit_method:
                method = "POST"
            i += json_value[1]
            continue
        query_value = _option_attached_or_next(token, curl_tokens, i, {"--url-query"})
        if query_value:
            query_parts_from_flags.extend(_parse_query_fragment(query_value[0]))
            i += query_value[1]
            continue
        output_value = _option_attached_or_next(
            token, curl_tokens, i, OUTPUT_VALUE_FLAGS
        )
        if output_value:
            side_effects.append(
                {"kind": _option_name(token), "target": output_value[0]}
            )
            i += output_value[1]
            continue
        auth_value = _option_attached_or_next(token, curl_tokens, i, {"-u", "--user"})
        if auth_value:
            auth_scope.append({"kind": "basic_auth", "scope_required": "true"})
            i += auth_value[1]
            continue
        cookie_value = _option_attached_or_next(
            token, curl_tokens, i, {"-b", "--cookie", "--cookie-jar"}
        )
        if cookie_value:
            auth_scope.append(
                {
                    "kind": _option_name(token),
                    "scope_required": "true",
                }
            )
            i += cookie_value[1]
            continue
        user_agent_value = _option_attached_or_next(
            token, curl_tokens, i, {"-A", "--user-agent"}
        )
        if user_agent_value:
            headers.append(("user-agent", _normalize_header_value(user_agent_value[0])))
            i += user_agent_value[1]
            continue
        value_flag = _option_attached_or_next(
            token, curl_tokens, i, RUNTIME_VALUE_FLAGS
        )
        if value_flag:
            unsupported_arguments.append(_option_name(token))
            i += value_flag[1]
            continue
        if token in {"-I", "--head"} or _short_flag_bundle_has(token, "I"):
            method = "HEAD"
            options["include_response_headers"] = True
            i += 1
            continue
        if token in {"-G", "--get"}:
            get_mode = True
            if not explicit_method:
                method = "GET"
            i += 1
            continue
        if token in {"-L", "--location"} or _short_flag_bundle_has(token, "L"):
            options["follow_redirects"] = True
            i += 1
            continue
        if token in {"-i", "--include"} or _short_flag_bundle_has(token, "i"):
            options["include_response_headers"] = True
            i += 1
            continue
        if token in {"--compressed"}:
            options["compressed"] = True
            i += 1
            continue
        if token in {"-f", "--fail", "--fail-with-body"}:
            options["fail_mode"] = (
                "with_body" if token == "--fail-with-body" else "fail"
            )
            i += 1
            continue
        if token in {"-k", "--insecure"}:
            options["insecure"] = True
            i += 1
            continue
        if token in {"-O", "--remote-name", "-J", "--remote-header-name"}:
            side_effects.append({"kind": token, "target": ""})
            i += 1
            continue
        if token in NOISE_FLAGS or _is_noise_short_bundle(token):
            ignored_options.append(token)
            i += 1
            continue
        if _looks_like_url(token):
            urls.append(token)
        else:
            unsupported_arguments.append(token)
        i += 1

    if not urls:
        urls = URL_RE.findall(command)
    if not urls:
        return None
    if get_mode:
        for part in raw_data_parts:
            query_parts_from_flags.extend(_parse_query_fragment(part["value"]))
        if not explicit_method:
            method = "GET"
    else:
        body_parts.extend(raw_data_parts)
        if raw_data_parts and method == "GET" and not explicit_method:
            method = "POST"
    url = _append_query_parts(urls[0], query_parts_from_flags)
    return _curl_fingerprint_from_parts(
        method=method,
        url=url,
        headers=headers,
        secret_headers=secret_headers,
        body_parts=body_parts,
        auth_scope=auth_scope,
        options=options,
        ignored_options=ignored_options,
        side_effects=side_effects,
        postprocess_signature=_postprocess_signature_from_tokens(tail_tokens),
        exact_supported=(
            not prefix_tokens and not unsupported_arguments and len(urls) == 1
        ),
        replay_safe=(
            not prefix_tokens
            and len(urls) == 1
            and not side_effects
            and not unsupported_arguments
            and _postprocess_replay_safe(tail_tokens)
            and not _has_dynamic_shell_input(command)
        ),
    )


def _curl_fingerprint_from_parts(
    method: str,
    url: str,
    headers: list[tuple[str, str]],
    secret_headers: list[tuple[str, str]],
    body_parts: list[dict[str, str]],
    auth_scope: list[dict[str, str]],
    options: dict[str, Any],
    ignored_options: list[str],
    side_effects: list[dict[str, str]],
    postprocess_signature: str,
    exact_supported: bool = True,
    replay_safe: bool | None = None,
) -> dict[str, Any]:
    normalized_url, url_parts, url_auth_scope = canonicalize_url(url)
    query_pairs = parse_qsl(url_parts.query, keep_blank_values=True)
    all_auth_scope = auth_scope + url_auth_scope
    normalized_headers = headers
    normalized_secret_headers = secret_headers
    normalized_options = {
        k: v for k, v in sorted(options.items()) if v not in (False, None, "", [])
    }
    return {
        "kind": "curl",
        "method": method.upper(),
        "url": normalized_url,
        "scheme": url_parts.scheme.lower(),
        "host": (url_parts.hostname or "").lower(),
        "path": url_parts.path or "/",
        "query_pairs": query_pairs,
        "query": _query_map(query_pairs),
        "headers": [
            {"name": name, "value": value} for name, value in normalized_headers
        ],
        "secret_headers": [
            {"name": name, "value_sha256": value_hash}
            for name, value_hash in normalized_secret_headers
        ],
        "body_parts": body_parts,
        "body_hash": sha256_json(body_parts) if body_parts else None,
        "auth_scope": all_auth_scope,
        "options": normalized_options,
        "ignored_options": ignored_options,
        "side_effects": side_effects,
        "postprocess_signature": postprocess_signature,
        "exact_supported": bool(
            exact_supported and not all_auth_scope and not normalized_secret_headers
        ),
        "replay_safe": bool(
            (not side_effects if replay_safe is None else replay_safe)
            and not all_auth_scope
            and not normalized_secret_headers
        ),
    }


def canonicalize_url(url: str):
    stripped = url.strip().rstrip(".,")
    parts = urlsplit(stripped)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("URL must be an absolute HTTP(S) URL")
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    query_pairs: list[tuple[str, str]] = []
    auth_scope: list[dict[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if SECRET_QUERY_RE.match(key):
            query_pairs.append((key, "<redacted>"))
            auth_scope.append({"kind": "url_query_secret", "name": key.lower()})
        else:
            query_pairs.append((key, value))
    query = urlencode(query_pairs, doseq=True)
    path = parts.path or "/"
    normalized = urlunsplit((scheme, netloc, path, query, ""))
    if parts.username or parts.password:
        auth_scope.append(
            {
                "kind": "url_userinfo",
                "scope_required": "true",
            }
        )
    return normalized, urlsplit(normalized), auth_scope


def _curl_exact_request(fp: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": fp.get("method"),
        "url": fp.get("url"),
        "headers": fp.get("headers", []),
        "secret_headers": fp.get("secret_headers", []),
        "body_hash": fp.get("body_hash"),
        "auth_scope": fp.get("auth_scope", []),
        "options": fp.get("options", {}),
    }


def _postprocess_signature_from_tokens(tail_tokens: list[str]) -> str:
    if not tail_tokens:
        return ""
    for marker in ("|", "&&", ";"):
        if marker in tail_tokens:
            idx = tail_tokens.index(marker)
            return _tokens_signature(tail_tokens[idx + 1 :])
    return ""


def _postprocess_replay_safe(tail_tokens: list[str]) -> bool:
    if not tail_tokens:
        return True
    if tail_tokens[0] != "|":
        return False
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tail_tokens[1:]:
        if token == "|":
            if not current:
                return False
            segments.append(current)
            current = []
            continue
        if token in SHELL_CONTROL_TOKENS or _is_redirect(token):
            return False
        current.append(token)
    if not current:
        return False
    segments.append(current)
    return all(_is_pure_stream_filter(segment) for segment in segments)


def _is_pure_stream_filter(tokens: list[str]) -> bool:
    if not tokens:
        return False
    command = tokens[0].rsplit("/", 1)[-1]
    arguments = tokens[1:]
    if command in {"head", "tail"}:
        return _is_pure_head_or_tail(arguments)
    if command == "grep":
        return _is_pure_grep(arguments)
    return False


def _is_pure_head_or_tail(arguments: list[str]) -> bool:
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if re.fullmatch(r"-\d+", token):
            index += 1
            continue
        if token in {"-n", "--lines", "-c", "--bytes"} and index + 1 < len(arguments):
            if not re.fullmatch(r"[+-]?\d+", arguments[index + 1]):
                return False
            index += 2
            continue
        if re.fullmatch(r"--(?:lines|bytes)=[+-]?\d+", token):
            index += 1
            continue
        return False
    return True


def _is_pure_grep(arguments: list[str]) -> bool:
    pattern_count = 0
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in {"-E", "-F", "-G", "-i", "-n", "-o", "-v", "-w", "-x"}:
            index += 1
            continue
        if token in {"-m", "--max-count"} and index + 1 < len(arguments):
            if not arguments[index + 1].isdigit():
                return False
            index += 2
            continue
        if token in {"-e", "--regexp"} and index + 1 < len(arguments):
            pattern_count += 1
            index += 2
            continue
        if token.startswith("-"):
            return False
        pattern_count += 1
        index += 1
    return pattern_count == 1


def _has_dynamic_shell_input(command: str) -> bool:
    return "$" in command or "`" in command


def _normalize_shell_text(command: str) -> str:
    return " ".join(command.strip().split())


def _intent_from_curl(fp: dict[str, Any]) -> str:
    query_pairs = fp.get("query_pairs") or []
    query_text = " ".join(f"{k}={v}" for k, v in query_pairs)
    parts = [
        "curl",
        str(fp.get("method", "GET")),
        str(fp.get("host", "")),
        str(fp.get("path", "")),
        query_text,
    ]
    return _normalize_query_text(" ".join(p for p in parts if p))


def _normalize_query_text(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"https?://", " ", lowered)
    lowered = re.sub(r"[^a-z0-9_\-./:]+", " ", lowered)
    return " ".join(lowered.split())


def _first_string(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            nested = _first_string(value[0], keys)
            if nested:
                return nested
    return None


def _normalized_domains(tool_input: dict[str, Any]) -> list[str]:
    domains = tool_input.get("domains")
    if not isinstance(domains, list):
        return []
    return sorted(str(d).lower() for d in domains)


def _split_header(value: str) -> tuple[str | None, str]:
    if ":" not in value:
        return None, value
    name, header_value = value.split(":", 1)
    return name.strip(), header_value


def _find_curl_index(tokens: list[str]) -> int | None:
    for i, token in enumerate(tokens):
        if token == "curl" or token.endswith("/curl"):
            return i
    return None


def _is_shell_boundary(token: str) -> bool:
    return token in SHELL_CONTROL_TOKENS or _is_redirect(token)


def _is_redirect(token: str) -> bool:
    if token in {"2>&1", "1>&2"}:
        return False
    return token.startswith(REDIRECT_PREFIXES)


def _shell_side_effects(tail_tokens: list[str]) -> list[dict[str, str]]:
    effects: list[dict[str, str]] = []
    for i, token in enumerate(tail_tokens):
        if not _is_redirect(token):
            continue
        target = ""
        if token in {">", ">>", "1>", "1>>", "2>", "2>>", "&>"} and i + 1 < len(
            tail_tokens
        ):
            target = tail_tokens[i + 1]
        elif ">" in token:
            target = token.split(">", 1)[1]
        effects.append({"kind": "shell_redirect", "target": target})
    return effects


def _looks_like_url(token: str) -> bool:
    return token.startswith("http://") or token.startswith("https://")


def _option_attached_or_next(
    token: str,
    tokens: list[str],
    index: int,
    option_names: set[str],
) -> tuple[str, int] | None:
    long_names = {name for name in option_names if name.startswith("--")}
    short_names = {
        name
        for name in option_names
        if name.startswith("-") and not name.startswith("--")
    }
    if token in option_names and index + 1 < len(tokens):
        return tokens[index + 1], 2
    for name in long_names:
        prefix = f"{name}="
        if token.startswith(prefix):
            return token[len(prefix) :], 1
    for name in short_names:
        if token.startswith(name) and token != name and len(name) == 2:
            return token[len(name) :], 1
    return None


def _option_name(token: str) -> str:
    if token.startswith("--") and "=" in token:
        return token.split("=", 1)[0]
    if token.startswith("-") and not token.startswith("--") and len(token) > 2:
        return token[:2]
    return token


def _add_header(
    header_text: str,
    headers: list[tuple[str, str]],
    secret_headers: list[tuple[str, str]],
) -> None:
    name, value = _split_header(header_text)
    if not name:
        return
    normalized_name = name.lower()
    normalized_value = _normalize_header_value(value)
    if SECRET_HEADER_RE.match(normalized_name):
        secret_headers.append((normalized_name, "<redacted>"))
    else:
        headers.append((normalized_name, normalized_value))


def _normalize_header_value(value: str) -> str:
    return " ".join(value.strip().split())


def _parse_query_fragment(value: str) -> list[tuple[str, str]]:
    parsed = parse_qsl(value, keep_blank_values=True)
    if parsed:
        return parsed
    if "=" in value:
        key, item_value = value.split("=", 1)
        return [(key, item_value)]
    return [(value, "")]


def _append_query_parts(url: str, extra_pairs: list[tuple[str, str]]) -> str:
    if not extra_pairs:
        return url
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True) + extra_pairs
    query = urlencode(pairs, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _query_map(query_pairs: list[tuple[str, str]]) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {}
    for key, value in query_pairs:
        grouped.setdefault(key, []).append(value)
    return {
        key: values[0] if len(values) == 1 else values
        for key, values in sorted(grouped.items())
    }


def _short_flag_bundle_has(token: str, flag: str) -> bool:
    if not token.startswith("-") or token.startswith("--") or len(token) <= 2:
        return False
    if token.startswith(("-X", "-H", "-d", "-A", "-u", "-o", "-m", "-F")):
        return False
    return flag in token[1:]


def _is_noise_short_bundle(token: str) -> bool:
    if not token.startswith("-") or token.startswith("--") or len(token) <= 2:
        return False
    allowed = set("sS")
    return all(ch in allowed for ch in token[1:])


def _tokens_signature(tokens: list[str]) -> str:
    if not tokens:
        return ""
    return _normalize_shell_text(shlex.join(tokens))
