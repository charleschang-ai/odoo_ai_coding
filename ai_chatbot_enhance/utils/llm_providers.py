# Part of Odoo. See LICENSE file for full copyright and licensing details.
from typing import NamedTuple

from odoo.api import Environment
from odoo.exceptions import UserError


class Provider(NamedTuple):
    name: str
    display_name: str
    embedding_model: str
    embedding_config: dict
    llms: list[tuple[str, str]]


PROVIDERS = [
    Provider(
        "openai",
        "OpenAI",
        "text-embedding-3-small",
        {
            # https://platform.openai.com/docs/api-reference/embeddings/create
            "max_batch_size": 2048,
            "max_tokens_per_request": 200000,
        },
        [
            ("gpt-3.5-turbo", "GPT-3.5 Turbo"),
            ("gpt-4", "GPT-4"),
            ("gpt-4o", "GPT-4o"),
            ("gpt-4.1", "GPT-4.1"),
            ("gpt-4.1-mini", "GPT-4.1 Mini"),
            ("gpt-5", "GPT-5"),
            ("gpt-5-mini", "GPT-5 Mini"),
        ],
    ),
    Provider(
        "google",
        "Google",
        "gemini-embedding-001",
        {
            # https://googleapis.dev/python/generativelanguage/latest/_modules/google/ai/generativelanguage_v1alpha/types/text_service.html#BatchEmbedTextRequest
            "max_batch_size": 100,
            "max_tokens_per_request": 10000,
        },
        [
            ("gemini-3.5-flash", "Gemini 3.5 Flash"),
            ("gemini-3.1-flash-lite", "Gemini 3.1 Flash lite"),
            ("gemini-2.5-pro", "Gemini 2.5 Pro"),
            ("gemini-2.5-flash", "Gemini 2.5 Flash"),
            ("gemini-1.5-pro", "Gemini 1.5 Pro"),
            ("gemini-1.5-flash", "Gemini 1.5 Flash"),
        ],
    ),
    Provider(
        "deepseek",
        "DeepSeek",
        "deepseek-embedding",
        {
            "max_batch_size": 100,
            "max_tokens_per_request": 128000,
        },
        [
            # V4 line (since 2026-04). The old names deepseek-chat /
            # deepseek-reasoner were retired from list-models in 2026-07: they
            # are mapped to the V4 equivalents in LEGACY_ALIASES, don't relist them.
            ("deepseek-v4-flash", "DeepSeek V4 Flash (fast & cheap, daily driver)"),
            ("deepseek-v4-pro", "DeepSeek V4 Pro (strongest: complex reasoning / long tasks)"),
        ],
    ),
    Provider(
        "qwen",
        "Qwen",
        "text-embedding-v2",
        # "text-embedding-v4",
        {
            "max_batch_size": 25,
            # "max_tokens_per_request": 8192,
            "max_tokens_per_request": 2048,
        },
        [
            ("qwen-plus-latest", "Qwen Plus Latest"),
            ("qwen-max", "Qwen Max"),
        ],
    ),
    # OpenAI-compatible providers below are routed to the generic handler
    # (see OPENAI_COMPAT_PROVIDERS in llm_api_service.py). They don't support
    # embeddings here, so embedding_model / embedding_config are placeholders.
    Provider(
        "kimi",
        "Kimi (Moonshot)",
        "",
        {},
        [
            # Checked one by one against platform.kimi.com/docs/models on
            # 2026-08-26. Both Kimi platforms are served: a Moonshot open
            # platform key (sk-...) goes to api.moonshot.cn, a new Kimi platform
            # key (sk-kimi-...) goes to api.kimi.com — LLMApiService picks the
            # endpoint from the key prefix, the model ids are the same.
            ("kimi-k3", "Kimi K3 (flagship: vision, 1M context)"),
            ("kimi-k2.7-code", "Kimi K2.7 Code (coding, 256k)"),
            ("kimi-k2.7-code-highspeed", "Kimi K2.7 Code High-speed (~180 tok/s)"),
            ("kimi-k2.6", "Kimi K2.6 (general: vision, thinking / non-thinking, 256k)"),
            ("kimi-k2.5", "Kimi K2.5 (previous generation)"),
        ],
    ),
    Provider(
        "zhipu",
        "Zhipu GLM",
        "",
        {},
        [
            ("glm-4.6", "GLM-4.6"),
            ("glm-4.5", "GLM-4.5"),
            ("glm-4.5-air", "GLM-4.5 Air"),
        ],
    ),
]

PROVIDERS_BY_NAME = {p.name: p for p in PROVIDERS}

DEPRECATED_MODELS = ["gemini-1.5-pro", "gemini-1.5-flash"]

# Retired model name -> live equivalent. Vendors retire names without waiting
# for users to update agents; a stored model must keep working instead of
# failing with "no provider found". Applied by canonical_model() right before
# the request, so the agent record itself is left untouched.
LEGACY_ALIASES = {
    "deepseek-chat": "deepseek-v4-flash",       # retired 2026-07, was routed to v4-flash non-thinking
    "deepseek-reasoner": "deepseek-v4-pro",     # same, thinking-mode equivalent
}

# Model-name prefix -> provider. The lists above are suggestions, not a
# whitelist: every vendor ships new ids every few weeks and a hand-entered or
# previously stored id (e.g. kimi-k2-0905-preview) must still route to the
# right endpoint without a code change.
_PREFIX_RULES = [
    (("deepseek",), "deepseek"),
    (("gemini", "gemma", "learnlm"), "google"),
    (("gpt-", "o1", "o3", "o4", "chatgpt"), "openai"),
    (("qwen", "qwq", "tongyi"), "qwen"),
    (("moonshot", "kimi", "k3"), "kimi"),
    (("glm", "chatglm", "codegeex"), "zhipu"),
]

SUPPORT_EMBEDDING_MODEL = ["openai", "google", "qwen"]

EMBEDDING_MODELS_SELECTION = [
    (provider.embedding_model, provider.display_name) for provider in PROVIDERS if
    provider.name in SUPPORT_EMBEDDING_MODEL
]


def get_provider_for_embedding_model(env, embedding_model):
    for p in PROVIDERS:
        if p.embedding_model == embedding_model and p.name in SUPPORT_EMBEDDING_MODEL:
            return p.name
    raise UserError(env._("No provider found for the embedding model"))


def canonical_model(llm_model: str) -> str:
    """Replace a retired model name by its live equivalent; others unchanged."""
    model = (llm_model or "").strip()
    return LEGACY_ALIASES.get(model, model)


def provider_by_prefix(llm_model: str) -> str | None:
    low = (llm_model or "").strip().lower()
    if not low:
        return None
    for prefixes, name in _PREFIX_RULES:
        if any(low.startswith(x) for x in prefixes):
            return name
    return None


def get_provider(env, llm_model):
    model = canonical_model(llm_model)
    for p in PROVIDERS:
        if model in [m[0] for m in p.llms]:
            return p.name
    if name := provider_by_prefix(model):
        return name
    raise UserError(env._("No provider found for the selected model"))


def get_provider_display_name(provider_name: str) -> str:
    p = PROVIDERS_BY_NAME.get(provider_name)
    return p.display_name if p else (provider_name or "")


def get_embedding_config(env, provider):
    for p in PROVIDERS:
        if p.name == provider:
            return p.embedding_config
    raise UserError(env._("No embedding configuration found for the provider"))


def check_model_depreciation(env: Environment, model: str) -> None:
    if model in DEPRECATED_MODELS:
        for provider in PROVIDERS:
            for name, label in provider.llms:
                if name == model:
                    raise UserError(env._("%s is no longer available. Please select a newer model.", label))
