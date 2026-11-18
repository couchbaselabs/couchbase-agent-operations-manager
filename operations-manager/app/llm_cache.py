"""
LLM response caching for agents.

Agents that route their model calls through the operations manager get the
same treatment their *tool* calls already get: one governed choke point that
sees every request, applies policy, and writes an auditable record. The
difference is what the choke point buys you here - a cache.

Three things live in this module:

  1. The provider catalog (Claude / ChatGPT / Gemini / AWS Bedrock), including per-model
     token pricing so "tokens saved" can be reported as money saved. Prices
     are list-price *estimates* and are meant to be edited - see PRICING.
  2. The cache key + invalidation policy engine. Everything a user can
     configure on the LLM Caching setup page is evaluated here, so the
     policy has exactly one implementation.
     Admins can add further providers at runtime - any endpoint speaking
     OpenAI's chat-completions API, with presets for Azure OpenAI,
     Snowflake Cortex, Databricks, Google DeepMind, Cohere, Mistral AI and
     self-hosted servers. See "Custom providers" below.
  3. Thin provider adapters over `requests`. If no API key is configured
     for the selected provider, the adapter returns a clearly-labelled
     deterministic stub instead of failing, so the whole feature (including
     the savings dashboard) is demoable with no outbound network access -
     the same "works on first boot" posture as the bundled sample MCP
     servers.

Nothing in here talks to Couchbase; persistence lives in
app/couchbase_client.py and orchestration lives in app/main.py.
"""
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger("operations-manager.llm_cache")

# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------
# `env_key` is the environment variable the operations manager reads the
# provider's API key from. A provider with no key configured still works -
# it just answers from the offline stub on a cache miss (see call_provider).
PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "id": "anthropic",
        "label": "Claude",
        "vendor": "Anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "endpoint": "https://api.anthropic.com/v1/messages",
        "docs_url": "https://docs.claude.com/en/api/messages",
        "models": ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"],
        "default_model": "claude-sonnet-4-5",
    },
    "openai": {
        "id": "openai",
        "label": "ChatGPT",
        "vendor": "OpenAI",
        "env_key": "OPENAI_API_KEY",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "docs_url": "https://platform.openai.com/docs/api-reference/chat",
        # gpt-5.6-luna is OpenAI's low-cost tier (1.1M context) - the
        # bundled default, so a fresh install spends as little as possible.
        "models": ["gpt-5.6-luna", "gpt-5", "gpt-5-mini", "gpt-4o"],
        "default_model": "gpt-5.6-luna",
    },
    "google": {
        "id": "google",
        "label": "Gemini",
        "vendor": "Google",
        "env_key": "GEMINI_API_KEY",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
        "docs_url": "https://ai.google.dev/api/generate-content",
        "models": ["gemini-3.1-pro-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
        "default_model": "gemini-2.5-flash",
    },
    # Amazon Bedrock. A "model" here is a Bedrock model ID or inference
    # profile ID, called through the Converse API (one request shape for
    # every model family). The list below is the pay-per-token catalog that
    # needs no provisioning; add custom / provisioned-throughput ARNs or
    # other model IDs through AWS_BEDROCK_MODEL_IDS (comma-separated).
    #
    # Credentials come from the standard AWS chain, so any of these works:
    #   - AWS_BEARER_TOKEN_BEDROCK (a Bedrock API key - the simplest)
    #   - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (/ AWS_SESSION_TOKEN)
    #   - AWS_BEDROCK_USE_DEFAULT_CREDENTIALS=true to trust the pod / instance
    #     role (IRSA, EKS Pod Identity, an EC2 instance profile)
    # The region comes from AWS_BEDROCK_REGION (then AWS_REGION /
    # AWS_DEFAULT_REGION, then us-east-1). See config.LLM_API_KEYS.
    "bedrock": {
        "id": "bedrock",
        "label": "Bedrock",
        "vendor": "Amazon Web Services",
        "env_key": "AWS_BEARER_TOKEN_BEDROCK",
        "endpoint": "https://bedrock-runtime.{region}.amazonaws.com/model/{model}/converse",
        "docs_url": "https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html",
        "models": [
            "amazon.nova-pro-v1:0",
            "amazon.nova-lite-v1:0",
            "amazon.nova-micro-v1:0",
            "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.meta.llama3-3-70b-instruct-v1:0",
        ],
        "default_model": "amazon.nova-lite-v1:0",
    },
}

for _model_id in (os.getenv("AWS_BEDROCK_MODEL_IDS") or "").split(","):
    _model_id = _model_id.strip()
    if _model_id and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", _model_id) \
            and _model_id not in PROVIDERS["bedrock"]["models"]:
        PROVIDERS["bedrock"]["models"].append(_model_id)

# USD per 1,000,000 tokens, (input, output). These are *estimates* used to
# turn "tokens saved" into "dollars saved" on the dashboard - they are not
# billing data. Override them for your own negotiated rates.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-5": (5.00, 25.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4o": (2.50, 10.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.075, 0.30),
    # Bedrock on-demand list prices, us-east-1. Cross-region inference
    # profiles (global./us. prefixes) bill at the underlying model's rate.
    "amazon.nova-pro-v1:0": (0.80, 3.20),
    "amazon.nova-lite-v1:0": (0.06, 0.24),
    "amazon.nova-micro-v1:0": (0.035, 0.14),
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0": (3.00, 15.00),
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": (1.00, 5.00),
    "us.meta.llama3-3-70b-instruct-v1:0": (0.72, 0.72),
}

# ---------------------------------------------------------------------------
# Custom providers
# ---------------------------------------------------------------------------
# Providers an admin adds from LLM Caching -> Providers & Policy. Every one
# is an endpoint that speaks OpenAI's chat-completions API, which covers the
# hosted platforms in CUSTOM_PRESETS as well as anything self-hosted (Ollama,
# vLLM, TGI, LiteLLM, an internal gateway).
#
# A custom provider is registered into PROVIDERS under a "custom-<slug>" ID,
# so the policy engine, per-role overrides, RAG applications, the dashboard
# and the topology view treat it exactly like a built-in one. What differs:
#   - its API key is entered in the dashboard and stored encrypted by
#     app/main.py (settings::custom_llm_providers); the plaintext lives only
#     in CUSTOM_SECRETS here, never in the environment and never in an API
#     response;
#   - its model list and per-model prices are whatever the admin entered
#     (CUSTOM_PRICING), keyed by provider as well as model so a self-hosted
#     "gpt-4o" alias does not inherit OpenAI's list price;
#   - it never answers from the offline stub. An endpoint was configured, so
#     a failure is a failure (same rule as a built-in provider with a key).
CUSTOM_ID_PREFIX = "custom-"
MAX_CUSTOM_PROVIDERS = 25
MAX_CUSTOM_MODELS = 50
MAX_TOKENS_PARAMS = ("max_tokens", "max_completion_tokens")
# How the API key is sent: "Authorization: Bearer <key>" (almost everyone)
# or Azure's "api-key: <key>" header.
AUTH_HEADERS = ("bearer", "api-key")
_CUSTOM_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}")

# Starting points for the "Add provider" form. `base_url` is the root the
# adapter appends /chat/completions to; a <placeholder> in it must be
# replaced before the provider can be saved. Model IDs and prices are
# suggestions - list-price estimates in USD per 1M tokens, editable like
# PRICING - because every one of these platforms changes its line-up more
# often than this file is released.
CUSTOM_PRESETS: list[dict] = [
    {
        "id": "azure_openai",
        "label": "Azure OpenAI",
        "vendor": "Microsoft Azure",
        "base_url": "https://<resource-name>.openai.azure.com/openai/v1",
        "docs_url": "https://learn.microsoft.com/azure/ai-foundry/openai/api-version-lifecycle",
        "api_key_label": "Azure OpenAI API key",
        "api_key_required": True,
        "auth_header": "api-key",
        "max_tokens_param": "max_completion_tokens",
        "notes": "Replace <resource-name> with your Azure OpenAI (or Foundry) resource. This is the v1 API, so "
                 "no api-version is needed. A model here is your deployment name, not the underlying model "
                 "name - replace the suggestions below with the deployments in your resource.",
        "models": [
            {"id": "gpt-5", "input_usd_per_1m": 1.25, "output_usd_per_1m": 10.00},
            {"id": "gpt-5-mini", "input_usd_per_1m": 0.25, "output_usd_per_1m": 2.00},
            {"id": "gpt-4o", "input_usd_per_1m": 2.50, "output_usd_per_1m": 10.00},
        ],
    },
    {
        "id": "snowflake_cortex",
        "label": "Snowflake Cortex",
        "vendor": "Snowflake",
        "base_url": "https://<account-identifier>.snowflakecomputing.com/api/v2/cortex/v1",
        "docs_url": "https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-rest-api",
        "api_key_label": "Programmatic access token (PAT)",
        "api_key_required": True,
        "max_tokens_param": "max_completion_tokens",
        "notes": "Replace <account-identifier> with your Snowflake account. The token's role needs the "
                 "SNOWFLAKE.CORTEX_USER database role, and each model must be available in the account's region. "
                 "Cortex bills in credits - set the prices to your contracted rate.",
        "models": [
            {"id": "claude-sonnet-4-5", "input_usd_per_1m": 3.00, "output_usd_per_1m": 15.00},
            {"id": "openai-gpt-5", "input_usd_per_1m": 1.25, "output_usd_per_1m": 10.00},
            {"id": "llama3.3-70b", "input_usd_per_1m": 0.72, "output_usd_per_1m": 0.72},
            {"id": "mistral-large2", "input_usd_per_1m": 2.00, "output_usd_per_1m": 6.00},
        ],
    },
    {
        "id": "databricks",
        "label": "Databricks-hosted LLMs",
        "vendor": "Databricks",
        "base_url": "https://<workspace-host>/serving-endpoints",
        "docs_url": "https://docs.databricks.com/aws/en/machine-learning/model-serving/score-foundation-models",
        "api_key_label": "Personal access token or service-principal token",
        "api_key_required": True,
        "max_tokens_param": "max_tokens",
        "notes": "Replace <workspace-host> with your workspace URL. A model here is a serving endpoint name: "
                 "a pay-per-token Foundation Model API endpoint, a provisioned-throughput endpoint or an "
                 "external model. Databricks bills in DBUs - set the prices to your rate.",
        "models": [
            {"id": "databricks-claude-sonnet-4-5", "input_usd_per_1m": 3.00, "output_usd_per_1m": 15.00},
            {"id": "databricks-meta-llama-3-3-70b-instruct", "input_usd_per_1m": 0.50, "output_usd_per_1m": 1.50},
            {"id": "databricks-gpt-oss-120b", "input_usd_per_1m": 0.15, "output_usd_per_1m": 0.60},
            {"id": "databricks-gemini-2-5-pro", "input_usd_per_1m": 1.25, "output_usd_per_1m": 10.00},
        ],
    },
    {
        "id": "google_deepmind",
        "label": "Google DeepMind",
        "vendor": "Google DeepMind",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "docs_url": "https://ai.google.dev/gemini-api/docs/openai",
        "api_key_label": "Gemini API key (Google AI Studio)",
        "api_key_required": True,
        "max_tokens_param": "max_tokens",
        "notes": "Google DeepMind's Gemini and Gemma models through the Gemini API's OpenAI-compatible "
                 "endpoint. Separate from the built-in Gemini provider, which uses GEMINI_API_KEY from the "
                 "environment and the native generateContent API.",
        "models": [
            {"id": "gemini-2.5-pro", "input_usd_per_1m": 1.25, "output_usd_per_1m": 10.00},
            {"id": "gemini-2.5-flash", "input_usd_per_1m": 0.30, "output_usd_per_1m": 2.50},
            {"id": "gemma-3-27b-it", "input_usd_per_1m": 0.00, "output_usd_per_1m": 0.00},
        ],
    },
    {
        "id": "cohere",
        "label": "Cohere",
        "vendor": "Cohere",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "docs_url": "https://docs.cohere.com/docs/compatibility-api",
        "api_key_label": "Cohere API key",
        "api_key_required": True,
        "max_tokens_param": "max_tokens",
        "notes": "Cohere's Command models through its Compatibility API.",
        "models": [
            {"id": "command-a-03-2025", "input_usd_per_1m": 2.50, "output_usd_per_1m": 10.00},
            {"id": "command-r-plus-08-2024", "input_usd_per_1m": 2.50, "output_usd_per_1m": 10.00},
            {"id": "command-r7b-12-2024", "input_usd_per_1m": 0.0375, "output_usd_per_1m": 0.15},
        ],
    },
    {
        "id": "mistral",
        "label": "Mistral AI",
        "vendor": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "docs_url": "https://docs.mistral.ai/api/",
        "api_key_label": "Mistral API key",
        "api_key_required": True,
        "max_tokens_param": "max_tokens",
        "notes": "Mistral's hosted models (La Plateforme). The -latest aliases follow Mistral's current "
                 "release of each tier, so check the prices against the model they point to today.",
        "models": [
            {"id": "mistral-large-latest", "input_usd_per_1m": 2.00, "output_usd_per_1m": 6.00},
            {"id": "mistral-medium-latest", "input_usd_per_1m": 0.40, "output_usd_per_1m": 2.00},
            {"id": "mistral-small-latest", "input_usd_per_1m": 0.10, "output_usd_per_1m": 0.30},
        ],
    },
    {
        "id": "self_hosted",
        "label": "Self-hosted LLM",
        "vendor": "Self-hosted",
        "base_url": "http://<host>:<port>/v1",
        "docs_url": "",
        "api_key_label": "API key (leave blank if the server needs none)",
        "api_key_required": False,
        "max_tokens_param": "max_tokens",
        "notes": "Any server that speaks OpenAI's /chat/completions API: Ollama (http://host:11434/v1), "
                 "vLLM, Text Generation Inference, LM Studio, llama.cpp, LiteLLM. The URL must be reachable "
                 "from the operations-manager container. Prices default to 0 - enter an amortized cost per "
                 "1M tokens if you want cache hits to show as dollars saved.",
        "models": [],
    },
]
CUSTOM_PRESET_BY_ID = {p["id"]: p for p in CUSTOM_PRESETS}

# id -> stored document (see custom_provider), id -> plaintext API key,
# (id, model) -> (input, output) USD per 1M tokens.
CUSTOM: dict[str, dict] = {}
CUSTOM_SECRETS: dict[str, str] = {}
CUSTOM_PRICING: dict[tuple[str, str], tuple[float, float]] = {}


def _price(value) -> float:
    try:
        return round(max(0.0, min(float(value or 0.0), 100_000.0)), 6)
    except (TypeError, ValueError):
        return 0.0


# "text-embedding-3-large", "embed-v4.0", "amazon.titan-embed-text-v2:0" ...
_EMBEDDING_MODEL_ID = re.compile(r"(^|[^a-z])embed(ding|dings)?([^a-z]|$)", re.IGNORECASE)
_AZURE_HOSTS = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")


def _azure_base_url(base_url: str) -> str:
    """Azure's portal shows the bare resource endpoint
    (https://<resource>.openai.azure.com/), which is what people paste. The
    OpenAI-compatible API lives under /openai/v1 on that host, so add it
    when the path is missing rather than save a URL that can only 404."""
    scheme, _, rest = base_url.partition("://")
    host, slash, path = rest.partition("/")
    if not host.lower().endswith(_AZURE_HOSTS):
        return base_url
    path = (slash + path).rstrip("/")
    if path in ("", "/openai"):
        return f"{scheme}://{host}/openai/v1"
    if "/deployments/" in path:
        raise ValueError(
            "That is a deployment-specific Azure URL. Use the resource's v1 endpoint instead - "
            f"{scheme}://{host}/openai/v1 - and enter each deployment name as a model below."
        )
    return base_url


def custom_provider(raw: dict | None, *, existing: dict | None = None, created_by: str | None = None) -> dict:
    """Validate one submitted custom provider into its stored shape.
    Raises ValueError with a message meant for the form. `existing` is the
    stored document being edited - its ID and creation stamp are kept, so
    renaming a provider does not orphan the cache entries, policies and
    savings history keyed by that ID."""
    raw = raw or {}
    preset = CUSTOM_PRESET_BY_ID.get(str(raw.get("preset") or (existing or {}).get("preset") or ""))

    label = str(raw.get("label") or "").strip()[:60]
    if not label:
        raise ValueError("Give the provider a name")
    vendor = str(raw.get("vendor") or (preset or {}).get("vendor") or "Custom").strip()[:60] or "Custom"

    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
    if "<" in base_url or ">" in base_url:
        raise ValueError("Replace the <placeholder> in the base URL with your own account or host")
    if not re.fullmatch(r"https?://[^\s/?#]+[^\s?#]*", base_url) or len(base_url) > 500:
        raise ValueError("Base URL must be an http(s) URL, e.g. https://llm.internal:8000/v1")
    base_url = _azure_base_url(base_url)

    models, seen = [], set()
    for m in raw.get("models") or []:
        if isinstance(m, str):
            m = {"id": m}
        model_id = str((m or {}).get("id") or "").strip()
        if not model_id:
            continue
        if not _CUSTOM_MODEL_ID.fullmatch(model_id):
            raise ValueError(f"'{model_id[:60]}' is not a usable model ID (letters, digits and _ . : / @ + - only)")
        if _EMBEDDING_MODEL_ID.search(model_id):
            raise ValueError(
                f"'{model_id[:60]}' looks like an embedding model. An LLM provider needs chat models - "
                "embedding models are added on the Knowledge Base page, when creating a knowledge set."
            )
        if model_id in seen:
            continue
        seen.add(model_id)
        models.append({
            "id": model_id,
            "input_usd_per_1m": _price(m.get("input_usd_per_1m")),
            "output_usd_per_1m": _price(m.get("output_usd_per_1m")),
        })
    if not models:
        raise ValueError("Add at least one model - the name the endpoint expects in its 'model' field")
    if len(models) > MAX_CUSTOM_MODELS:
        raise ValueError(f"A provider can list at most {MAX_CUSTOM_MODELS} models")

    default_model = str(raw.get("default_model") or "").strip()
    if default_model not in seen:
        default_model = models[0]["id"]

    max_tokens_param = raw.get("max_tokens_param") or (preset or {}).get("max_tokens_param") or "max_tokens"
    if max_tokens_param not in MAX_TOKENS_PARAMS:
        max_tokens_param = "max_tokens"
    auth_header = raw.get("auth_header") or (existing or {}).get("auth_header") \
        or (preset or {}).get("auth_header") or "bearer"
    if auth_header not in AUTH_HEADERS:
        auth_header = "bearer"

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if existing:
        provider_id = existing["id"]
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "provider"
        provider_id = CUSTOM_ID_PREFIX + slug
    return {
        "id": provider_id,
        "label": label,
        "vendor": vendor,
        "preset": preset["id"] if preset else None,
        "base_url": base_url,
        "docs_url": (preset or {}).get("docs_url", ""),
        "models": models,
        "default_model": default_model,
        "max_tokens_param": max_tokens_param,
        "auth_header": auth_header,
        "custom": True,
        "created_by": (existing or {}).get("created_by") or created_by,
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }


def register_custom(doc: dict, api_key: str | None = None):
    """Make one validated custom provider selectable. `api_key` None keeps
    whatever key is already held; "" clears it."""
    pid = doc["id"]
    CUSTOM[pid] = doc
    for key in [k for k in CUSTOM_PRICING if k[0] == pid]:
        del CUSTOM_PRICING[key]
    for m in doc["models"]:
        CUSTOM_PRICING[(pid, m["id"])] = (m["input_usd_per_1m"], m["output_usd_per_1m"])
    if api_key is not None:
        if api_key.strip():
            CUSTOM_SECRETS[pid] = api_key.strip()
        else:
            CUSTOM_SECRETS.pop(pid, None)
    PROVIDERS[pid] = {
        "id": pid,
        "label": doc["label"],
        "vendor": doc["vendor"],
        "env_key": "",
        "endpoint": doc["base_url"] + "/chat/completions",
        "docs_url": doc.get("docs_url") or "",
        "models": [m["id"] for m in doc["models"]],
        "default_model": doc["default_model"],
        "max_tokens_param": doc.get("max_tokens_param") or "max_tokens",
        "auth_header": doc.get("auth_header") or "bearer",
        "custom": True,
    }


def unregister_custom(provider_id: str):
    if provider_id in CUSTOM:
        CUSTOM.pop(provider_id, None)
        CUSTOM_SECRETS.pop(provider_id, None)
        PROVIDERS.pop(provider_id, None)
        for key in [k for k in CUSTOM_PRICING if k[0] == provider_id]:
            del CUSTOM_PRICING[key]


def set_custom(docs: dict, secrets: dict | None = None):
    """Replace every custom provider at once (startup load)."""
    for pid in list(CUSTOM):
        unregister_custom(pid)
    for pid, doc in (docs or {}).items():
        register_custom(doc, (secrets or {}).get(pid, ""))


def public_custom(doc: dict) -> dict:
    """A custom provider as the dashboard sees it - everything except the key."""
    return dict(doc, has_api_key=doc["id"] in CUSTOM_SECRETS)


def is_configured(provider: str, api_keys: dict) -> bool:
    """Whether a miss on this provider reaches a real model: a built-in one
    needs its key, a custom one only exists once it has an endpoint."""
    if (PROVIDERS.get(provider) or {}).get("custom"):
        return True
    return bool((api_keys.get(provider) or "").strip())


CACHE_SCOPES = ("global", "per_role", "per_subject")
EVICTION_POLICIES = ("lru", "lfu", "fifo")

# Every field here is surfaced on the LLM Caching setup page. Anything the
# user can change about *when a cached answer stops being usable* is in this
# dict and nowhere else.
DEFAULT_CACHE_CONFIG: dict = {
    "enabled": True,
    # -- provider selection ------------------------------------------------
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "fallback_provider": None,
    "max_output_tokens": 1024,
    "temperature": 0.0,
    # -- matching ----------------------------------------------------------
    "semantic_enabled": True,
    "similarity_threshold": 0.94,
    "semantic_candidates": 20,
    # -- invalidation ------------------------------------------------------
    "ttl_seconds": 3600,
    "stale_while_revalidate_seconds": 0,
    "max_entries": 5000,
    "eviction_policy": "lru",
    "max_reuse_hits": 0,                     # 0 = unlimited reuse
    "invalidate_on_model_change": True,
    "invalidate_on_config_change": True,
    "invalidate_on_catalog_change": False,
    "cache_scope": "global",
    "namespace": "default",
    "bypass_patterns": [
        r"\b(today|right now|current time|as of now|latest)\b",
    ],
    "no_cache_roles": [],
    "sweep_interval_minutes": 5,
}


def normalize_config(raw: dict | None) -> dict:
    """Merge a stored/submitted config over the defaults and coerce every
    field to a sane type and range. The UI is not the validator - this is."""
    cfg = dict(DEFAULT_CACHE_CONFIG)
    for k, v in (raw or {}).items():
        if k in cfg:
            cfg[k] = v

    provider = cfg.get("provider")
    if provider not in PROVIDERS:
        provider = DEFAULT_CACHE_CONFIG["provider"]
    cfg["provider"] = provider

    if cfg.get("model") not in PROVIDERS[provider]["models"]:
        cfg["model"] = PROVIDERS[provider]["default_model"]

    fb = cfg.get("fallback_provider")
    cfg["fallback_provider"] = fb if fb in PROVIDERS and fb != provider else None

    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["semantic_enabled"] = bool(cfg.get("semantic_enabled", True))
    cfg["invalidate_on_model_change"] = bool(cfg.get("invalidate_on_model_change", True))
    cfg["invalidate_on_config_change"] = bool(cfg.get("invalidate_on_config_change", True))
    cfg["invalidate_on_catalog_change"] = bool(cfg.get("invalidate_on_catalog_change", False))

    cfg["similarity_threshold"] = _clamp(float(cfg.get("similarity_threshold", 0.94)), 0.50, 0.9999)
    cfg["semantic_candidates"] = int(_clamp(int(cfg.get("semantic_candidates", 20) or 20), 1, 200))
    cfg["ttl_seconds"] = int(_clamp(int(cfg.get("ttl_seconds", 3600) or 0), 0, 60 * 60 * 24 * 90))
    cfg["stale_while_revalidate_seconds"] = int(
        _clamp(int(cfg.get("stale_while_revalidate_seconds", 0) or 0), 0, 60 * 60 * 24)
    )
    cfg["max_entries"] = int(_clamp(int(cfg.get("max_entries", 5000) or 0), 0, 1_000_000))
    cfg["max_reuse_hits"] = int(_clamp(int(cfg.get("max_reuse_hits", 0) or 0), 0, 1_000_000))
    cfg["max_output_tokens"] = int(_clamp(int(cfg.get("max_output_tokens", 1024) or 1024), 16, 32000))
    cfg["temperature"] = _clamp(float(cfg.get("temperature", 0.0) or 0.0), 0.0, 2.0)
    cfg["sweep_interval_minutes"] = int(_clamp(int(cfg.get("sweep_interval_minutes", 5) or 5), 1, 1440))

    if cfg.get("eviction_policy") not in EVICTION_POLICIES:
        cfg["eviction_policy"] = "lru"
    if cfg.get("cache_scope") not in CACHE_SCOPES:
        cfg["cache_scope"] = "global"

    ns = str(cfg.get("namespace") or "default").strip() or "default"
    cfg["namespace"] = re.sub(r"[^a-zA-Z0-9_.:-]", "-", ns)[:64]

    patterns = cfg.get("bypass_patterns") or []
    cfg["bypass_patterns"] = [str(p) for p in patterns if str(p).strip()][:50]
    cfg["no_cache_roles"] = [str(r) for r in (cfg.get("no_cache_roles") or [])][:50]
    return cfg


def _clamp(value, low, high):
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Per-role / per-agent policy overrides
# ---------------------------------------------------------------------------
# The global config above is the default every caller gets. A policy
# override narrows that for one role or one agent identity: a different
# provider/model, a different TTL, caching off entirely. It carries only the
# fields it sets - everything else keeps following the global policy, so a
# later change to the global TTL still reaches an agent whose override only
# picked a model.
#
# Resolution order: a policy matched on the caller's agent identity wins
# over one matched on its role, which wins over the global default. Within
# the same match type the first enabled policy in stored order applies.
# Operational knobs (max_entries, eviction, sweep interval, invalidation
# rules, bypass patterns) are deliberately not overridable: those describe
# the one cache, not one caller's view of it.
POLICY_MATCH_TYPES = ("role", "agent")
POLICY_OVERRIDE_FIELDS = (
    "enabled", "provider", "model", "max_output_tokens", "temperature",
    "semantic_enabled", "similarity_threshold", "ttl_seconds",
    "max_reuse_hits", "cache_scope", "namespace",
)


def _policy_id(name: str, match_type: str, match_value: str) -> str:
    material = f"{match_type}:{match_value}:{name}:{time.time_ns()}"
    return "llmpolicy-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def normalize_policy(raw: dict | None, base: dict | None = None) -> dict | None:
    """Coerce one submitted override into its stored shape, or None when
    it has nothing to match on. `config` keeps only overridable fields and
    only the ones actually supplied; the values are validated by running
    them through normalize_config over `base` (the global policy), so a
    bad model name resolves the same way it would at the top level."""
    raw = raw or {}
    match = raw.get("match") or {}
    match_type = str(match.get("type") or "").strip()
    match_value = str(match.get("value") or "").strip()[:200]
    if match_type not in POLICY_MATCH_TYPES or not match_value:
        return None

    supplied = {k: v for k, v in (raw.get("config") or {}).items() if k in POLICY_OVERRIDE_FIELDS}
    # A model supplied without a provider is ambiguous; a provider supplied
    # without a model means "that provider's default".
    if "model" in supplied and "provider" not in supplied:
        supplied.pop("model")
    merged = normalize_config({**(base or DEFAULT_CACHE_CONFIG), **supplied})
    config = {k: merged[k] for k in supplied}

    name = str(raw.get("name") or f"{match_type} {match_value}").strip()[:120]
    return {
        "policy_id": str(raw.get("policy_id") or "").strip() or _policy_id(name, match_type, match_value),
        "name": name,
        "match": {"type": match_type, "value": match_value},
        "enabled": bool(raw.get("enabled", True)),
        "config": config,
    }


def normalize_policies(raw: list | None, base: dict | None = None) -> list[dict]:
    out, seen = [], set()
    for item in raw or []:
        pol = normalize_policy(item, base)
        if pol and pol["policy_id"] not in seen:
            seen.add(pol["policy_id"])
            out.append(pol)
    return out[:200]


def resolve_policy(global_cfg: dict, policies: list[dict], role: str | None, agent_id: str | None):
    """The effective cache config for one caller, and the override that
    produced it (None when the global policy applies as-is)."""
    chosen = None
    for match_type, value in (("agent", agent_id), ("role", role)):
        if not value:
            continue
        for pol in policies or []:
            if pol.get("enabled", True) and pol["match"]["type"] == match_type and pol["match"]["value"] == value:
                chosen = pol
                break
        if chosen:
            break
    if not chosen:
        return dict(global_cfg), None
    return normalize_config({**global_cfg, **(chosen.get("config") or {})}), chosen


def policy_summary(policy: dict | None) -> dict | None:
    """What a caller is told about the override that shaped its call."""
    if not policy:
        return None
    return {"policy_id": policy["policy_id"], "name": policy["name"], "match": dict(policy["match"])}


def config_fingerprint(cfg: dict) -> str:
    """Identity of the settings that affect what a cached answer *means*.

    Deliberately excludes two groups. Operational knobs (max_entries, sweep
    interval, eviction policy) are out because changing how much you keep
    should not throw away what you already have. Provider and model are out
    because `invalidate_on_model_change` already governs those, and folding
    them in here would let the config rule delete entries the model rule
    deliberately keeps (per-request overrides). What is left is the set of
    settings that change what an answer means for *every* entry: namespace,
    scope, match strictness and generation parameters."""
    material = {
        k: cfg.get(k)
        for k in (
            "namespace", "cache_scope",
            "similarity_threshold", "max_output_tokens", "temperature",
        )
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Keys, tokens, pricing
# ---------------------------------------------------------------------------
def normalize_prompt(prompt: str) -> str:
    """Collapse the differences that shouldn't cost a second model call:
    surrounding whitespace, internal runs of whitespace, and case."""
    return re.sub(r"\s+", " ", (prompt or "").strip()).lower()


def scope_key(cfg: dict, role: str | None, subject: str | None) -> str:
    """Which callers may share a cached answer. `global` is the cheapest and
    the default; `per_role`/`per_subject` trade hit rate for isolation when
    prompts can carry data one role shouldn't see reflected back at another."""
    mode = cfg.get("cache_scope", "global")
    if mode == "per_role":
        return f"role:{role or 'anonymous'}"
    if mode == "per_subject":
        return f"subject:{subject or 'anonymous'}"
    return "global"


def entry_id(cfg: dict, provider: str, model: str, prompt: str, params: dict, scope: str) -> str:
    material = json.dumps(
        {
            "provider": provider,
            "model": model,
            "namespace": cfg.get("namespace", "default"),
            "scope": scope,
            "prompt": normalize_prompt(prompt),
            "params": {k: params.get(k) for k in sorted(params or {})},
        },
        sort_keys=True,
    )
    return "llmcache::" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def estimate_tokens(text: str) -> int:
    """~4 characters per token. Only used when a provider doesn't report
    usage (i.e. offline stub mode); real responses carry real counts."""
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def price_for(model: str, provider: str | None = None) -> tuple[float, float]:
    """(input, output) USD per 1M tokens. A custom provider's own price for
    the model wins - including an explicit 0 for a self-hosted one."""
    custom = CUSTOM_PRICING.get((provider or "", model))
    if custom is not None:
        return custom
    return PRICING.get(model, (1.00, 5.00))


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int, provider: str | None = None) -> float:
    in_price, out_price = price_for(model, provider)
    return round((prompt_tokens * in_price + completion_tokens * out_price) / 1_000_000, 6)


def is_bypassed(prompt: str, cfg: dict, role: str | None) -> str | None:
    """Return a human-readable reason this prompt must never be served from
    (or written to) the cache, or None if it's cacheable."""
    if role and role in (cfg.get("no_cache_roles") or []):
        return f"role '{role}' is on the no-cache list"
    for pattern in cfg.get("bypass_patterns") or []:
        try:
            if re.search(pattern, prompt or "", re.IGNORECASE):
                return f"prompt matched bypass pattern /{pattern}/"
        except re.error:
            logger.warning("Ignoring invalid bypass pattern: %s", pattern)
    return None


# ---------------------------------------------------------------------------
# Invalidation policy
# ---------------------------------------------------------------------------
def _parse_ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


# Public alias - main.py needs the same timestamp parsing to compute an
# entry's age and its remaining TTL.
parse_timestamp = _parse_ts


def evaluate_entry(
    entry: dict,
    cfg: dict,
    *,
    now: float | None = None,
    config_version: str | None = None,
    catalog_version: str | None = None,
) -> tuple[str, str | None]:
    """Classify a stored entry as 'fresh', 'stale' (usable under
    stale-while-revalidate) or 'invalid'.

    Returns (state, reason). This is the single implementation of every
    user-configurable invalidation rule; the read path, the background
    sweeper and the Cache Entries table all call it, so what the UI shows
    and what the gateway does can never drift apart."""
    now = now or time.time()
    age = now - _parse_ts(entry.get("created_at"))

    # Entries written for an explicitly overridden provider/model are exempt:
    # the caller asked for that model on purpose, so "the *selected* model
    # changed" is not a statement about them.
    if (
        cfg.get("invalidate_on_model_change")
        and not entry.get("override")
        and entry.get("model") != cfg.get("model")
    ):
        return "invalid", f"selected model changed to '{cfg.get('model')}'"
    if cfg.get("invalidate_on_config_change") and config_version and entry.get("config_version") != config_version:
        return "invalid", "cache configuration changed"
    if cfg.get("invalidate_on_catalog_change") and catalog_version and entry.get("catalog_version") != catalog_version:
        return "invalid", "tool catalog changed since this answer was produced"

    max_reuse = int(cfg.get("max_reuse_hits") or 0)
    if max_reuse and int(entry.get("hit_count") or 0) >= max_reuse:
        return "invalid", f"reuse limit of {max_reuse} hit(s) reached"

    ttl = int(cfg.get("ttl_seconds") or 0)
    if ttl:
        if age > ttl + int(cfg.get("stale_while_revalidate_seconds") or 0):
            return "invalid", f"older than TTL ({ttl}s)"
        if age > ttl:
            return "stale", f"past TTL ({ttl}s), inside the stale-while-revalidate window"

    return "fresh", None


def eviction_sort_key(entry: dict):
    """Ordering used when `max_entries` is exceeded - lowest sorts first and
    is evicted first."""
    return (
        int(entry.get("hit_count") or 0),
        _parse_ts(entry.get("last_hit_at") or entry.get("created_at")),
    )


def select_evictions(entries: list[dict], cfg: dict) -> list[str]:
    max_entries = int(cfg.get("max_entries") or 0)
    if not max_entries or len(entries) <= max_entries:
        return []
    policy = cfg.get("eviction_policy", "lru")
    if policy == "lfu":
        ordered = sorted(entries, key=lambda e: (int(e.get("hit_count") or 0), _parse_ts(e.get("created_at"))))
    elif policy == "fifo":
        ordered = sorted(entries, key=lambda e: _parse_ts(e.get("created_at")))
    else:  # lru
        ordered = sorted(entries, key=lambda e: _parse_ts(e.get("last_hit_at") or e.get("created_at")))
    overflow = len(entries) - max_entries
    return [e["entry_id"] for e in ordered[:overflow] if e.get("entry_id")]


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------
class ProviderResult(dict):
    """{text, prompt_tokens, completion_tokens, stub, provider, model}"""


def _stub_completion(provider: str, model: str, prompt: str, cfg: dict) -> ProviderResult:
    """Deterministic offline answer, used when no API key is configured for
    the selected provider. Deterministic matters: the same prompt yields the
    same stub, so cache hit/miss behaviour is honest even with no key."""
    digest = hashlib.sha256(normalize_prompt(prompt).encode("utf-8")).hexdigest()[:12]
    label = PROVIDERS[provider]["label"]
    text = (
        f"[offline stub - no {PROVIDERS[provider]['env_key']} configured]\n\n"
        f"{label} ({model}) would answer the following prompt here:\n\n"
        f"\"{(prompt or '').strip()[:400]}\"\n\n"
        f"Deterministic response id {digest}. Set {PROVIDERS[provider]['env_key']} in .env "
        f"to proxy this call to the real provider - caching, savings accounting and "
        f"invalidation behave identically either way."
    )
    return ProviderResult(
        text=text,
        prompt_tokens=estimate_tokens(prompt),
        completion_tokens=estimate_tokens(text),
        stub=True,
        provider=provider,
        model=model,
    )


def call_provider(provider: str, model: str, prompt: str, cfg: dict, api_keys: dict, timeout: int = 60) -> ProviderResult:
    """Single outbound call to the selected provider. Falls back to the
    offline stub when the provider has no key configured; a configured key
    that *fails* raises, because silently stubbing a real outage would put
    fabricated text into the cache."""
    spec = PROVIDERS.get(provider)
    if not spec:
        raise ValueError(f"Unknown provider '{provider}'")
    max_tokens = int(cfg.get("max_output_tokens", 1024))
    temperature = float(cfg.get("temperature", 0.0))

    if spec.get("custom"):
        return _openai_compatible(
            provider, spec, model, prompt, max_tokens, temperature, CUSTOM_SECRETS.get(provider, ""), timeout,
        )

    key = (api_keys.get(provider) or "").strip()
    if not key:
        return _stub_completion(provider, model, prompt, cfg)

    if provider == "anthropic":
        resp = requests.post(
            spec["endpoint"],
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        text = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
        usage = body.get("usage") or {}
        return ProviderResult(
            text=text,
            prompt_tokens=int(usage.get("input_tokens") or estimate_tokens(prompt)),
            completion_tokens=int(usage.get("output_tokens") or estimate_tokens(text)),
            stub=False,
            provider=provider,
            model=model,
        )

    if provider == "openai":
        resp = requests.post(
            spec["endpoint"],
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_completion_tokens": max_tokens,
                "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        choices = body.get("choices") or [{}]
        text = ((choices[0].get("message") or {}).get("content")) or ""
        usage = body.get("usage") or {}
        return ProviderResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or estimate_tokens(prompt)),
            completion_tokens=int(usage.get("completion_tokens") or estimate_tokens(text)),
            stub=False,
            provider=provider,
            model=model,
        )

    if provider == "google":
        url = f"{spec['endpoint']}/{model}:generateContent"
        resp = requests.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        candidates = body.get("candidates") or [{}]
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts)
        usage = body.get("usageMetadata") or {}
        return ProviderResult(
            text=text,
            prompt_tokens=int(usage.get("promptTokenCount") or estimate_tokens(prompt)),
            completion_tokens=int(usage.get("candidatesTokenCount") or estimate_tokens(text)),
            stub=False,
            provider=provider,
            model=model,
        )

    if provider == "bedrock":
        return _bedrock_converse(model, prompt, max_tokens, temperature, timeout)

    raise ValueError(f"No adapter implemented for provider '{provider}'")


def _openai_compatible(
    provider: str, spec: dict, model: str, prompt: str, max_tokens: int, temperature: float,
    api_key: str, timeout: int,
) -> ProviderResult:
    """Adapter for custom providers: one POST to <base_url>/chat/completions.
    The key is optional (a self-hosted server often has none). Redirects are
    not followed - a 30x from an LLM endpoint is a misconfigured URL, and
    following it would replay the prompt somewhere the admin did not enter."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        if spec.get("auth_header") == "api-key":
            headers["api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        spec.get("max_tokens_param") or "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(spec["endpoint"], headers=headers, json=payload, timeout=timeout, allow_redirects=False)
    if resp.status_code == 400 and "temperature" in (resp.text or "").lower():
        # Reasoning models (GPT-5 family, o-series and their hosted
        # equivalents) accept only their default temperature. Send the call
        # once more without the field rather than fail every request.
        payload.pop("temperature")
        resp = requests.post(spec["endpoint"], headers=headers, json=payload, timeout=timeout, allow_redirects=False)
    if resp.status_code >= 300:
        detail = re.sub(r"\s+", " ", resp.text or "").strip()[:300]
        raise RuntimeError(f"HTTP {resp.status_code} from {spec['endpoint']}" + (f": {detail}" if detail else ""))
    try:
        body = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"{spec['endpoint']} did not return JSON - is the base URL right?") from exc
    choices = body.get("choices") if isinstance(body, dict) else None
    if not choices:
        raise RuntimeError(f"{spec['endpoint']} returned no choices - is this an OpenAI-compatible chat endpoint?")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):  # content-part arrays, as some servers return
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = content or ""
    usage = body.get("usage") or {}
    return ProviderResult(
        text=text,
        prompt_tokens=int(usage.get("prompt_tokens") or estimate_tokens(prompt)),
        completion_tokens=int(usage.get("completion_tokens") or estimate_tokens(text)),
        stub=False,
        provider=provider,
        model=model,
    )


def test_custom(doc: dict, api_key: str = "", timeout: int = 20) -> dict:
    """One small real call to a custom provider's default model, without
    registering it - what the form's "Test connection" button runs. Returns
    {ok, model, latency_ms, reply | error}; never raises."""
    spec = {
        "endpoint": doc["base_url"] + "/chat/completions",
        "max_tokens_param": doc.get("max_tokens_param") or "max_tokens",
        "auth_header": doc.get("auth_header") or "bearer",
    }
    model = doc["default_model"]
    started = time.time()
    try:
        result = _openai_compatible(
            doc["id"], spec, model, "Reply with the single word: OK", 64, 0.0, (api_key or "").strip(), timeout,
        )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:500]
        if error.startswith("HTTP 404"):
            error += (
                f" - Two things cause a 404 here: the base URL is not the root that /chat/completions is "
                f"appended to, or '{model}' is not a model (on Azure: a deployment) that exists on this endpoint."
            )
        elif error.startswith(("HTTP 401", "HTTP 403")):
            error += " - The endpoint rejected the API key, or the key is being sent in the wrong header."
        return {"ok": False, "model": model, "latency_ms": int((time.time() - started) * 1000),
                "error": error[:800]}
    return {
        "ok": True, "model": model, "latency_ms": int((time.time() - started) * 1000),
        "reply": (result["text"] or "").strip()[:200],
        "prompt_tokens": result["prompt_tokens"], "completion_tokens": result["completion_tokens"],
    }


def public_presets() -> list[dict]:
    return [dict(p, auth_header=p.get("auth_header") or "bearer") for p in CUSTOM_PRESETS]


def bedrock_region() -> str:
    return (
        os.getenv("AWS_BEDROCK_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "us-east-1"
    ).strip()


def _bedrock_converse(model: str, prompt: str, max_tokens: int, temperature: float, timeout: int) -> ProviderResult:
    """Bedrock goes through boto3 rather than `requests` because every call
    has to be SigV4-signed (or carry a Bedrock API key, which botocore also
    handles). The Converse API gives one request/response shape for every
    model family on Bedrock, so this adapter needs no per-model branches."""
    import boto3
    from botocore.config import Config as BotoConfig

    # Deployments pass every provider variable through, so an unused Bedrock
    # API key arrives as an empty string. botocore treats a variable that is
    # set at all as a token and sends it instead of signing with the pod /
    # instance role or access keys, and Bedrock answers 403.
    if not (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "x").strip():
        os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)

    client = boto3.client(
        "bedrock-runtime",
        region_name=bedrock_region(),
        config=BotoConfig(read_timeout=timeout, connect_timeout=min(timeout, 10), retries={"max_attempts": 2}),
    )
    body = client.converse(
        modelId=model,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    content = ((body.get("output") or {}).get("message") or {}).get("content") or []
    text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    usage = body.get("usage") or {}
    return ProviderResult(
        text=text,
        prompt_tokens=int(usage.get("inputTokens") or estimate_tokens(prompt)),
        completion_tokens=int(usage.get("outputTokens") or estimate_tokens(text)),
        stub=False,
        provider="bedrock",
        model=model,
    )


def provider_catalog(api_keys: dict) -> list[dict]:
    """Provider list for the setup page, annotated with which ones have a
    key configured (never the key itself)."""
    out = []
    for pid, spec in PROVIDERS.items():
        out.append({
            "id": pid,
            "label": spec["label"],
            "vendor": spec["vendor"],
            "env_key": spec["env_key"],
            "docs_url": spec["docs_url"],
            "default_model": spec["default_model"],
            "api_key_configured": is_configured(pid, api_keys),
            "custom": bool(spec.get("custom")),
            "base_url": CUSTOM[pid]["base_url"] if pid in CUSTOM else None,
            "models": [
                {
                    "id": m,
                    "input_usd_per_1m": price_for(m, pid)[0],
                    "output_usd_per_1m": price_for(m, pid)[1],
                }
                for m in spec["models"]
            ],
        })
    return out


# ---------------------------------------------------------------------------
# Savings dashboard
# ---------------------------------------------------------------------------
HIT_OUTCOMES = ("hit_exact", "hit_semantic")


def build_hourly_series(hourly_events: list[dict], *, buckets: int = 12) -> list[dict]:
    """Bucket a genuinely time-bounded RAW event list into hourly hit/miss
    counts for the trend chart. Separated out of build_dashboard so a
    Couchbase-aggregated caller wouldn't need to pull a raw event list at
    all - though the live dashboard route has since moved past even that:
    see build_dashboard_aggregate() / CouchbaseStore.
    llm_dashboard_aggregate_since, which get the trend chart (and the
    summary/model-breakdown panels) from one GROUP BY query instead of a
    raw fetch, however generous its LIMIT, which silently truncates this
    chart's window under real throughput. Kept here as the reference
    implementation for a plain, in-hand event list (also still used by
    build_dashboard)."""
    import datetime as _dt

    now = _dt.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    hour_keys = [(now - _dt.timedelta(hours=i)) for i in range(buckets - 1, -1, -1)]
    hourly = {h.strftime("%Y-%m-%dT%H:00:00Z"): {"hits": 0, "misses": 0} for h in hour_keys}
    for e in hourly_events:
        ts = e.get("timestamp") or ""
        if len(ts) < 13:
            continue
        key = ts[:13] + ":00:00Z"
        if key not in hourly:
            continue
        if e.get("outcome") in HIT_OUTCOMES:
            hourly[key]["hits"] += 1
        elif e.get("outcome") == "miss":
            hourly[key]["misses"] += 1

    return [
        {
            "hour": h.strftime("%H:00"),
            "timestamp": h.strftime("%Y-%m-%dT%H:00:00Z"),
            "hits": hourly[h.strftime("%Y-%m-%dT%H:00:00Z")]["hits"],
            "misses": hourly[h.strftime("%Y-%m-%dT%H:00:00Z")]["misses"],
        }
        for h in hour_keys
    ]


def build_dashboard_aggregate(
    rows: list[dict], *, trend_hours: int = 12, lifetime_by_model: dict | None = None,
    usage_rows: list[dict] | None = None,
) -> dict:
    """Build the ENTIRE live dashboard payload - 'summary' (donut + stat
    cards), 'model_breakdown' ('Savings by provider & model'), and
    'hourly' (the trend chart) - from ONE set of pre-aggregated Couchbase
    GROUP BY rows keyed by (hour_key, provider, model, outcome). See
    CouchbaseStore.llm_dashboard_aggregate_since.

    This replaces what used to be two functions (build_dashboard_summary
    + build_hourly_series_from_aggregate) each consuming its own
    full-window query - a genuinely time-bounded window (e.g. 24h) can be
    far too many documents to fetch into Python one by one under real
    traffic, and running three separate full-window scans for summary,
    model breakdown, and hourly trend was needlessly rescanning the same
    data three times. One query, reduced once here, removes that
    redundancy - only the small, already-reduced rows cross into this
    process.

    `hour_key` is expected to be the first 13 characters of the event
    timestamp ("%Y-%m-%dT%H:%M:%SZ" -> "%Y-%m-%dT%H"); the hourly trend
    only uses the trailing `trend_hours` of them; the summary and
    model_breakdown sum across every row regardless of hour, i.e. the
    full window the query was run over.

    `lifetime_by_model` (see CouchbaseStore.get_lifetime_stats), when
    given, overrides each model_breakdown row's cost_saved_usd/
    cost_spent_usd with the all-time total for that (provider, model) -
    the rest of the row (requests, hit rate, tokens saved) stays
    window-scoped. Keyed the same way CouchbaseStore._model_key builds
    it: "<provider>::<model>"."""
    import datetime as _dt

    outcome_totals: dict[str, dict] = {}
    model_totals: dict[tuple, dict] = {}

    now = _dt.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    hour_keys = [(now - _dt.timedelta(hours=i)) for i in range(trend_hours - 1, -1, -1)]
    hourly = {h.strftime("%Y-%m-%dT%H"): {"hits": 0, "misses": 0} for h in hour_keys}

    for r in rows:
        outcome = r.get("outcome")
        n = int(r.get("n") or 0)
        tokens_saved = int(r.get("tokens_saved") or 0)
        total_tokens = int(r.get("total_tokens") or 0)
        cost_saved_usd = float(r.get("cost_saved_usd") or 0.0)
        cost_usd = float(r.get("cost_usd") or 0.0)
        latency_saved_ms = int(r.get("latency_saved_ms") or 0)
        latency_ms_sum = int(r.get("latency_ms_sum") or 0)

        ot = outcome_totals.setdefault(outcome, {
            "n": 0, "tokens_saved": 0, "cost_saved_usd": 0.0, "total_tokens": 0,
            "cost_usd": 0.0, "latency_saved_ms": 0, "latency_ms_sum": 0,
        })
        ot["n"] += n
        ot["tokens_saved"] += tokens_saved
        ot["cost_saved_usd"] += cost_saved_usd
        ot["total_tokens"] += total_tokens
        ot["cost_usd"] += cost_usd
        ot["latency_saved_ms"] += latency_saved_ms
        ot["latency_ms_sum"] += latency_ms_sum

        if outcome in HIT_OUTCOMES + ("miss",):
            mkey = (r.get("provider") or "unknown", r.get("model") or "unknown")
            mt = model_totals.setdefault(mkey, {
                "requests": 0, "hits": 0, "misses": 0,
                "tokens_saved": 0, "tokens_spent": 0,
                "cost_saved_usd": 0.0, "cost_spent_usd": 0.0,
            })
            mt["requests"] += n
            if outcome in HIT_OUTCOMES:
                mt["hits"] += n
                mt["tokens_saved"] += tokens_saved
                mt["cost_saved_usd"] += cost_saved_usd
            else:
                mt["misses"] += n
                mt["tokens_spent"] += total_tokens
                mt["cost_spent_usd"] += cost_usd

        hour_key = r.get("hour_key") or ""
        if hour_key in hourly:
            if outcome in HIT_OUTCOMES:
                hourly[hour_key]["hits"] += n
            elif outcome == "miss":
                hourly[hour_key]["misses"] += n

    def _n(outcome):
        return int((outcome_totals.get(outcome) or {}).get("n") or 0)

    def _sum(outcome, field):
        return (outcome_totals.get(outcome) or {}).get(field) or 0

    exact_hits = _n("hit_exact")
    semantic_hits = _n("hit_semantic")
    hits = exact_hits + semantic_hits
    misses = _n("miss")
    bypasses = _n("bypass")
    errors = _n("error")
    cacheable = hits + misses
    requests = sum(v["n"] for v in outcome_totals.values())

    tokens_saved = int(_sum("hit_exact", "tokens_saved")) + int(_sum("hit_semantic", "tokens_saved"))
    cost_saved = float(_sum("hit_exact", "cost_saved_usd")) + float(_sum("hit_semantic", "cost_saved_usd"))
    latency_saved = int(_sum("hit_exact", "latency_saved_ms")) + int(_sum("hit_semantic", "latency_saved_ms"))
    hit_latency_sum = int(_sum("hit_exact", "latency_ms_sum")) + int(_sum("hit_semantic", "latency_ms_sum"))
    tokens_spent = int(_sum("miss", "total_tokens"))
    cost_spent = float(_sum("miss", "cost_usd"))
    miss_latency_sum = int(_sum("miss", "latency_ms_sum"))

    summary = {
        "requests": requests,
        "cacheable_requests": cacheable,
        "hits": hits,
        "exact_hits": exact_hits,
        "semantic_hits": semantic_hits,
        "misses": misses,
        "bypasses": bypasses,
        "errors": errors,
        "hit_rate_pct": round((hits / cacheable) * 100, 1) if cacheable else 0.0,
        "tokens_saved": tokens_saved,
        "tokens_spent": tokens_spent,
        "cost_saved_usd": round(cost_saved, 4),
        "cost_spent_usd": round(cost_spent, 4),
        "latency_saved_ms": latency_saved,
        "avg_hit_latency_ms": round(hit_latency_sum / hits) if hits else 0,
        "avg_miss_latency_ms": round(miss_latency_sum / misses) if misses else 0,
    }

    # Which roles drove each (provider, model) in the window - from
    # CouchbaseStore.llm_usage_by_role_since, a separate GROUP BY so the
    # main aggregate's covering index stays untouched. Agents under
    # different policy overrides reach different models, and this is what
    # shows who is on which.
    used_by: dict[tuple, dict[str, int]] = {}
    for u in usage_rows or []:
        key = (u.get("provider") or "unknown", u.get("model") or "unknown")
        role = u.get("role") or "unknown"
        used_by.setdefault(key, {})[role] = used_by.get(key, {}).get(role, 0) + int(u.get("n") or 0)

    lifetime_by_model = lifetime_by_model or {}
    breakdown = []
    for (provider, model), row in model_totals.items():
        roles = used_by.get((provider, model), {})
        row = dict(
            row, provider=provider, model=model,
            provider_label=PROVIDERS.get(provider, {}).get("label", provider),
            used_by=[
                {"role": r, "requests": n}
                for r, n in sorted(roles.items(), key=lambda kv: kv[1], reverse=True)
            ],
        )
        row["hit_rate_pct"] = round((row["hits"] / row["requests"]) * 100, 1) if row["requests"] else 0.0
        lifetime_row = lifetime_by_model.get(f"{provider}::{model}")
        if lifetime_row:
            row["cost_saved_usd"] = round(float(lifetime_row.get("cost_saved_usd_total") or 0.0), 4)
            row["cost_spent_usd"] = round(float(lifetime_row.get("cost_spent_usd_total") or 0.0), 4)
        else:
            row["cost_saved_usd"] = round(row["cost_saved_usd"], 4)
            row["cost_spent_usd"] = round(row["cost_spent_usd"], 4)
        breakdown.append(row)
    breakdown.sort(key=lambda r: r["tokens_saved"], reverse=True)

    hourly_series = [
        {
            "hour": h.strftime("%H:00"),
            "timestamp": h.strftime("%Y-%m-%dT%H:00:00Z"),
            "hits": hourly[h.strftime("%Y-%m-%dT%H")]["hits"],
            "misses": hourly[h.strftime("%Y-%m-%dT%H")]["misses"],
        }
        for h in hour_keys
    ]

    return {"summary": summary, "model_breakdown": breakdown, "hourly": hourly_series}


def build_dashboard(events: list[dict], *, buckets: int = 12, hourly_events: list[dict] | None = None) -> dict:
    """Aggregate a raw cache event list into the LLM Caching dashboard
    shape. Superseded for the live dashboard route by
    build_dashboard_aggregate() (see main.py / CouchbaseStore.
    llm_dashboard_aggregate_since), which gets everything from one
    Couchbase GROUP BY instead of pulling a potentially huge raw event
    list into Python - kept here as the reference implementation for a
    plain, count-bounded event list."""
    hits = [e for e in events if e.get("outcome") in HIT_OUTCOMES]
    misses = [e for e in events if e.get("outcome") == "miss"]
    bypasses = [e for e in events if e.get("outcome") == "bypass"]
    errors = [e for e in events if e.get("outcome") == "error"]
    cacheable = len(hits) + len(misses)

    tokens_saved = sum(int(e.get("tokens_saved") or 0) for e in hits)
    tokens_spent = sum(int(e.get("total_tokens") or 0) for e in misses)
    cost_saved = sum(float(e.get("cost_saved_usd") or 0.0) for e in hits)
    cost_spent = sum(float(e.get("cost_usd") or 0.0) for e in misses)
    latency_saved = sum(max(0, int(e.get("latency_saved_ms") or 0)) for e in hits)

    def _avg(rows, key):
        vals = [int(r.get(key) or 0) for r in rows]
        return round(sum(vals) / len(vals)) if vals else 0

    hourly = build_hourly_series(hourly_events if hourly_events is not None else events, buckets=buckets)

    by_model: dict[tuple, dict] = {}
    for e in events:
        if e.get("outcome") not in HIT_OUTCOMES + ("miss",):
            continue
        key = (e.get("provider") or "unknown", e.get("model") or "unknown")
        row = by_model.setdefault(key, {
            "provider": key[0],
            "provider_label": PROVIDERS.get(key[0], {}).get("label", key[0]),
            "model": key[1],
            "requests": 0, "hits": 0, "misses": 0,
            "tokens_saved": 0, "tokens_spent": 0,
            "cost_saved_usd": 0.0, "cost_spent_usd": 0.0,
        })
        row["requests"] += 1
        if e.get("outcome") in HIT_OUTCOMES:
            row["hits"] += 1
            row["tokens_saved"] += int(e.get("tokens_saved") or 0)
            row["cost_saved_usd"] += float(e.get("cost_saved_usd") or 0.0)
        else:
            row["misses"] += 1
            row["tokens_spent"] += int(e.get("total_tokens") or 0)
            row["cost_spent_usd"] += float(e.get("cost_usd") or 0.0)

    breakdown = []
    for row in by_model.values():
        row["hit_rate_pct"] = round((row["hits"] / row["requests"]) * 100, 1) if row["requests"] else 0.0
        row["cost_saved_usd"] = round(row["cost_saved_usd"], 4)
        row["cost_spent_usd"] = round(row["cost_spent_usd"], 4)
        breakdown.append(row)
    breakdown.sort(key=lambda r: r["tokens_saved"], reverse=True)

    return {
        "summary": {
            "requests": len(events),
            "cacheable_requests": cacheable,
            "hits": len(hits),
            "exact_hits": sum(1 for e in hits if e.get("outcome") == "hit_exact"),
            "semantic_hits": sum(1 for e in hits if e.get("outcome") == "hit_semantic"),
            "misses": len(misses),
            "bypasses": len(bypasses),
            "errors": len(errors),
            "hit_rate_pct": round((len(hits) / cacheable) * 100, 1) if cacheable else 0.0,
            "tokens_saved": tokens_saved,
            "tokens_spent": tokens_spent,
            "cost_saved_usd": round(cost_saved, 4),
            "cost_spent_usd": round(cost_spent, 4),
            "latency_saved_ms": latency_saved,
            "avg_hit_latency_ms": _avg(hits, "latency_ms"),
            "avg_miss_latency_ms": _avg(misses, "latency_ms"),
        },
        "hourly": hourly,
        "model_breakdown": breakdown,
    }
