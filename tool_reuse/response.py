from __future__ import annotations

import json
from typing import Any


def extract_response_text(tool_response: dict[str, Any]) -> str:
    chunks: list[str] = []
    content = tool_response.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
            elif isinstance(item, str):
                chunks.append(item)
    for key in ("text", "output", "stdout", "stderr"):
        value = tool_response.get(key)
        if isinstance(value, str):
            chunks.append(value)
    if not chunks:
        return json.dumps(tool_response, ensure_ascii=False, sort_keys=True)
    return "\n".join(chunks)
