# -*- coding: utf-8 -*-
"""Coding loop engine (single ReAct loop over a PERSISTENT chat history).

CodingLLMService subclasses the platform's LLMApiService for the per-provider
request/response plumbing (_request_llm, _build_tool_call_response) and adds:

- run_turn(): one user turn = rounds of (LLM call -> tool calls -> results)
  until the model answers without tool calls, a tool pauses the turn
  (ask_user_question), the round cap trips, or the user stops it;
- the history is a chat-format `inputs` list that the CALLER persists after
  every round (durable progress, resumable across turns);
- prefix-stable compaction before each call + overflow-shrink-retry;
- transient-error retries, per-call read timeout, repeat-call reminders and
  chat-template control-token neutralization of tool output.
"""
import json
import logging
import re
import time

from odoo.exceptions import UserError
from odoo.addons.ai_chatbot_enhance.utils.llm_api_service import LLMApiService

from . import compaction

_logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 180
MAX_TRANSIENT_RETRIES = 3
_TRANSIENT_MARKERS = ('timed out', 'timeout', 'connection', 'reset', 'temporarily',
                      '503', '502', '504', '429', 'rate limit', 'overloaded')
_OVERFLOW_MARKERS = ('context length', 'context_length', 'maximum context', 'too many tokens',
                     'context window', 'prompt is too long', 'input is too long',
                     'exceeds the model')
# chat-template control tokens inside file/command output would be tokenized
# as REAL control tokens by some inference stacks and forge a conversation turn
_SPECIAL_TOKEN_RE = re.compile(r'<\|(?:im_start|im_end|endoftext|eot_id|start_header_id|'
                               r'end_header_id|begin_of_text|end_of_text)\|>')
REPEAT_LIMIT = 3
# tools whose identical re-run is meaningful (state may have changed): higher cap
REPEAT_EXEMPT_LIMIT = 15
REPEAT_EXEMPT = {'sandbox_test', 'shell', 'git_status', 'git_diff', 'git_log', 'todo_write',
                 'ask_user_question', 'list_attachments'}
# a write changes the world: every repeat counter restarts after one
REPEAT_RESET_ON = {'write_file', 'edit_file', 'multi_edit', 'scaffold_module', 'shell'}


class StopTurn(Exception):
    """Raised by a tool that pauses/ends the turn (ask_user_question).
    `reason` names the tool, `payload` its validated arguments, `call_id` the
    tool call whose result must be supplied when the turn resumes."""

    def __init__(self, reason, payload=None):
        super().__init__(reason)
        self.reason = reason
        self.payload = payload or {}
        self.call_id = None


class TaskAborted(Exception):
    """Raised by check_continue when the user stopped the run or a budget tripped."""


class ContextOverflow(UserError):
    pass


class StreamUnsupported(Exception):
    """The provider rejected the streaming request: retry without streaming."""


def _has_content(message):
    content = message.get('content')
    if isinstance(content, str):
        return bool(content.strip())
    return bool(content)


def sanitize_history(inputs):
    """Repair a chat-format history in place so every provider accepts it:
    - assistant messages with neither content nor tool_calls get a placeholder
      (histories saved before this guard existed carry such messages);
    - `reasoning_content` never goes back to the provider;
    - content None with tool_calls becomes "" (some endpoints reject null).
    Returns the number of repaired messages."""
    repaired = 0
    for m in inputs:
        if not isinstance(m, dict) or m.get('role') != 'assistant':
            continue
        if 'reasoning_content' in m:
            m.pop('reasoning_content', None)
            repaired += 1
        if not _has_content(m) and not m.get('tool_calls'):
            m['content'] = "(no reply)"
            repaired += 1
        elif m.get('content') is None and m.get('tool_calls'):
            m['content'] = ""
    return repaired


def neutralize_special_tokens(text):
    return _SPECIAL_TOKEN_RE.sub(lambda m: m.group(0).replace('<|', '<​|'), text or '')


class CodingLLMService(LLMApiService):

    def __init__(self, env, provider='openai', on_delta=None):
        super().__init__(env, provider)
        self._repeat_counts = {}
        # on_delta(text_chunk) receives content deltas while a chat-completions
        # provider streams; None = plain non-streaming requests
        self.on_delta = on_delta

    def _request(self, *args, **kwargs):
        kwargs.setdefault('timeout', REQUEST_TIMEOUT)
        body = kwargs.get('body') if 'body' in kwargs else (args[3] if len(args) > 3 else None)
        endpoint = kwargs.get('endpoint') if 'endpoint' in kwargs else (args[1] if len(args) > 1 else '')
        if self.on_delta and isinstance(body, dict) and 'messages' in body \
                and str(endpoint or '').rstrip('/').endswith('chat/completions'):
            try:
                return self._request_streaming(body=body, **{k: v for k, v in kwargs.items() if k != 'body'},
                                               **({} if 'endpoint' in kwargs else {'endpoint': endpoint}))
            except StreamUnsupported as e:
                _logger.info("ai_code: streaming unsupported for %s (%s) — falling back", self.provider, e)
        return super()._request(*args, **kwargs)

    # ── SSE streaming for OpenAI-style chat completions ────────────────
    def _request_streaming(self, method='post', endpoint='/chat/completions', headers=None, body=None,
                           base_url=None, timeout=REQUEST_TIMEOUT, **_ignored):
        """Stream the completion, forward content deltas to on_delta, and
        return a response dict IDENTICAL in shape to the non-streaming one so
        the platform's parsing code works unchanged."""
        import requests
        route = f"{base_url or self.base_url}/{endpoint.strip('/')}"
        payload = dict(body, stream=True)
        payload['stream_options'] = {'include_usage': True}
        try:
            resp = requests.request(method, route, headers=headers, json=payload,
                                    timeout=timeout, stream=True)
        except requests.exceptions.RequestException as e:
            raise UserError(repr(e))
        if resp.status_code >= 400:
            try:
                err = resp.json()
                detail = (err.get('error') or {}).get('message') if isinstance(err, dict) else None
            except ValueError:
                detail = None
            detail = detail or resp.text[:500]
            if resp.status_code in (400, 404, 422) and 'stream' in detail.lower():
                raise StreamUnsupported(detail)
            _logger.warning("LLM API request failed: %s", detail)
            raise UserError(detail)
        content_parts, reasoning_parts = [], []
        tool_calls = {}           # index -> {"id", "type", "function": {"name", "arguments"}}
        finish_reason, usage, role = None, None, 'assistant'
        got_any = False
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                got_any = True
                if chunk.get('usage'):
                    usage = chunk['usage']
                for choice in chunk.get('choices') or []:
                    delta = choice.get('delta') or {}
                    if delta.get('role'):
                        role = delta['role']
                    text = delta.get('content')
                    if text:
                        content_parts.append(text)
                        try:
                            self.on_delta(text)
                        except Exception:  # noqa: BLE001 — UI plumbing must not break the call
                            _logger.debug("ai_code: on_delta failed", exc_info=True)
                    if delta.get('reasoning_content'):
                        reasoning_parts.append(delta['reasoning_content'])
                    for tc in delta.get('tool_calls') or []:
                        idx = tc.get('index', 0)
                        slot = tool_calls.setdefault(idx, {'id': '', 'type': 'function',
                                                           'function': {'name': '', 'arguments': ''}})
                        if tc.get('id'):
                            slot['id'] = tc['id']
                        fn = tc.get('function') or {}
                        if fn.get('name'):
                            slot['function']['name'] += fn['name']
                        if fn.get('arguments'):
                            slot['function']['arguments'] += fn['arguments']
                    if choice.get('finish_reason'):
                        finish_reason = choice['finish_reason']
        finally:
            resp.close()
        if not got_any:
            raise StreamUnsupported("empty stream")
        message = {'role': role, 'content': ''.join(content_parts) or None}
        if tool_calls:
            message['tool_calls'] = [tool_calls[i] for i in sorted(tool_calls)]
        # reasoning_content is deliberately NOT kept: the assistant message goes
        # back into the history and DeepSeek rejects inputs carrying it (400)
        return {'choices': [{'index': 0, 'message': message, 'finish_reason': finish_reason}],
                'usage': usage or {}}

    def _request_llm_retrying(self, *args, **kwargs):
        last_error = None
        for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
            try:
                return self._request_llm(*args, **kwargs)
            except UserError as e:
                message = str(e.args[0] if e.args else e).lower()
                if any(marker in message for marker in _OVERFLOW_MARKERS):
                    raise ContextOverflow(str(e.args[0] if e.args else e))
                if not any(marker in message for marker in _TRANSIENT_MARKERS):
                    raise
                last_error = e
                if attempt < MAX_TRANSIENT_RETRIES:
                    wait = 2 ** attempt
                    _logger.warning("ai_code: transient LLM error (attempt %s/%s), retrying in "
                                    "%ss: %s", attempt, MAX_TRANSIENT_RETRIES, wait, message[:160])
                    time.sleep(wait)
        raise last_error

    # ── the loop ───────────────────────────────────────────────────────
    def run_turn(self, llm_model, system_prompts, first_user, inputs, tools,
                 max_rounds=60, temperature=0.1, on_text=None, on_tool_start=None,
                 on_tool_result=None, on_round=None, check_continue=None, inbox=None):
        """Drive the loop on `inputs` (chat-format history, mutated in place).

        :param tools: {name: (description, allow_end_message, callable, schema)};
                      callable(arguments=<dict>) -> (result_str, error|None) or
                      raises StopTurn.
        :param inbox: optional fn() -> list[str] of user messages that arrived
                      while the turn was running (injected before the next call).
        :return: (status, stop) with status in 'done' | 'paused' | 'max_rounds'
        """
        def call(cb, *a):
            if cb:
                try:
                    return cb(*a)
                except (StopTurn, TaskAborted):
                    raise
                except Exception:  # noqa: BLE001 — observers must never kill the loop
                    _logger.exception("ai_code: loop callback failed")

        empty_replies = 0
        for round_no in range(1, max_rounds + 1):
            if check_continue:
                check_continue()
            compaction.compact(inputs)
            sanitize_history(inputs)
            for text in (inbox() if inbox else []) or []:
                inputs.append({"role": "user", "content": text})

            responses, next_actions, new_inputs = self._call_with_overflow_guard(
                llm_model, system_prompts, first_user, inputs, tools, temperature)
            # an EMPTY assistant message (no text, no tool call — e.g. the model
            # spent its whole output on reasoning) must never enter the history:
            # DeepSeek rejects it on the next call ("content or tool_calls must
            # be set"). Drop it and ask again; give up after a few tries.
            tail = new_inputs[-1] if new_inputs and isinstance(new_inputs[-1], dict) else {}
            if tail.get('role') == 'assistant' and not next_actions \
                    and not _has_content(tail) and not tail.get('tool_calls'):
                empty_replies += 1
                _logger.warning("ai_code: empty assistant reply (%s/3)", empty_replies)
                if empty_replies < 3:
                    inputs.append({"role": "user", "content":
                                   "[System] Your reply was empty (no text, no tool call). "
                                   "Answer in text or call a tool. Keep reasoning short."})
                    continue
                raise UserError("The model returned an empty reply three times in a row. "
                                "Try again, or pick another model.")
            inputs[:] = new_inputs
            for text in responses:
                if text:
                    call(on_text, text)

            assistant = inputs[-1] if inputs and isinstance(inputs[-1], dict) else {}
            declared = assistant.get('tool_calls') or []
            answered_ids = {call_id for _n, call_id, _a in next_actions}
            # calls whose arguments were not valid JSON never reach next_actions;
            # the provider still expects a tool message for each declared call
            for tc in declared:
                cid = tc.get('id')
                if cid and cid not in answered_ids:
                    name = (tc.get('function') or {}).get('name', '?')
                    msg = (f"Tool {name} was NOT executed: its arguments were not valid JSON "
                           "(usually the reply was cut by max_tokens). Send less content per "
                           "call — split the write into several write_file / edit_file calls.")
                    call(on_tool_result, name, msg, True, 0)
                    inputs.append(self._build_tool_call_response(cid, msg))

            if not next_actions:
                call(on_round, round_no, False)
                return 'done', None

            paused = None
            for idx, (tool_name, call_id, arguments) in enumerate(next_actions):
                if paused is not None:
                    skip = "Skipped: the turn paused on a user question before this call."
                    inputs.append(self._build_tool_call_response(call_id, skip))
                    continue
                if check_continue:
                    check_continue()
                arguments = dict(arguments or {})
                call(on_tool_start, tool_name, arguments)
                t0 = time.perf_counter()
                result, error = self._exec_tool(tools, tool_name, arguments)
                if isinstance(result, StopTurn):
                    result.call_id = call_id
                    paused = result
                    call(on_tool_result, tool_name, f"[turn paused: {result.reason}]", None,
                         int((time.perf_counter() - t0) * 1000))
                    continue
                result = neutralize_special_tokens(str(result))
                call(on_tool_result, tool_name, result, error,
                     int((time.perf_counter() - t0) * 1000))
                inputs.append(self._build_tool_call_response(call_id, result))
            call(on_round, round_no, True)
            if paused is not None:
                return 'paused', paused
        _logger.warning("ai_code: turn exhausted %s rounds", max_rounds)
        return 'max_rounds', None

    def _exec_tool(self, tools, tool_name, arguments):
        if tool_name not in tools:
            return (f"Error: unknown tool '{tool_name}'. Available: {', '.join(tools)}.",
                    True)
        if tool_name in REPEAT_RESET_ON:
            self._repeat_counts.clear()
        key = (tool_name, json.dumps(arguments, sort_keys=True, ensure_ascii=False,
                                     default=str)[:2000])
        count = self._repeat_counts.get(key, 0) + 1
        self._repeat_counts[key] = count
        limit = REPEAT_EXEMPT_LIMIT if tool_name in REPEAT_EXEMPT else REPEAT_LIMIT
        if count > limit:
            return (f"You have already made this exact {tool_name} call {count - 1} times "
                    "with the same arguments and seen its result. Repeating it will not "
                    "change anything: change your approach, use a different tool or "
                    "arguments, or explain to the user what is blocking you."), True
        try:
            return tools[tool_name][2](arguments=arguments)
        except StopTurn as stop:
            return stop, None
        except TaskAborted:
            raise
        except Exception as e:  # noqa: BLE001 — a tool bug is data for the model, not a crash
            _logger.exception("ai_code: tool %s crashed", tool_name)
            return f"Tool error: {type(e).__name__}: {e}", True

    def _call_with_overflow_guard(self, llm_model, system_prompts, first_user, inputs,
                                  tools, temperature):
        try:
            return self._request_llm_retrying(
                llm_model, system_prompts, [first_user], tools=tools, inputs=inputs,
                temperature=temperature)
        except ContextOverflow:
            before = compaction.messages_tokens(inputs)
            after = compaction.shrink_for_overflow(inputs)
            if after >= before:
                raise
            _logger.warning("ai_code: context overflow — shrunk history %s -> %s est. tokens, "
                            "retrying", before, after)
            return self._request_llm_retrying(
                llm_model, system_prompts, [first_user], tools=tools, inputs=inputs,
                temperature=temperature)

    # ── one-shot helper (cheap model side channel) ─────────────────────
    def simple_completion(self, llm_model, system_prompt, user_prompt, temperature=0.3):
        responses, _calls, _inputs = self._request_llm_retrying(
            llm_model, [system_prompt], [user_prompt], temperature=temperature)
        return "\n".join(r for r in responses if r).strip()
