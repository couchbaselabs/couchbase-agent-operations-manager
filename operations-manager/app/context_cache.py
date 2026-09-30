"""
Context caching for agents.

Agents that route their *tool* calls and their *model* calls through the
operations manager already get one governed choke point apiece (see
app/main.py's discover/invoke, and app/llm_cache.py for /v1/llm/complete).
This module adds the same treatment for the third thing an agent spends
time on: fetching context. A "context" here is any slow-to-fetch,
deterministic-per-key value a data source can hand back - a warehouse
query result, a search-API response, a document lookup - that an agent
would otherwise cache for itself against its own database.

The AOM SDK exposes this as `client.context_get(key, ...)` /
`client.context_set(key, value, ...)` (see sdk/aom_sdk/client.py). Any
agent using the SDK for this, not just one demo, shows up here: the same
choke point sees every lookup, decides whether it is still fresh, and
writes an auditable hit/miss record - which is what makes a "Context
Cache" dashboard possible at all, instead of every agent's own private
Couchbase bucket being invisible to the appliance that is supposed to be
governing it.

Two differences from the LLM cache this is modeled on:

  1. No semantic matching. A cached value is opaque JSON the agent
     supplied - there is no prompt to embed, so matching is a single exact
     key (namespace + cache_scope + key -> one deterministic document ID,
     one KV get).
  2. No token/dollar accounting. The unit of "savings" is time: the agent
     reports how long the original fetch took (`source_latency_ms`) when
     it writes an entry, and every later hit's savings is that origin
     latency minus this round trip - the same "Latency Avoided" idea the
     LLM cache dashboard already reports, just without a cost figure
     riding alongside it (there is no meaningful universal price for "a
     data source call").

Nothing in here talks to Couchbase; persistence lives in
app/couchbase_client.py and orchestration lives in app/main.py.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone

CACHE_SCOPES = ("global", "per_role", "per_agent")
EVICTION_POLICIES = ("lru", "lfu", "fifo")

# Every field here is surfaced on the Context Cache page's policy panel -
# same convention as llm_cache.DEFAULT_CACHE_CONFIG: anything the user can
# change about when a cached value stops being usable is in this dict and
# nowhere else.
DEFAULT_CACHE_CONFIG: dict = {
    "enabled": True,
    "ttl_seconds": 3600,
    "max_entries": 20000,
    "eviction_policy": "lru",
    # "global": any agent/role sharing a namespace+key sees the same entry.
    # "per_role"/"per_agent": trade hit rate for isolation when the same key
    # means something different depending on who is asking.
    "cache_scope": "per_agent",
    "namespace": "default",
    "max_value_bytes": 65536,
    "sweep_interval_minutes": 5,
}


def normalize_config(raw: dict | None) -> dict:
    """Merge a stored/submitted config over the defaults and coerce every
    field to a sane type and range. The UI is not the validator - this is."""
    cfg = dict(DEFAULT_CACHE_CONFIG)
    for k, v in (raw or {}).items():
        if k in cfg:
            cfg[k] = v

    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["ttl_seconds"] = int(_clamp(int(cfg.get("ttl_seconds", 3600) or 0), 0, 60 * 60 * 24 * 90))
    cfg["max_entries"] = int(_clamp(int(cfg.get("max_entries", 20000) or 0), 0, 2_000_000))
    cfg["max_value_bytes"] = int(_clamp(int(cfg.get("max_value_bytes", 65536) or 65536), 256, 5_000_000))
    cfg["sweep_interval_minutes"] = int(_clamp(int(cfg.get("sweep_interval_minutes", 5) or 5), 1, 1440))

    if cfg.get("eviction_policy") not in EVICTION_POLICIES:
        cfg["eviction_policy"] = "lru"
    if cfg.get("cache_scope") not in CACHE_SCOPES:
        cfg["cache_scope"] = "per_agent"

    ns = str(cfg.get("namespace") or "default").strip() or "default"
    cfg["namespace"] = re.sub(r"[^a-zA-Z0-9_.:-]", "-", ns)[:64]
    return cfg


def _clamp(value, low, high):
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Keys and scope
# ---------------------------------------------------------------------------
def scope_key(cfg: dict, role: str | None, subject: str | None) -> str:
    """Which callers may share a cached value. `global` is the cheapest and
    trades isolation for hit rate; `per_role`/`per_agent` isolate a key that
    means something different depending on who is asking - the default,
    since two agents fetching "the same" key from two different tools is a
    coincidence more often than it's a cache hit."""
    mode = cfg.get("cache_scope", "per_agent")
    if mode == "per_role":
        return f"role:{role or 'anonymous'}"
    if mode == "per_agent":
        return f"agent:{subject or 'anonymous'}"
    return "global"


def normalize_key(key: str) -> str:
    return re.sub(r"\s+", " ", (key or "").strip())


def entry_id(cfg: dict, namespace: str, scope: str, key: str) -> str:
    material = json.dumps(
        {"namespace": namespace or cfg.get("namespace", "default"), "scope": scope, "key": normalize_key(key)},
        sort_keys=True,
    )
    return "ctxcache::" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


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
# entry's age.
parse_timestamp = _parse_ts


def evaluate_entry(entry: dict, cfg: dict, *, now: float | None = None) -> tuple[str, str | None]:
    """Classify a stored entry as 'fresh' or 'invalid'. Returns (state,
    reason) - the single implementation of the TTL rule, so a read, the
    background sweeper and the Cache Entries table can never disagree about
    whether an entry is still good.

    TTL is read off the entry itself, not the live policy: a value written
    with an explicit per-call `ttl_seconds` keeps that lifetime even if the
    default policy TTL changes later, exactly like an agent asked for."""
    now = now or time.time()
    age = now - _parse_ts(entry.get("created_at"))
    ttl = entry.get("ttl_seconds")
    ttl = int(ttl) if ttl is not None else int(cfg.get("ttl_seconds") or 0)
    if ttl and age > ttl:
        return "invalid", f"older than TTL ({ttl}s)"
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


def value_preview(value, limit: int = 240) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    text = text.strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def value_size_bytes(value) -> int:
    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8"))


# ---------------------------------------------------------------------------
# Savings dashboard
# ---------------------------------------------------------------------------
def build_dashboard_aggregate(rows: list[dict], *, trend_hours: int = 12, lifetime: dict | None = None) -> dict:
    """Build the entire live dashboard payload - 'summary' (donut + stat
    cards), 'agent_breakdown' ('Traffic by agent & namespace'), and 'hourly'
    (the trend chart) - from ONE set of pre-aggregated Couchbase GROUP BY
    rows keyed by (hour_key, namespace, subject, outcome). See
    CouchbaseStore.context_dashboard_aggregate_since - same shape and same
    reasoning as llm_cache.build_dashboard_aggregate, adapted for
    hit/miss/write instead of hit_exact/hit_semantic/miss/bypass/error and
    for latency instead of tokens/cost.

    `lifetime` (see CouchbaseStore.get_context_lifetime_stats), when given,
    overrides the headline "all-time" stat cards - the rest stays
    window-scoped (24h)."""
    import datetime as _dt

    outcome_totals: dict[str, dict] = {}
    agent_totals: dict[tuple, dict] = {}

    now = _dt.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    hour_keys = [(now - _dt.timedelta(hours=i)) for i in range(trend_hours - 1, -1, -1)]
    hourly = {h.strftime("%Y-%m-%dT%H"): {"hits": 0, "misses": 0} for h in hour_keys}

    for r in rows:
        outcome = r.get("outcome")
        n = int(r.get("n") or 0)
        latency_saved_ms = int(r.get("latency_saved_ms") or 0)
        latency_ms_sum = int(r.get("latency_ms_sum") or 0)
        value_bytes_sum = int(r.get("value_bytes_sum") or 0)

        ot = outcome_totals.setdefault(outcome, {"n": 0, "latency_saved_ms": 0, "latency_ms_sum": 0, "value_bytes_sum": 0})
        ot["n"] += n
        ot["latency_saved_ms"] += latency_saved_ms
        ot["latency_ms_sum"] += latency_ms_sum
        ot["value_bytes_sum"] += value_bytes_sum

        if outcome in ("hit", "miss"):
            akey = (r.get("subject") or "unknown", r.get("namespace") or "default")
            at = agent_totals.setdefault(akey, {"lookups": 0, "hits": 0, "misses": 0, "latency_saved_ms": 0, "writes": 0})
            at["lookups"] += n
            if outcome == "hit":
                at["hits"] += n
                at["latency_saved_ms"] += latency_saved_ms
            else:
                at["misses"] += n
        elif outcome == "write":
            akey = (r.get("subject") or "unknown", r.get("namespace") or "default")
            at = agent_totals.setdefault(akey, {"lookups": 0, "hits": 0, "misses": 0, "latency_saved_ms": 0, "writes": 0})
            at["writes"] += n

        hour_key = r.get("hour_key") or ""
        if hour_key in hourly:
            if outcome == "hit":
                hourly[hour_key]["hits"] += n
            elif outcome == "miss":
                hourly[hour_key]["misses"] += n

    def _n(outcome):
        return int((outcome_totals.get(outcome) or {}).get("n") or 0)

    def _sum(outcome, field):
        return (outcome_totals.get(outcome) or {}).get(field) or 0

    hits = _n("hit")
    misses = _n("miss")
    writes = _n("write")
    lookups = hits + misses
    latency_saved = int(_sum("hit", "latency_saved_ms"))
    hit_latency_sum = int(_sum("hit", "latency_ms_sum"))
    miss_latency_sum = int(_sum("miss", "latency_ms_sum"))
    bytes_cached = int(_sum("write", "value_bytes_sum"))

    summary = {
        "lookups": lookups,
        "hits": hits,
        "misses": misses,
        "writes": writes,
        "hit_rate_pct": round((hits / lookups) * 100, 1) if lookups else 0.0,
        "latency_saved_ms": latency_saved,
        "avg_hit_latency_ms": round(hit_latency_sum / hits) if hits else 0,
        "avg_miss_latency_ms": round(miss_latency_sum / misses) if misses else 0,
        "bytes_cached": bytes_cached,
    }

    lifetime = lifetime or {}
    breakdown = []
    for (subject, namespace), row in agent_totals.items():
        row = dict(row, agent=subject, namespace=namespace)
        row["hit_rate_pct"] = round((row["hits"] / row["lookups"]) * 100, 1) if row["lookups"] else 0.0
        breakdown.append(row)
    breakdown.sort(key=lambda r: r["lookups"], reverse=True)

    hourly_series = [
        {
            "hour": h.strftime("%H:00"),
            "timestamp": h.strftime("%Y-%m-%dT%H:00:00Z"),
            "hits": hourly[h.strftime("%Y-%m-%dT%H")]["hits"],
            "misses": hourly[h.strftime("%Y-%m-%dT%H")]["misses"],
        }
        for h in hour_keys
    ]

    return {
        "summary": summary,
        "agent_breakdown": breakdown,
        "hourly": hourly_series,
        "lookups_total": int(lifetime.get("lookups_total") or lookups),
        "hits_total": int(lifetime.get("hits_total") or hits),
        "latency_saved_ms_total": int(lifetime.get("latency_saved_ms_total") or latency_saved),
    }
