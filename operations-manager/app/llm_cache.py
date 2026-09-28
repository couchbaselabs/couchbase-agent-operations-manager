"""
LLM response caching for agents.

Agents that route their model calls through the operations manager get the
same treatment their *tool* calls already get: one governed choke point that
sees every request, applies policy, and writes an auditable record. The
difference is what the choke point buys you here - a cache.

Three things live in this module:

  1. The provider catalog (Claude / ChatGPT / Gemini), including per-model
     token pricing so "tokens saved" can be reported as money saved. Prices
     are list-price *estimates* and are meant to be edited - see PRICING.
  2. The cache key + invalidation policy engine. Everything a user can
     configure on the LLM Caching setup page is evaluated here, so the
     policy has exactly one implementation.
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
        "models": ["gpt-5", "gpt-5-mini", "gpt-4o"],
        "default_model": "gpt-5-mini",
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
}

# USD per 1,000,000 tokens, (input, output). These are *estimates* used to
# turn "tokens saved" into "dollars saved" on the dashboard - they are not
# billing data. Override them for your own negotiated rates.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-5": (5.00, 25.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4o": (2.50, 10.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.075, 0.30),
}

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


def price_for(model: str) -> tuple[float, float]:
    return PRICING.get(model, (1.00, 5.00))


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    in_price, out_price = price_for(model)
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
    key = (api_keys.get(provider) or "").strip()
    if not key:
        return _stub_completion(provider, model, prompt, cfg)

    max_tokens = int(cfg.get("max_output_tokens", 1024))
    temperature = float(cfg.get("temperature", 0.0))

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

    raise ValueError(f"No adapter implemented for provider '{provider}'")


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
            "api_key_configured": bool((api_keys.get(pid) or "").strip()),
            "models": [
                {
                    "id": m,
                    "input_usd_per_1m": price_for(m)[0],
                    "output_usd_per_1m": price_for(m)[1],
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
    rows: list[dict], *, trend_hours: int = 12, lifetime_by_model: dict | None = None
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

    lifetime_by_model = lifetime_by_model or {}
    breakdown = []
    for (provider, model), row in model_totals.items():
        row = dict(
            row, provider=provider, model=model,
            provider_label=PROVIDERS.get(provider, {}).get("label", provider),
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
