#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_reuse.exact.matcher import match_exact  # noqa: E402


def allow() -> int:
    print(json.dumps({"decision": "allow"}))
    return 0


def deny_with_cache(result: dict) -> int:
    response = result.get("tool_response")
    record = result.get("selected", {})
    context = {
        "tool_reuse": {
            "hit_type": "exact-v5",
            "exact_key": result.get("exact_key"),
            "record_key": record.get("record_key"),
            "ended_at": record.get("ended_at"),
            "freshness_class": record.get("freshness_class"),
            "ttl_seconds": record.get("ttl_seconds"),
            "note": (
                "The tool call was not executed because exact-v5 found a fresh, "
                "successful, replayable response."
            ),
        },
        "cached_tool_response": response,
    }
    print(
        json.dumps(
            {
                "decision": "deny",
                "reason": "tool reuse cache exact hit",
                "additionalContext": json.dumps(context, ensure_ascii=False),
                "toolResponse": response,
                "toolResponseMetadata": {
                    "cacheHitType": "exact",
                    "cacheRecordKey": record.get("record_key"),
                    "cacheObservedAt": record.get("ended_at"),
                },
            },
            ensure_ascii=False,
        )
    )
    return 2


def main() -> int:
    db_path = os.environ.get("TOOL_REUSE_DB")
    cache_scope = os.environ.get("TOOL_REUSE_SCOPE")
    if not db_path or not cache_scope:
        return allow()
    try:
        event = json.load(sys.stdin)
        tool_name = event.get("tool_name")
        tool_input = event.get("tool_input")
        if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
            return allow()
        result = match_exact(
            db_path,
            tool_name,
            tool_input,
            include_response=True,
            cache_scope=cache_scope,
        )
        if result.get("reusable") is True:
            return deny_with_cache(result)
        return allow()
    except Exception as exc:
        print(f"tool_reuse hook failed open: {exc}", file=sys.stderr)
        return allow()


if __name__ == "__main__":
    raise SystemExit(main())
