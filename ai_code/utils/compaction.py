# -*- coding: utf-8 -*-
"""Prefix-stable, idempotent context compaction for chat-format histories.

Ported from ai-lucky-star's three-stage `_compact_inplace`:

1. char threshold  -> stub OLD big tool results and the big arguments of old
   write_file / edit_file / multi_edit calls (the file is on disk anyway);
2. CJK-aware token estimate over the limit -> deep-stub old tool results and
   old assistant prose;
3. still over the limit -> drop the OLDEST assistant+tool blocks as a unit
   (never orphan a tool message, never drop a user message) down to 3/4 of
   the limit (hysteresis) and keep one "N earlier messages omitted" notice.

Stages 1-2 only ever shorten items that are already old, so the byte prefix
sent to the provider stays identical round after round and DeepSeek / Kimi /
Qwen context caching keeps hitting. The full text of every tool result lives
in the ai.code.event journal, retrievable with get_full_observation.
"""
import json
import re

KEEP_RECENT = 8                    # newest messages are never touched
SHRINK_CHARS = 300_000             # stage 1 trigger (chars, all messages)
TOOL_STUB_CHARS = 1_600
ARGS_STUB_CHARS = 600
MAX_EST_TOKENS = 90_000            # stage 2/3 trigger (CJK-aware estimate)
DEEP_TOOL_CHARS = 300
DEEP_TEXT_CHARS = 400
DROP_KEEP_TAIL = 12                # stage 3 never drops into the newest N

_STUB_MARK = "\n... [older tool output condensed; use get_full_observation(event_id) or re-run the tool]"
_ARGS_MARK = "... [earlier write content condensed; the file is on disk — read_file it]"
_DEEP_MARK = "... [deep-condensed]"
_DROP_PREFIX = "[Earlier conversation omitted:"
_ARGS_FIELDS = {"write_file": ("content",), "edit_file": ("old_string", "new_string"),
                "multi_edit": ("edits",)}
_CJK_RE = re.compile(r"[　-鿿豈-﫿＀-￯]")


def est_tokens(text):
    """CJK ~1 token/char, everything else ~4 chars/token."""
    text = text or ""
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk) // 4


def _dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _msg_chars(m):
    n = len(str(m.get("content") or ""))
    if m.get("tool_calls"):
        n += len(_dumps(m["tool_calls"]))
    return n


def _msg_tokens(m):
    n = est_tokens(str(m.get("content") or ""))
    if m.get("tool_calls"):
        n += est_tokens(_dumps(m["tool_calls"]))
    return n


def messages_tokens(messages):
    return sum(_msg_tokens(m) for m in messages if isinstance(m, dict))


def _is_drop_notice(m):
    return m.get("role") == "user" and str(m.get("content") or "").startswith(_DROP_PREFIX)


def _stub_tool_call(tc):
    """Shallow-copy stub of a chat-format tool call whose function arguments
    carry a big write payload. Idempotent (already-stubbed values end with
    the marker)."""
    fn = tc.get("function") if isinstance(tc, dict) else None
    if not isinstance(fn, dict):
        return tc
    fields = _ARGS_FIELDS.get(fn.get("name"))
    raw = fn.get("arguments")
    if not fields or not isinstance(raw, str) or len(raw) <= ARGS_STUB_CHARS * 2:
        return tc
    try:
        args = json.loads(raw)
    except ValueError:
        return tc
    if not isinstance(args, dict):
        return tc
    changed = False
    for f in fields:
        v = args.get(f)
        if isinstance(v, str) and len(v) > ARGS_STUB_CHARS and not v.endswith(_ARGS_MARK):
            args[f] = v[:ARGS_STUB_CHARS] + _ARGS_MARK
            changed = True
        elif isinstance(v, (list, dict)):
            dumped = _dumps(v)
            if len(dumped) > ARGS_STUB_CHARS:
                args[f] = dumped[:ARGS_STUB_CHARS] + _ARGS_MARK
                changed = True
    if not changed:
        return tc
    return {**tc, "function": {**fn, "arguments": _dumps(args)}}


def compact(messages):
    """Compact a chat-format message list IN PLACE. Returns the number of
    messages dropped by stage 3 (0 when the prefix stayed stable)."""
    if not messages:
        return 0
    cutoff = max(0, len(messages) - KEEP_RECENT)

    total = sum(_msg_chars(m) for m in messages if isinstance(m, dict))
    if total > SHRINK_CHARS:
        for i, m in enumerate(messages):
            if i >= cutoff or not isinstance(m, dict):
                continue
            content = str(m.get("content") or "")
            if m.get("role") == "tool" and len(content) > TOOL_STUB_CHARS \
                    and not content.endswith(_STUB_MARK):
                m["content"] = content[:TOOL_STUB_CHARS] + _STUB_MARK
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                m["tool_calls"] = [_stub_tool_call(tc) for tc in m["tool_calls"]]

    est = messages_tokens(messages)
    if est <= MAX_EST_TOKENS:
        return 0

    for i, m in enumerate(messages):
        if i >= cutoff or not isinstance(m, dict) or _is_drop_notice(m):
            continue
        content = str(m.get("content") or "")
        if content.endswith(_DEEP_MARK):
            continue
        if m.get("role") == "tool" and len(content) > DEEP_TOOL_CHARS + 40:
            m["content"] = content[:DEEP_TOOL_CHARS] + _DEEP_MARK
        elif m.get("role") == "assistant" and len(content) > DEEP_TEXT_CHARS + 40:
            m["content"] = content[:DEEP_TEXT_CHARS] + _DEEP_MARK

    est = messages_tokens(messages)
    if est <= MAX_EST_TOKENS:
        return 0
    return _drop_oldest(messages, est, MAX_EST_TOKENS * 3 // 4)


def _drop_oldest(messages, est, target):
    dropped = 0
    if not messages or not _is_drop_notice(messages[0]):
        messages.insert(0, {"role": "user", "content": ""})
    i = 1
    while est > target and len(messages) - i > DROP_KEEP_TAIL:
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") == "user":
            i += 1          # user messages are the task anchors — never dropped
            continue
        end = i + 1
        if m.get("role") == "assistant" and m.get("tool_calls"):
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
        est -= sum(_msg_tokens(x) for x in messages[i:end])
        dropped += end - i
        del messages[i:end]
    prev = str(messages[0].get("content") or "")
    m_prev = re.search(r"(\d+)", prev)
    total_dropped = dropped + (int(m_prev.group(1)) if m_prev else 0)
    messages[0]["content"] = (
        f"{_DROP_PREFIX} {total_dropped} messages. Their results are on disk / in the "
        "task journal. Re-read files with read_file when you need facts; do not rely on "
        "memory of the omitted exchange.]")
    return dropped


def shrink_for_overflow(messages):
    """After a real provider context-overflow error: deep-stub everything old
    and drop to 3/4 of min(current, limit). Returns the new estimate."""
    cutoff = max(0, len(messages) - KEEP_RECENT)
    for i, m in enumerate(messages):
        if i >= cutoff or not isinstance(m, dict) or _is_drop_notice(m):
            continue
        content = str(m.get("content") or "")
        if m.get("role") == "assistant" and m.get("tool_calls"):
            m["tool_calls"] = [_stub_tool_call(tc) for tc in m["tool_calls"]]
        if m.get("role") == "tool" and not content.endswith(_DEEP_MARK) \
                and len(content) > DEEP_TOOL_CHARS + 40:
            m["content"] = content[:DEEP_TOOL_CHARS] + _DEEP_MARK
        elif m.get("role") == "assistant" and not content.endswith(_DEEP_MARK) \
                and len(content) > DEEP_TEXT_CHARS + 40:
            m["content"] = content[:DEEP_TEXT_CHARS] + _DEEP_MARK
    est = messages_tokens(messages)
    _drop_oldest(messages, est, min(est, MAX_EST_TOKENS) * 3 // 4)
    return messages_tokens(messages)
