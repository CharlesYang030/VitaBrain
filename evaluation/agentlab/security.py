from __future__ import annotations

import re
from typing import Any


# `token(?!s)`：拦 auth token/frame_token 等凭证，但放行 token 计数字段
# （prompt_tokens/completion_tokens/…_tokens_details 是审计需要的用量数据）。
_SENSITIVE_KEYS = re.compile(r"(authorization|api[_-]?key|token(?!s)|secret|password|cookie)", re.I)
_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


# Native AI2-THOR IDs carry coordinates/indices after the pipe; the
# digit requirement keeps prompt enums like "evidence|object|spatial" from
# false-positives.
_RAW_OBJECT_ID = re.compile(r"[A-Za-z][\w.]*\|(?=[\w+.|-]*\d)[\w+.|-]{3,}")
# Generated scene objects and screen channels also use category_hash IDs.
# Observation IDs and public per-episode handles are intentionally allowed.
SPAWNED_OBJECT_ID_PATTERN = re.compile(
    r"\b(?:fx_[A-Za-z0-9_]+|vis_[A-Za-z0-9_]+|"
    r"(?!h_|obs_|mem_|run_)[A-Za-z][A-Za-z0-9]*_[0-9a-fA-F]{4,})\b")
_DATA_URL = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+")
_PRIVATE_MARKERS = (
    '"raw_id"', '"raw_key"', '"handle_map"', '"frame_token"',
    '"content_if_readable"', '"information_grants"', '"_private"',
)


def scan_events_for_leaks(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Server-side leak audit over everything the model could have seen.

    Only agent_visible / agent_output events are scanned: those are exactly
    the surfaces that reach the model or come back from it.
    """
    import json as _json

    problems: list[dict[str, Any]] = []
    scanned = 0
    for event in events:
        if event.get("visibility") not in ("agent_visible", "agent_output"):
            continue
        scanned += 1
        payload = event.get("payload", {})
        if event.get('stage') == 'MEMORY_COMPACTION_RESPONDED':
            # Provider envelopes are retained for evaluator diagnostics only.
            # E.g. a provider's resp_<hex> identifier is not a scene object ID.
            # The actual model output remains in `content` and is fully scanned.
            payload = {k: v for k, v in payload.items() if k != 'raw'}
        text = _json.dumps(payload, ensure_ascii=False)
        text = _DATA_URL.sub("[image]", text)
        for match in list(_RAW_OBJECT_ID.finditer(text)) + list(
                SPAWNED_OBJECT_ID_PATTERN.finditer(text)):
            problems.append({
                "seq": event.get("seq"), "stage": event.get("stage"),
                "kind": "raw_object_id", "excerpt": match.group(0)[:60],
            })
        if _KEY_PATTERN.search(text):
            problems.append({
                "seq": event.get("seq"), "stage": event.get("stage"),
                "kind": "api_key_pattern", "excerpt": "sk-…",
            })
        for marker in _PRIVATE_MARKERS:
            if marker in text:
                problems.append({
                    "seq": event.get("seq"), "stage": event.get("stage"),
                    "kind": "private_field_name", "excerpt": marker,
                })
    return {
        "scanned_model_facing_events": scanned,
        "total_events": len(events),
        "problems": problems,
        "clean": not problems,
    }


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return _KEY_PATTERN.sub("[REDACTED_API_KEY]", value)
    return value
