import copy
import json
import os
import requests
import typing
from datetime import datetime
from logging import getLogger
import time
from typing import Callable, Any

from ..utils.ai_logging import ai_response_logging, get_ai_logging_session, api_call_logging
from odoo import _
from odoo.api import Environment
from odoo.exceptions import UserError
from ..utils.llm_providers import check_model_depreciation, canonical_model

_logger = getLogger(__name__)

# OpenAI-compatible providers handled by the generic handler (_request_llm_openai_compat).
# Adding a new provider here only requires: base_url below, a Provider entry in
# llm_providers.PROVIDERS, and an API key field in res.config.settings.
# The API key is read from ir.config_parameter "ai_chatbot_enhance.<provider>_key"
# or the environment variable "ODOO_AI_<PROVIDER>_TOKEN".
OPENAI_COMPAT_PROVIDERS = {
    "kimi": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
}

# Kimi has two platforms whose keys are NOT interchangeable: the legacy Moonshot
# open platform (platform.moonshot.cn, key "sk-...") is served by api.moonshot.cn,
# the 2026 Kimi platform (platform.kimi.com, key "sk-kimi-...") by
# api.kimi.com/coding/v1 - a new-platform key on the old endpoint always gets
# 401 Invalid Authentication. The endpoint is picked from the key prefix so
# both kinds of key work without any extra setting (same model ids on both).
KIMI_PLATFORM_BASE = "https://api.kimi.com/coding/v1"
KIMI_PLATFORM_KEY_PREFIX = "sk-kimi-"

# Output cap sent to the OpenAI-compatible endpoints (DeepSeek, Kimi, GLM).
# Thinking models (DeepSeek V4 thinking mode, Kimi K2.6 thinking) spend this
# budget on reasoning_content too: with the vendor default (4-8k) a long task
# can burn it all before the first visible token and come back empty. It is
# an upper bound, billing is per generated token.
DEFAULT_MAX_TOKENS = 16384


def _cached_tokens(usage: dict) -> int:
    """Prompt-cache hits, whichever field the vendor uses: DeepSeek reports
    prompt_cache_hit_tokens, OpenAI-style endpoints prompt_tokens_details.
    cached_tokens, Moonshot cached_tokens."""
    return (usage.get("prompt_cache_hit_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or usage.get("cached_tokens")
            or 0)


def _reasoning_only_fallback(reasoning: str) -> str:
    """A thinking model that exhausted max_tokens inside reasoning_content
    returns an empty content. An empty reply would end the run looking like
    "it just stopped", so hand back the tail of the reasoning instead."""
    return ("(The model spent this turn's output budget on thinking and gave no "
            "final answer; this is the end of its reasoning)\n" + reasoning[-1200:])


class Embedding(typing.TypedDict):
    index: int
    embedding: list[float]
    object: str


class EmbeddingResponse(typing.TypedDict):
    object: str
    data: list[Embedding]
    model: str
    usage: dict


class LLMApiService:
    def __init__(self, env: Environment, provider: str = 'openai') -> None:
        self.provider = provider
        self.env = env
        base_url = None
        if self.provider == 'openai':
            base_url = "https://api.openai.com/v1"
        elif self.provider == 'google':
            base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
        elif self.provider == 'deepseek':
            base_url = "https://api.deepseek.com/v1"
        elif self.provider == 'qwen':
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        elif self.provider == 'kimi':
            base_url = self._kimi_base_url()
        elif self.provider in OPENAI_COMPAT_PROVIDERS:
            base_url = OPENAI_COMPAT_PROVIDERS[self.provider]
        else:
            raise NotImplementedError(f"Unsupported provider: {self.provider}")

        self.base_url = base_url

    def _kimi_base_url(self) -> str:
        """Kimi endpoint by key prefix (see KIMI_PLATFORM_BASE). A missing key
        is reported later by _get_api_token, not here."""
        key = self._get_api_token(raise_if_missing=False) or ""
        if key.startswith(KIMI_PLATFORM_KEY_PREFIX):
            return KIMI_PLATFORM_BASE
        return OPENAI_COMPAT_PROVIDERS["kimi"]

    def _request_llm(self, *args, **kwargs):
        # retired model names (deepseek-chat, ...) are swapped for their live
        # equivalent here, right before the request, so stored agents keep working
        if "llm_model" in kwargs:
            kwargs["llm_model"] = canonical_model(kwargs["llm_model"])
        elif args:
            args = (canonical_model(args[0]),) + tuple(args[1:])
        model = kwargs.get("llm_model") or args[0]
        check_model_depreciation(self.env, model)

        if self.provider == 'openai':
            return self._request_llm_openai(*args, **kwargs)

        if self.provider == 'google':
            return self._request_llm_google(*args, **kwargs)

        if self.provider == 'deepseek':
            return self._request_llm_deepseek(*args, **kwargs)

        if self.provider == 'qwen':
            return self._request_llm_qwen(*args, **kwargs)

        if self.provider in OPENAI_COMPAT_PROVIDERS:
            return self._request_llm_openai_compat(*args, **kwargs)

        raise NotImplementedError()

    def request_llm(
            self, llm_model: str, system_prompts: list[str], user_prompts: list[str],
            tools: dict[str, tuple[str, bool, Callable[[dict[str, Any]], Any], dict]] | None = None,
            files: list[dict] | None = None, schema: dict | None = None, temperature: float = 0.2,
            inputs: list[dict] | None = None, web_grounding: bool = False,
    ) -> list[str]:
        check_model_depreciation(self.env, llm_model)
        # Budget gate for non-chat paths (server actions, digests); the chat
        # path already blocks earlier with a polite message
        budget_blocked, budget_notice = self.env['ai.token.usage'].sudo()._check_budget(self.env.user)
        if budget_blocked:
            raise UserError(budget_notice)
        with ai_response_logging(llm_model):
            try:
                return self._request_llm_silent_simple(
                    llm_model=llm_model,
                    system_prompts=system_prompts,
                    user_prompts=user_prompts,
                    tools=tools,
                    files=files,
                    schema=schema,
                    temperature=temperature,
                    inputs=inputs,
                    web_grounding=web_grounding,
                )
            finally:
                self._persist_token_usage(llm_model)

    def _ai_stop_requested(self, turn_start_dt):
        """Whether the user clicked Stop after this turn started (chat
        background flow only). Read from the DB so it works across server
        processes; READ COMMITTED shows the stop write as soon as the stop
        RPC commits."""
        if not self.env.context.get('ai_background_job'):
            return False
        channel = self.env.context.get('discuss_channel')
        if channel is None:
            return False
        try:
            return channel._ai_stop_requested_since(turn_start_dt)
        except Exception:  # noqa: BLE001 - a broken check must not kill the turn
            _logger.debug("AI: stop-request check failed", exc_info=True)
            return False

    def _keep_typing_alive(self):
        """Refresh the agent's typing indicator between successive API calls.

        Only active in the background-thread chat flow (context flag
        ai_background_job): the frontend expires a remote typing status after
        a while, and long agent loops would otherwise look dead. The commit is
        required for the bus event to actually go out mid-turn; it is safe
        here — this cursor belongs to the background job alone."""
        if not self.env.context.get('ai_background_job'):
            return
        channel = self.env.context.get('discuss_channel')
        agent_id = self.env.context.get('ai_en_agent_id')
        if channel is None or not agent_id:
            return
        try:
            agent = self.env['ai.agent.bot'].sudo().browse(agent_id)
            member = channel.sudo().channel_member_ids.filtered(
                lambda m: m.partner_id == agent.partner_id)
            member._notify_typing(True)
            self.env.cr.commit()
        except Exception:  # noqa: BLE001 - cosmetic, never break the turn
            _logger.debug("AI: could not refresh typing indicator", exc_info=True)

    def _persist_token_usage(self, llm_model):
        """Persist the aggregated token usage of this turn (ai.token.usage).
        Never breaks the response flow."""
        session = get_ai_logging_session()
        if session is None:
            return
        try:
            input_tokens = int(session.get('real_input_tokens') or 0)
            output_tokens = int(session.get('output_tokens') or 0)
            cached_tokens = int(session.get('cached_tokens') or 0)
            is_estimated = False
            if not input_tokens and not output_tokens:
                # provider did not report usage: fall back to the estimate
                input_tokens = int(session.get('tokens_in') or 0)
                is_estimated = True
            if not (input_tokens or output_tokens or session.get('api_calls')):
                return  # nothing happened at all
            channel = self.env.context.get('discuss_channel')
            Usage = self.env['ai.token.usage'].sudo()
            with self.env.cr.savepoint():
                Usage.create({
                    'user_id': self.env.uid,
                    'agent_id': self.env.context.get('ai_en_agent_id') or False,
                    'channel_id': channel.id if channel else False,
                    'provider': self.provider,
                    'llm_model': llm_model,
                    'api_calls': session.get('api_calls', 0),
                    'tool_calls': session.get('tool_calls', 0),
                    'input_tokens': input_tokens,
                    'cached_tokens': cached_tokens,
                    'output_tokens': output_tokens,
                    'is_estimated': is_estimated,
                    'duration': round(time.perf_counter() - session.get('start_time', time.perf_counter()), 2),
                    'cost': Usage._compute_cost_amount(llm_model, input_tokens, cached_tokens, output_tokens),
                })
        except Exception:  # noqa: BLE001 - usage logging must never break the chat
            _logger.exception("AI: failed to persist token usage")

    def _request_llm_silent_simple(
            self, llm_model: str, system_prompts: list[str], user_prompts: list[str],
            tools: dict[str, tuple[str, bool, Callable[[dict[str, Any]], Any], dict]] | None = None,
            files: list[dict] | None = None, schema: dict | None = None, temperature: float = 0.2,
            inputs: list[dict] | None = None, web_grounding: bool = False,
    ):

        AI_MAX_SUCCESSIVE_CALLS = 500
        AI_MAX_TOOL_CALLS_PER_CALL = 500
        # Wall-clock budget for one turn: with the generation now running in a
        # background thread nothing external kills a runaway loop, so we stop
        # it ourselves and tell the user.
        try:
            max_turn_seconds = int(self.env['ir.config_parameter'].sudo().get_param(
                'ai_chatbot_enhance.max_turn_seconds', '600'))
        except (ValueError, TypeError):
            max_turn_seconds = 600
        turn_start = time.monotonic()
        # microsecond=0: the DB truncates the stop timestamp to seconds — a
        # stop clicked within the starting second must still win
        turn_start_dt = datetime.utcnow().replace(microsecond=0)

        if tools:
            tools = copy.deepcopy(tools)
            for __, allow_end_message, __, tool_parameter_schema in tools.values():
                if allow_end_message and "__end_message" not in tool_parameter_schema["properties"]:
                    tool_parameter_schema["properties"]["__end_message"] = {
                        "type": "string",
                        "description": "If you are not waiting a result and you are done, write here your last message (it must follow the instructions). If you will do an action after this one, leave it empty.",
                    }
                if "__end_message" in tool_parameter_schema["properties"] and "__end_message" not in tool_parameter_schema["required"]:
                    tool_parameter_schema["required"].append("__end_message")

        inputs = inputs or []

        if self.provider == 'google':
            # OpenAI / Odoo inputs -> Gemini
            inputs = [
                {"role": "user" if i["role"] == "user" else "model", "parts": [{"text": i["content"]}]}
                for i in inputs
            ]

        all_responses = []
        for api_call in range(AI_MAX_SUCCESSIVE_CALLS):
            if api_call and time.monotonic() - turn_start > max_turn_seconds:
                _logger.warning(
                    "AI: turn exceeded %ss after %s API calls, stopping the loop",
                    max_turn_seconds, api_call)
                all_responses.append(self.env._(
                    "This request was taking too long and was stopped for safety. "
                    "Here is what was completed so far — please continue with a "
                    "more specific request."))
                break
            if self._ai_stop_requested(turn_start_dt):
                _logger.info("AI: generation stopped by the user (before API call #%d)", api_call)
                all_responses.append(self.env._("⏹️ Generation stopped at your request."))
                break
            self._keep_typing_alive()
            responses, next_actions, inputs = self._request_llm(
                llm_model,
                system_prompts,
                user_prompts,
                files=files,
                inputs=inputs,
                schema=schema,
                tools=tools,
                temperature=temperature,
                web_grounding=web_grounding,
            )
            all_responses.extend(responses)

            _logger.info(
                "[AI DIAG] Loop #%d | available_tools=%s | tool_calls=%s | text_response=%s",
                api_call,
                list(tools.keys()) if tools else [],
                [(name, args) for name, _cid, args in next_actions],
                responses,
            )

            # Second checkpoint: stop clicked while the LLM call was in
            # flight — abort BEFORE executing any tool it requested (must
            # run before the no-actions break so a stopped final text
            # answer is caught too; the chat layer then discards it)
            if self._ai_stop_requested(turn_start_dt):
                _logger.info("AI: generation stopped by the user (after API call #%d)", api_call)
                all_responses.append(self.env._("⏹️ Generation stopped at your request."))
                break

            if not next_actions:
                break

            done = False
            session = get_ai_logging_session()

            if session:
                session["tool_calls"] += min(len(next_actions), AI_MAX_TOOL_CALLS_PER_CALL)

            for i, (tool_name, call_id, arguments) in enumerate(next_actions):
                if i >= AI_MAX_TOOL_CALLS_PER_CALL:
                    _logger.warning("AI: Tool call limit reached, stopping further tool calls")
                    inputs.append(self._build_tool_call_response(call_id, "Error: This tool call isn't processed because of tool call limit, try again"))
                    continue

                if tool_name not in tools:
                    _logger.error("AI: Try to call a forbidden action %s", tool_name)
                    inputs.append(self._build_tool_call_response(call_id, f"Error: unknown tool '{tool_name}'. Try again with the correct tool name."))
                    continue

                has_end_message = "__end_message" in arguments
                end_message = arguments.pop("__end_message", None)
                result, error = tools[tool_name][2](arguments=arguments)

                inputs.append(self._build_tool_call_response(call_id, result))

                if has_end_message and error is None:
                    done = True
                    if end_response := end_message and end_message.strip():
                        all_responses.append(end_response)
                        _logger.info("AI: action terminate early: %s", end_response)
                    else:
                        _logger.info("AI: action terminate early with empty message")

            if session and len(next_actions) > 1:  # Batch of tool calls
                _logger.debug("[AI Tool Summary] Batch #%d completed, %d tool calls", session["current_batch_id"], len(next_actions))

            if done:
                break

        _logger.info("AI: API calls %s", api_call + 1)

        if not all_responses:
            error_msg = "Processing loop ended with no response."
            if api_call + 1 >= AI_MAX_SUCCESSIVE_CALLS:
                error_msg = "Number of successive API calls exceeded, please try again with a more precise request."
            raise ValueError(error_msg)

        return all_responses

    def _request_llm_openai(
        self, llm_model, system_prompts, user_prompts, tools=None,
        files=None, schema=None, temperature=0.2, inputs=(), web_grounding=False
    ):
        user_content = [{"type": "input_text", "text": prompt} for prompt in user_prompts]

        if files:
            def _build_file(idx, file):
                if file["mimetype"] == "text/plain":
                    return {"type": "input_text", "text": file["value"]}

                file_uri = f"data:{file['mimetype']};base64,{file['value']}"
                if file['mimetype'] == 'application/pdf':
                    return {
                        "type": "input_file",
                        "filename": f"file_{idx}.pdf",
                        "file_data": file_uri,
                    }

                assert file["mimetype"].startswith("image/")
                return {"type": "input_image", "image_url": file_uri, "detail": "low"}

            user_content.extend(
                _build_file(idx, file)
                for idx, file in enumerate(files, start=1)
            )

        body = {
            "model": llm_model,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {"type": "input_text", "text": prompt}
                        for prompt in system_prompts
                    ],
                },
                {"role": "user", "content": user_content},
                *inputs,
            ],
            "store": False,
        }
        if llm_model not in ('gpt-5', 'gpt-5-mini'):
            body["temperature"] = temperature

        if schema:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "json_schema",
                    "schema": schema,
                    "strict": True,
                },
            }

        if tools:
            body["tools"] = self._to_open_ai_tool_schema([{
                "description": tool_description,
                "parameters": tool_parameter_schema,
                "type": "function",
                "name": tool_name,
                "strict": True,
            } for tool_name, (tool_description, __, __, tool_parameter_schema) in tools.items()])
            body["parallel_tool_calls"] = True

        if web_grounding:
            search_tool = {
                'type': 'web_search_preview',
            }
            if country_code := self.env.company.country_id.code:
                search_tool['user_location'] = {
                    'type': 'approximate',
                    'country': country_code,
                }
                if city := self.env.company.city:
                    search_tool['user_location']['city'] = city
            body.setdefault("tools", []).append(search_tool)

        with api_call_logging(body["input"], tools) as record_response:
            response, to_call, next_inputs, request_token_usage = self._request_llm_openai_helper(body, tools, inputs)
            if record_response:
                record_response(to_call, response, request_token_usage)
            return response, to_call, next_inputs

    def _to_open_ai_tool_schema(self, schema):
        if self.provider != "openai":
            return schema

        for tool in schema:
            required = tool["parameters"]["required"]
            non_required = set(tool["parameters"]["properties"]) - set(required)
            for name in non_required:
                tool["parameters"]["properties"][name]["type"] = [tool["parameters"]["properties"][name]["type"], "null"]
            tool["parameters"]["required"].extend(non_required)
            tool["parameters"]["additionalProperties"] = False
        return schema

    def _request_llm_openai_helper(self, body, tools=None, inputs=()):
        llm_response = self._request(
            method="post",
            endpoint="/responses",
            headers=self._get_base_headers(),
            body=body,
        )

        to_call = []
        response = []
        next_inputs = list(inputs or ())

        output_lines = llm_response.get("output") or []
        has_tool_calls = any(line.get('type') == 'function_call' for line in output_lines)

        for line in output_lines:
            if line.get('type') == 'function_call':
                tool_name = line.get("name", "")

                try:
                    arguments = json.loads(line.get("arguments") or "")
                except json.decoder.JSONDecodeError:
                    _logger.error("AI: Malformed arguments: %s", line)
                    continue

                to_call.append((tool_name, line.get('call_id'), arguments))
                next_inputs.append(line)

            elif not has_tool_calls:
                if text := line.get('text'):
                    response.append(text)
                elif line.get('type') == 'message':
                    response.extend(t for c in line.get('content', ()) if (t := c.get('text')))

        request_token_usage = {}
        if usage := llm_response.get('usage'):
            request_token_usage["input_tokens"] = usage.get("input_tokens", 0)
            request_token_usage["cached_tokens"] = usage.get('input_tokens_details', {}).get('cached_tokens', 0)
            request_token_usage["output_tokens"] = usage.get("output_tokens", 0)

        return response, to_call, next_inputs, request_token_usage

    def _request(
        self, method: str, endpoint: str, headers: dict[str, str], body: dict,
        data: dict | None = None, files: dict | None = None, params: dict | None = None,
        base_url: str | None = None, timeout: int = 300
    ) -> dict:
        route = f"{base_url or self.base_url}/{endpoint.strip('/')}"
        try:
            response = requests.request(
                method,
                route,
                params=params,
                headers=headers,
                json=body,
                data=data,
                timeout=timeout,
                files=files
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            error = repr(e)
            if e.response is not None:
                try:
                    response = e.response.json()
                    if isinstance(response, list) and response:
                        # Gemini return error in a list
                        response = response[0]
                    if isinstance(response, dict) and (json_error := response.get('error', {}).get('message')):
                        error = json_error
                    else:
                        error = json.dumps(response, indent=2)
                except ValueError:
                    error = e.response.text
                if not error:
                    error = repr(e)

            _logger.warning("LLM API request failed: %s", error)
            raise UserError(error)

    def _request_llm_deepseek(
            self, llm_model: str, system_prompts: list[str], user_prompts: list[str],
            tools: dict | None = None, files: list[dict] | None = None, schema: dict | None = None,
            temperature: float = 0.2, inputs: list[dict] | None = None, web_grounding: bool = False
    ):
        messages = []
        for prompt in system_prompts:
            messages.append({"role": "system", "content": prompt})

        user_content = "\n".join(user_prompts)
        if files:
            file_texts = [f.get("value", "") for f in files if f.get("mimetype") == "text/plain"]
            if file_texts:
                user_content += "\n\n[Attached Files Content]:\n" + "\n".join(file_texts)

        messages.append({"role": "user", "content": user_content})

        if inputs:
            for input_item in inputs:
                if isinstance(input_item, dict):
                    messages.append(input_item)

        body = {
            "model": llm_model or "deepseek-v4-flash",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "stream": False,
        }

        if schema:
            body["response_format"] = {"type": "json_object"}

        if tools:
            # plain function calling on /v1. The former "strict" mode is a
            # /beta-only feature and forced every parameter to be required,
            # which changed the tools' contract (and mutated the shared schema).
            body["tools"] = [{
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": tool_desc,
                    "parameters": tool_params,
                },
            } for tool_name, (tool_desc, __, __, tool_params) in tools.items()]

        llm_response = self._request(
            method="post",
            endpoint="/chat/completions",
            headers=self._get_base_headers(),
            body=body,
            base_url=self.base_url
        )

        to_call = []
        response = []
        next_inputs = list(inputs or [])
        request_token_usage = {}

        if choices := llm_response.get("choices"):
            message = choices[0].get("message", {})

            next_inputs.append(message)

            if tool_calls := message.get("tool_calls"):
                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"]
                    tool_call_id = tool_call["id"]
                    arguments_str = tool_call["function"]["arguments"]
                    try:
                        arguments = json.loads(arguments_str)
                    except json.decoder.JSONDecodeError as e:
                        _logger.error("AI: Malformed arguments for %s: %s", tool_name, arguments_str)
                        continue
                    to_call.append((tool_name, tool_call_id, arguments))

            if content := message.get("content"):
                response.append(content)
            elif not to_call and (reasoning := message.get("reasoning_content")):
                # V4 thinking mode: reasoning lives in reasoning_content, not content
                response.append(_reasoning_only_fallback(reasoning))

        if usage := llm_response.get("usage"):
            request_token_usage["input_tokens"] = usage.get("prompt_tokens", 0)
            request_token_usage["cached_tokens"] = _cached_tokens(usage)
            request_token_usage["output_tokens"] = usage.get("completion_tokens", 0)

        # This legacy handler predates api_call_logging: feed the logging
        # session directly so token usage persistence sees this call
        if session := get_ai_logging_session():
            session["api_calls"] += 1
            session["real_input_tokens"] += request_token_usage.get("input_tokens", 0)
            session["output_tokens"] += request_token_usage.get("output_tokens", 0)
            session["cached_tokens"] = request_token_usage.get("cached_tokens", 0)

        return response, to_call, next_inputs

    def _request_llm_qwen(
            self, llm_model: str, system_prompts: list[str], user_prompts: list[str],
            tools: dict | None = None, files: list[dict] | None = None, schema: dict | None = None,
            temperature: float = 0.2, inputs: list[dict] | None = None, web_grounding: bool = False
    ):
        messages = []
        for prompt in system_prompts:
            messages.append({"role": "system", "content": prompt})

        user_content = "\n".join(user_prompts)
        if files:
            file_texts = [f.get("value", "") for f in files if f.get("mimetype") == "text/plain"]
            if file_texts:
                user_content += "\n\n[Attached Files Content]:\n" + "\n".join(file_texts)

        messages.append({"role": "user", "content": user_content})

        if inputs:
            for input_item in inputs:
                if isinstance(input_item, dict):
                    messages.append(input_item)

        body = {
            "model": llm_model or "qwen-plus-latest",
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }

        if tools:
            body["tools"] = []
            for tool_name, (tool_desc, _, _, tool_params) in tools.items():
                function_def = {
                    "name": tool_name,
                    "description": tool_desc,
                    "parameters": tool_params,
                }
                body["tools"].append({
                    "type": "function",
                    "function": function_def,
                })

        if web_grounding:
            body["enable_search"] = True

        llm_response = self._request(
            method="post",
            endpoint="/chat/completions",
            headers=self._get_base_headers(),
            body=body,
            base_url=self.base_url
        )

        to_call = []
        response = []
        next_inputs = list(inputs or [])
        request_token_usage = {}

        if choices := llm_response.get("choices"):
            message = choices[0].get("message", {})
            next_inputs.append(message)

            if tool_calls := message.get("tool_calls"):
                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"]
                    tool_call_id = tool_call["id"]
                    arguments_str = tool_call["function"]["arguments"]
                    try:
                        arguments = json.loads(arguments_str)
                    except json.decoder.JSONDecodeError as e:
                        _logger.error("AI: Malformed arguments for %s: %s", tool_name, arguments_str)
                        continue
                    to_call.append((tool_name, tool_call_id, arguments))

            if content := message.get("content"):
                response.append(content)

        if usage := llm_response.get("usage"):
            request_token_usage["input_tokens"] = usage.get("prompt_tokens", 0)
            request_token_usage["cached_tokens"] = usage.get("cached_tokens", 0)
            request_token_usage["output_tokens"] = usage.get("completion_tokens", 0)

        # This legacy handler predates api_call_logging: feed the logging
        # session directly so token usage persistence sees this call
        if session := get_ai_logging_session():
            session["api_calls"] += 1
            session["real_input_tokens"] += request_token_usage.get("input_tokens", 0)
            session["output_tokens"] += request_token_usage.get("output_tokens", 0)
            session["cached_tokens"] = request_token_usage.get("cached_tokens", 0)

        return response, to_call, next_inputs

    def _request_llm_openai_compat(
            self, llm_model: str, system_prompts: list[str], user_prompts: list[str],
            tools: dict | None = None, files: list[dict] | None = None, schema: dict | None = None,
            temperature: float = 0.2, inputs: list[dict] | None = None, web_grounding: bool = False
    ):
        """Generic handler for any provider exposing an OpenAI-compatible
        /chat/completions endpoint (Kimi/Moonshot, Zhipu GLM, ...).

        Only relies on the standard Chat Completions contract: messages,
        tools (function calling), response_format and usage. Vendor-private
        parameters (e.g. web search) are intentionally not sent.
        """
        messages = []
        for prompt in system_prompts:
            messages.append({"role": "system", "content": prompt})

        user_content = "\n".join(user_prompts)
        if files:
            file_texts = [f.get("value", "") for f in files if f.get("mimetype") == "text/plain"]
            if file_texts:
                user_content += "\n\n[Attached Files Content]:\n" + "\n".join(file_texts)

        if user_content:
            messages.append({"role": "user", "content": user_content})

        for input_item in inputs or []:
            if isinstance(input_item, dict):
                messages.append(input_item)

        body = {
            "model": llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "stream": False,
        }

        if schema:
            body["response_format"] = {"type": "json_object"}

        if tools:
            body["tools"] = [{
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": tool_desc,
                    "parameters": tool_params,
                },
            } for tool_name, (tool_desc, __, __, tool_params) in tools.items()]

        if web_grounding:
            _logger.warning("AI: web grounding is not supported for provider '%s', ignoring", self.provider)

        with api_call_logging(messages, tools) as record_response:
            llm_response = self._request(
                method="post",
                endpoint="/chat/completions",
                headers=self._get_base_headers(),
                body=body,
            )

            to_call = []
            response = []
            next_inputs = list(inputs or [])
            request_token_usage = {}

            if choices := llm_response.get("choices"):
                message = choices[0].get("message", {})
                next_inputs.append(message)

                for tool_call in message.get("tool_calls") or []:
                    tool_name = tool_call["function"]["name"]
                    arguments_str = tool_call["function"]["arguments"]
                    try:
                        arguments = json.loads(arguments_str)
                    except json.decoder.JSONDecodeError:
                        _logger.error("AI: Malformed arguments for %s: %s", tool_name, arguments_str)
                        continue
                    to_call.append((tool_name, tool_call["id"], arguments))

                if content := message.get("content"):
                    response.append(content)
                elif not to_call and (reasoning := message.get("reasoning_content")
                                      or message.get("reasoning")):
                    # thinking models (Kimi K2.6 thinking, GLM) put the chain of
                    # thought in reasoning_content; an empty content is not "no answer"
                    response.append(_reasoning_only_fallback(reasoning))

            if usage := llm_response.get("usage"):
                request_token_usage["input_tokens"] = usage.get("prompt_tokens", 0)
                request_token_usage["cached_tokens"] = _cached_tokens(usage)
                request_token_usage["output_tokens"] = usage.get("completion_tokens", 0)

            if record_response:
                record_response(to_call, response, request_token_usage)

            return response, to_call, next_inputs

    def _get_base_headers(self) -> dict[str, str]:
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self._get_api_token()}',
        }

    def _get_api_token(self, raise_if_missing: bool = True):
        provider_config = {
            "openai": {
                "config_key": "ai_chatbot_enhance.openai_key",
                "env_var": "ODOO_AI_CHATGPT_TOKEN",
            },
            "deepseek": {
                "config_key": "ai_chatbot_enhance.deepseek_key",
                "env_var": "ODOO_AI_DEEPSEEK_TOKEN",
            },
            "google": {
                "config_key": "ai_chatbot_enhance.google_key",
                "env_var": "ODOO_AI_GEMINI_TOKEN",
            },
            "qwen": {
                "config_key": "ai_chatbot_enhance.qwen_key",
                "env_var": "ODOO_AI_QWEN_TOKEN",
            },
        }
        config = provider_config.get(self.provider)
        if config is None and self.provider in OPENAI_COMPAT_PROVIDERS:
            config = {
                "config_key": f"ai_chatbot_enhance.{self.provider}_key",
                "env_var": f"ODOO_AI_{self.provider.upper()}_TOKEN",
            }
        if config is None:
            raise UserError(_("Unsupported provider '%s'", self.provider))

        if api_key := self.env["ir.config_parameter"].sudo().get_param(config["config_key"]) or os.getenv(config["env_var"]):
            return api_key.strip()

        if not raise_if_missing:
            return None
        raise UserError(_("No API key set for provider '%s'", self.provider))

    def _request_llm_google(
        self, llm_model, system_prompts, user_prompts, tools=None,
        files=None, schema=None, temperature=0.2, inputs=(), web_grounding=False,
    ):
        if (tools or web_grounding) and schema:
            raise NotImplementedError("Gemini does not support structured output with tools")
        if web_grounding and tools:
            # https://ai.google.dev/gemini-api/docs/function-calling?example=meeting#native-tools
            # see note, live api feature only for the moment
            raise NotImplementedError("Gemini does not support tools with web grounding")
        body = {
            "contents": [],
            "generationConfig": {
                "temperature": temperature,
            },
        }
        if system_prompts:
            body["systemInstruction"] = {
                "parts": [
                    {"text": prompt}
                    for prompt in system_prompts
                ],
            }
        if user_prompts:
            body["contents"].append({
                "role": "user",
                "parts": [
                    {"text": prompt}
                    for prompt in user_prompts
                ],
            })

        body["contents"].extend(inputs)

        if files:
            def _build_file(idx, file):
                if file["mimetype"] == "text/plain":
                    return {"text": file["value"]}

                return {"inline_data": {"mime_type": file['mimetype'], "data": file["value"]}}

            body["contents"].append({"role": "user", "parts":
                [_build_file(idx, file) for idx, file in enumerate(files, start=1)]})

        if schema:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseJsonSchema"] = schema

        if tools:
            body["tools"] = {
                "functionDeclarations": [{
                    "description": tool_description,
                    "parameters": tool_parameter_schema,
                    "name": tool_name,
                } for tool_name, (tool_description, __, __, tool_parameter_schema) in tools.items()]
            }
        if web_grounding:
            body["tools"] = {'google_search': {}}

        if llm_model == "gemini-2.5-flash":
            # from testing, increasing thinking budget results in the LLM actually replying ¯\_(ツ)_/¯
            body['generationConfig']["thinkingConfig"] = {
                "thinkingBudget": 512,
            }

        with api_call_logging(body["contents"], tools) as record_response:
            response = None
            to_call = []
            next_inputs = inputs
            request_token_usage = {}
            for attempt in range(3):
                response, to_call, next_inputs, request_token_usage = self._request_llm_google_helper(body, llm_model, inputs)
                if response or to_call:
                    break
                _logger.warning("Gemini failed to generate a response, retrying...")
            if not (response or to_call):
                response = "Error: failed to generate a response, try again later."
            if record_response:
                record_response(to_call, response, request_token_usage)
            return response, to_call, next_inputs

    def _request_llm_google_helper(self, body, llm_model, inputs=()):
        llm_response = self._request(
            method="post",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            headers={"x-goog-api-key": self._get_api_token()},
            endpoint=f"/models/{llm_model}:generateContent",
            params={},
            body=body,
        )

        to_call = []
        response = []
        next_inputs = list(inputs or ())

        candidates = llm_response.get("candidates") or []
        has_tool_calls = any(
            part.get('functionCall')
            for candidate in candidates
            for part in candidate.get('content', {}).get('parts') or []
        )

        for candidate in candidates:
            for line in candidate.get('content', {}).get('parts') or ():
                if f_info := line.get('functionCall'):
                    to_call.append((f_info['name'], f_info['name'], f_info['args']))
                    next_inputs.append({"role": "model", "parts": [line]})
                elif not has_tool_calls:
                    if r := line.get('text'):
                        response.append(r)
                    else:
                        _logger.warning("Gemini: could not parse %s", line)

        request_token_usage = {}
        if usage := llm_response.get("usageMetadata"):
            request_token_usage["input_tokens"] = usage.get("promptTokenCount", 0)
            request_token_usage["cached_tokens"] = usage.get("cachedContentTokenCount", 0)
            request_token_usage["output_tokens"] = usage.get("candidatesTokenCount", 0)

        return response, to_call, next_inputs, request_token_usage

    def get_ai_embedding(
        self,
        input: str | list[str] | list[int] | list[list[int]],
        dimensions: int,
        model: str = 'text-embedding-3-small',
        encoding_format: str | None = None,
        user: str | None = None,
    ) -> EmbeddingResponse:
        check_model_depreciation(self.env, model)
        body = {
            'input': input,
            'model': model
        }
        self._add_if_set(body, 'encoding_format', encoding_format)
        self._add_if_set(body, 'dimensions', dimensions)
        self._add_if_set(body, 'user', user)

        return self._request(
            method='post',
            endpoint='/embeddings',
            headers=self._get_base_headers(),
            body=body,
        )

    def _add_if_set(self, d: dict, key: str, value):
        if value is not None:
            d[key] = value

    def _build_tool_call_response(self, tool_call_id, return_value):
        """Build the response for the given tool call.

        :param tool_call_id: The identifier of the tool call
        :param return_value: The value the tool returned
        """
        if self.provider == "openai":
            return {
                "type": "function_call_output",
                "call_id": tool_call_id,
                "output": str(return_value),
            }

        if self.provider == "google":
            return {
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": tool_call_id,
                        "response": {"result": str(return_value)},
                    },
                }],
            }

        if self.provider == "deepseek":
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": str(return_value),
            }

        if self.provider == "qwen":
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": str(return_value),
            }

        if self.provider in OPENAI_COMPAT_PROVIDERS:
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": str(return_value),
            }

        raise NotImplementedError(f"Unsupported provider: {self.provider}")
