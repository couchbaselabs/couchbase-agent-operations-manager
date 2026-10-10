"""
Couchbase Agent Operations Manager - configuration.

Docker Compose supplies safe defaults for every value here. For a local
non-Docker run, set the corresponding environment variables yourself.
"""
import os

APPLIANCE_NAME = os.getenv("APPLIANCE_NAME", "Couchbase Agent Operations Manager")

COUCHBASE_CONFIG = {
    "connection_string": os.getenv("COUCHBASE_CONNECTION_STRING", "couchbase://localhost"),
    "username": os.getenv("COUCHBASE_USERNAME", "Administrator"),
    "password": os.getenv("COUCHBASE_PASSWORD", "CouchbaseDemo123!"),
    "bucket": os.getenv("COUCHBASE_BUCKET", "agent_operations"),
    "scope": os.getenv("COUCHBASE_SCOPE", "agent_operations"),
    "servers_collection": "servers",
    "tools_collection": "tools",
    "identities_collection": "identities",
    "access_log_collection": "access_log",
    "llm_cache_collection": "llm_cache",
    "llm_cache_log_collection": "llm_cache_log",
    "context_cache_collection": "context_cache",
    "context_cache_log_collection": "context_cache_log",
    "settings_collection": "settings",
    "agent_memory_collection": "agent_memory",
    "users_collection": "users",
    "traces_collection": "traces",
    "evals_collection": "evals",
    "counters_collection": "counters",
    "approvals_collection": "approvals",
    "knowledge_collection": "knowledge",
    "tools_index": os.getenv("COUCHBASE_TOOLS_INDEX", "tools_rbac_vector_index"),
    "llm_cache_index": os.getenv("COUCHBASE_LLM_CACHE_INDEX", "llm_cache_vector_index"),
    "agent_memory_index": os.getenv("COUCHBASE_AGENT_MEMORY_INDEX", "agent_memory_vector_index"),
    "knowledge_index": os.getenv("COUCHBASE_KNOWLEDGE_INDEX", "knowledge_vector_index"),
    "search_host": os.getenv("COUCHBASE_SEARCH_HOST", "localhost"),
    # Scheme + port for the Search (FTS) admin REST API. The bundled server
    # speaks plain http on 8094; an external TLS-only cluster (Capella, or
    # a self-managed cluster with only the secure ports open) needs
    # https on 18094 - set COUCHBASE_SEARCH_SCHEME=https and
    # COUCHBASE_SEARCH_PORT=18094 together.
    "search_scheme": os.getenv("COUCHBASE_SEARCH_SCHEME", "http"),
    "search_port": int(os.getenv("COUCHBASE_SEARCH_PORT", "8094")),
    # TLS trust for couchbases:// connections and https Search calls. A
    # path to a PEM CA bundle (a private/corporate CA that signed your
    # cluster's certificate) or empty to use the system trust store, which
    # is right for Capella and any publicly-signed certificate. Set
    # COUCHBASE_TLS_VERIFY=false only for a lab cluster with a self-signed
    # certificate you can't install - it disables verification entirely.
    "tls_ca_file": os.getenv("COUCHBASE_TLS_CA_FILE", ""),
    "tls_verify": os.getenv("COUCHBASE_TLS_VERIFY", "true").strip().lower() not in ("0", "false", "no"),
}


def couchbase_requests_verify():
    """`verify=` argument for every `requests` call against the cluster's
    REST APIs: the CA bundle path when one is configured, False when
    verification is switched off, True (system trust store) otherwise."""
    if not COUCHBASE_CONFIG["tls_verify"]:
        return False
    return COUCHBASE_CONFIG["tls_ca_file"] or True

# Base URL for the bundled sample MCP tool servers (jira/zendesk/snowflake/
# shadow-diagnostics). Only used to seed the three *trusted* sample servers
# on first boot - real deployments point server registrations at whatever
# MCP endpoints they actually operate, via the Servers page or POST /v1/servers.
SAMPLE_MCP_SERVERS_BASE_URL = os.getenv("SAMPLE_MCP_SERVERS_BASE_URL", "http://localhost:8100")

# Seed identities: API key -> RBAC role, provisioned into the `identities`
# collection on startup if not already present. Rotate these for anything
# beyond local evaluation - see the Roles page / README.
SEED_API_KEYS = {
    os.getenv("API_KEY_SUPPORT_AGENT", "demo-support-agent-9f21"): "support_agent",
    os.getenv("API_KEY_FINANCE_ANALYST", "demo-finance-analyst-7e83"): "finance_analyst",
    os.getenv("API_KEY_ADMIN", "demo-admin-4c56"): "admin",
}

EMBEDDING_CONFIG = {
    "model_name": os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
    "vector_dim": int(os.getenv("EMBEDDING_VECTOR_DIM", "384")),
}

SERVER_CONFIG = {
    "host": os.getenv("HOST", "0.0.0.0"),
    "port": int(os.getenv("PORT", "8090")),
}

# ---------------------------------------------------------------------------
# CORS - browser cross-origin access to the API.
# ---------------------------------------------------------------------------
# Empty by default: the dashboard is always served same-origin (nginx
# proxies /v1/* and /api/* through to this service under the ui container's
# own origin - see ui/nginx.conf.template), so no browser cross-origin
# access to session-cookie-protected routes is actually needed out of the
# box. Set this to a comma-separated list of exact origins
# (e.g. "https://aom.example.com,https://ops.example.com") only if you have
# a real reason for a *different* origin's browser JS to call this API with
# the dashboard session cookie attached. Never combine a wildcard origin
# with credentialed (cookie/session) access - that defeats the same-origin
# protection the session cookie exists to provide (see main.py).
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]

# How long an audit-log entry survives before Couchbase expires it.
#
# This is a capacity setting as much as a compliance one. One entry is
# written per request - discover, invoke, completion, dashboard login, every
# rate-limit decision - and Couchbase holds each document's metadata in RAM
# for as long as it exists, whether or not its value has been ejected to
# disk. Retention x request rate is therefore what decides whether the bucket
# fills up; a bucket that fills up stops accepting writes altogether and no
# restart clears it.
#
# 7 days is the shipped default because it survives a long demo or a weekend
# unattended without approaching the bucket quota that
# couchbase-init/init.sh provisions. Raise it for a real compliance window -
# but raise COUCHBASE_BUCKET_RAMSIZE with it, and remember that expired items
# free their metadata at the metadata purge interval (3 days by default),
# not the moment they expire.
AUDIT_LOG_RETENTION_HOURS = int(os.getenv("AUDIT_LOG_RETENTION_HOURS", str(24 * 7)))

# How many recent audit-log entries the insights engine and dashboard
# time series look back over.
INSIGHTS_LOOKBACK_ENTRIES = int(os.getenv("INSIGHTS_LOOKBACK_ENTRIES", "250"))

# Size of the worker-thread pool every blocking Couchbase call runs in.
#
# Each `asyncio.to_thread(...)` in this codebase hands work to asyncio's
# *default* executor, which is min(32, cpu_count + 4) threads - inside a
# container that is usually 8, sized off however many CPUs Docker was
# given rather than off how much of this app's work is I/O-bound waiting
# on Couchbase. In front of those threads sits an unbounded queue, so once
# a few slow N1QL queries park every thread, every later call - a KV get on
# the login path included - waits behind a queue that only grows. That is a
# silent collapse: the process stays up and accepts connections, and nginx
# reports 504 because nothing ever answers.
#
# Sizing the pool explicitly (these are I/O waits, not CPU work, so more
# threads than cores is the point) and capping in-flight requests at the
# server (--limit-concurrency, see docker-entrypoint.sh) turns that into
# fast, visible backpressure instead.
WORKER_THREADS = int(os.getenv("WORKER_THREADS", "32"))

# Ceiling on a single N1QL/Search request, and the one setting that actually
# bounds how long a worker thread can be held.
#
# The SDK defaults to 75s. Every query in this codebase runs inside
# `asyncio.to_thread(...)`, and a thread already running in the executor
# cannot be cancelled - so the request-level deadline in app/main.py frees
# the *request* but not the thread underneath it. The thread comes back only
# when the SDK call returns or times out. That makes this the real backstop
# against pool exhaustion: it caps worst-case thread occupancy at 30s rather
# than 75s.
#
# Raise it if a bulk operator action legitimately needs longer (purging a
# large LLM cache, pruning a long eval history are the plausible ones) - but
# raising it also raises how long one bad query can hold a worker.
QUERY_TIMEOUT_SECONDS = int(os.getenv("QUERY_TIMEOUT_SECONDS", "30"))

# Whole-request ceiling for the dashboard's own routes (everything that is
# not an agent-facing path - see AGENT_PATH_PREFIXES in app/main.py, whose
# ceiling is the operator-editable governance policy instead).
#
# Set just under nginx's proxy_read_timeout (60s - see
# ui/nginx.conf.template), because whoever gives up first decides what
# happens. If nginx wins, the operator gets an HTML "Gateway Time-out" page
# in place of the dashboard AND the worker stays parked - nginx hanging up
# does not cancel the handler, so the request that caused the timeout keeps
# holding its thread long after the browser gave up on it. That is the leak
# that turns a slow appliance into an unreachable one. If this wins, the UI
# renders an ordinary error in the one panel that failed and the worker is
# released immediately.
#
# 55s deliberately preserves today's user-visible ceiling rather than
# tightening it: any operator action already slower than 60s (a large
# knowledge-base upload, a full catalog re-ingest) was already being cut off
# by nginx. If you need to raise it for one of those, raise
# proxy_read_timeout in ui/nginx.conf.template to match - this must stay the
# smaller of the two.
DASHBOARD_REQUEST_TIMEOUT_SECONDS = int(os.getenv("DASHBOARD_REQUEST_TIMEOUT_SECONDS", "55"))

# MCP Tool Hijacking detection (see app/hijack_detection.py). The background
# monitor re-scans every already-ingested tool's stored description against
# the current pattern bank on this interval (no MCP round-trip - see
# catalog_ingest.rescan_all_tools). The chain-correlation window is how long
# after a response-poisoning-flagged invoke a subsequent higher-risk invoke
# by the same subject still counts as a possible cross-tool hijack chain.
HIJACK_SCAN_INTERVAL_MINUTES = int(os.getenv("HIJACK_SCAN_INTERVAL_MINUTES", "5"))
HIJACK_CHAIN_WINDOW_SECONDS = int(os.getenv("HIJACK_CHAIN_WINDOW_SECONDS", "120"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


# ---------------------------------------------------------------------------
# Agent run tracing (see app/tracing.py)
# ---------------------------------------------------------------------------
# Every discover/invoke/complete/memory call writes spans belonging to a run.
# Turning this off stops the writes without changing any other behaviour -
# the gateway does not depend on its own traces for anything.
TRACING_ENABLED = os.getenv("TRACING_ENABLED", "true").lower() != "false"

# How long a span or run summary survives before Couchbase expires it.
#
# Traces are the highest-volume thing this appliance writes - one run can be
# a dozen spans - so this is the shortest of the three retention windows.
# Three days of traces is roughly the same number of documents as the seven
# days of audit entries AUDIT_LOG_RETENTION_HOURS keeps, which is the point:
# what fills a Couchbase bucket is item count, not age, because every
# document's metadata stays resident in RAM whether or not its value has been
# ejected to disk. Retention windows are how this appliance stays inside its
# bucket quota, so they are sized against each other rather than each picked
# on its own.
TRACE_RETENTION_HOURS = int(os.getenv("TRACE_RETENTION_HOURS", str(24 * 3)))
# Fraction of agent runs whose spans are stored (0.0-1.0). Every span costs
# three Couchbase operations and one document for TRACE_RETENTION_HOURS, so
# at sustained high request rates tracing dominates the bucket's size.
# Sampling is per trace (a run is kept whole or not at all), and a span
# with status "error" is always stored whatever the rate.
TRACE_SAMPLE_RATE = min(1.0, max(0.0, float(os.getenv("TRACE_SAMPLE_RATE", "1.0"))))

# How many recent runs the Traces page and the run aggregates look back over.
TRACE_LOOKBACK_RUNS = int(os.getenv("TRACE_LOOKBACK_RUNS", "500"))


# ---------------------------------------------------------------------------
# Evaluation (see app/evals.py)
# ---------------------------------------------------------------------------
# Whether a catalog change (the same trigger that invalidates the LLM cache,
# for the same reason: an agent's behaviour depends on which tools it was
# allowed to see) also runs every dataset marked run_on_catalog_change.
EVAL_RUN_ON_CATALOG_CHANGE = os.getenv("EVAL_RUN_ON_CATALOG_CHANGE", "true").lower() != "false"

# Seed the bundled starter dataset on first boot, so the feature is
# demonstrable before anyone has written a dataset of their own - the same
# posture as the sample MCP servers and the offline LLM stub.
EVAL_SEED_STARTER_DATASET = os.getenv("EVAL_SEED_STARTER_DATASET", "true").lower() != "false"

# How many eval runs to keep per dataset. Runs carry every case result, so
# they are not small; the regression gate only ever needs the previous one.
EVAL_RUN_HISTORY = int(os.getenv("EVAL_RUN_HISTORY", "50"))


# ---------------------------------------------------------------------------
# Knowledge base (see app/knowledge.py)
# ---------------------------------------------------------------------------
# Chunk size and overlap, in characters. The defaults sit inside what the
# configured embedding model represents well - raising the chunk size past
# the model's useful context produces chunks it cannot distinguish.
KNOWLEDGE_CHUNK_CHARS = int(os.getenv("KNOWLEDGE_CHUNK_CHARS", "1200"))
KNOWLEDGE_CHUNK_OVERLAP = int(os.getenv("KNOWLEDGE_CHUNK_OVERLAP", "200"))

# Largest upload accepted, in megabytes. Ingestion embeds every chunk on
# CPU, so this bounds how long one upload can occupy the service.
KNOWLEDGE_MAX_UPLOAD_MB = int(os.getenv("KNOWLEDGE_MAX_UPLOAD_MB", "20"))


# ---------------------------------------------------------------------------
# LLM response caching for agents (see app/llm_cache.py)
# ---------------------------------------------------------------------------
# Provider API keys. Every one of these is optional: a provider with no key
# configured still answers on a cache miss, from a clearly-labelled offline
# stub, so the caching gateway and its savings dashboard work on first boot
# with no outbound network access. Configure a key to proxy real calls.
LLM_API_KEYS = {
    "anthropic": os.getenv("ANTHROPIC_API_KEY", ""),
    "openai": os.getenv("OPENAI_API_KEY", ""),
    "google": os.getenv("GEMINI_API_KEY", ""),
    # Databricks needs a workspace URL as well as a token; without
    # DATABRICKS_HOST the provider is treated as unconfigured (offline stub).
    "databricks": os.getenv("DATABRICKS_TOKEN", "") if os.getenv("DATABRICKS_HOST", "").strip() else "",
}

# Runtime cache policy lives in Couchbase (settings::llm_cache) because it is
# user-editable from the LLM Caching page. These are only the bootstrap
# defaults applied the first time the appliance starts with an empty
# settings collection - after that, the stored document wins.
LLM_CACHE_DEFAULTS = {
    "enabled": os.getenv("LLM_CACHE_ENABLED", "true").lower() != "false",
    "provider": os.getenv("LLM_CACHE_PROVIDER", "anthropic"),
    "model": os.getenv("LLM_CACHE_MODEL", "claude-sonnet-4-5"),
    "ttl_seconds": int(os.getenv("LLM_CACHE_TTL_SECONDS", "3600")),
    "max_entries": int(os.getenv("LLM_CACHE_MAX_ENTRIES", "5000")),
    "similarity_threshold": float(os.getenv("LLM_CACHE_SIMILARITY_THRESHOLD", "0.94")),
    "semantic_enabled": os.getenv("LLM_CACHE_SEMANTIC_ENABLED", "true").lower() != "false",
}

# How long a cache hit/miss event survives before Couchbase expires it. The
# savings dashboard is computed from these events, so this is also how far
# back "tokens saved" can look.
#
# Matched to AUDIT_LOG_RETENTION_HOURS (7 days): one event is written per
# agent completion, so this grows with traffic exactly like the audit log
# does, and the same bucket-quota arithmetic applies - see
# TRACE_RETENTION_HOURS. Raise it for a longer savings history only alongside
# COUCHBASE_BUCKET_RAMSIZE.
LLM_CACHE_LOG_RETENTION_HOURS = int(os.getenv("LLM_CACHE_LOG_RETENTION_HOURS", str(24 * 7)))

# Runtime context-cache policy lives in Couchbase (settings::context_cache),
# same convention as LLM_CACHE_DEFAULTS above - these are only the
# bootstrap defaults applied the first time the appliance starts with an
# empty settings collection.
CONTEXT_CACHE_DEFAULTS = {
    "enabled": os.getenv("CONTEXT_CACHE_ENABLED", "true").lower() != "false",
    "ttl_seconds": int(os.getenv("CONTEXT_CACHE_TTL_SECONDS", "3600")),
    "max_entries": int(os.getenv("CONTEXT_CACHE_MAX_ENTRIES", "20000")),
    "eviction_policy": os.getenv("CONTEXT_CACHE_EVICTION_POLICY", "lru"),
    "cache_scope": os.getenv("CONTEXT_CACHE_SCOPE", "per_agent"),
}

# Same reasoning/shape as LLM_CACHE_LOG_RETENTION_HOURS - one event per
# context_get/context_set call, so this grows with traffic the same way.
CONTEXT_CACHE_LOG_RETENTION_HOURS = int(os.getenv("CONTEXT_CACHE_LOG_RETENTION_HOURS", str(24 * 7)))

# How many recent cache events the savings dashboard aggregates over.
LLM_CACHE_LOOKBACK_ENTRIES = int(os.getenv("LLM_CACHE_LOOKBACK_ENTRIES", "2000"))


# ---------------------------------------------------------------------------
# Local dashboard login (human users of the Settings/Servers/Roles UI - not
# to be confused with the agent identities above, which authenticate with a
# bearer API key and never see a login page).
# ---------------------------------------------------------------------------
# Signs session tokens (see app/user_auth.py) and, via a derived key, encrypts
# secrets stored at rest in Couchbase (currently just the LDAP bind
# password). Docker Compose generates a random one into .env on first
# `start.sh` run - see that script. Set your own for a non-Docker deploy;
# changing it invalidates every existing session and re-encrypts nothing
# already stored, so rotate LDAP bind creds afterward if you change it in
# production.
# "or", not just the getenv default, so an *empty* env var (a .env
# hand-copied from .env.example without running start.sh, which is what
# actually generates one) still falls back instead of signing sessions
# and encrypting the LDAP bind password with an empty string.
AUTH_SECRET_KEY = os.getenv("AUTH_SECRET_KEY") or "dev-only-insecure-secret-change-me"

# How long a browser session stays signed in before the login page reappears.
AUTH_SESSION_TTL_HOURS = int(os.getenv("AUTH_SESSION_TTL_HOURS", "12"))

# The built-in local account every fresh install boots with. It has no
# password until the first person to reach the login page sets one (see
# POST /v1/auth/bootstrap) - there is no factory-default password to leave
# unchanged.
DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")

# Couchbase settings-collection doc id for the LDAP configuration (same
# settings::<name> convention as settings::llm_cache) - see app/user_auth.py.
LDAP_SETTINGS_DOC = "settings::ldap"

# Couchbase settings-collection doc id for the SIEM/log-forwarding
# destinations (same settings::<name> convention) - see app/siem_forwarding.py.
SIEM_SETTINGS_DOC = "settings::siem"

# Couchbase settings-collection doc id for the rate limits, budgets and
# timeouts applied to the gateway (same settings::<name> convention) - see
# app/governance.py and Settings -> Limits & Budgets.
GOVERNANCE_SETTINGS_DOC = "settings::governance"

# Couchbase settings-collection doc id for the input guardrails and PII
# policy (same settings::<name> convention) - see app/guardrails.py and
# Settings -> Guardrails & PII.
GUARDRAILS_SETTINGS_DOC = "settings::guardrails"

# Couchbase settings-collection doc id for the human approval tier - see
# app/approvals.py and the Approvals page.
APPROVALS_SETTINGS_DOC = "settings::approvals"

# Couchbase settings-collection doc id for memory consolidation - dedup,
# importance scoring and session rollup (see app/memory_consolidation.py
# and the Agent Memory page).
MEMORY_SETTINGS_DOC = "settings::memory"

# Couchbase settings-collection doc id for federated agent identity -
# accepting bearer JWTs from a customer IdP (see app/agent_oidc.py and
# Settings -> Agent Identities).
AGENT_OIDC_SETTINGS_DOC = "settings::agent_oidc"

# How long a rotated agent key keeps working after its replacement is
# issued. Rotation that causes an outage is rotation nobody performs, so
# the default leaves an hour for whatever holds the old key to pick up the
# new one. A key believed to be compromised is revoked instead, which
# takes effect on the next request.
AGENT_KEY_ROTATION_GRACE_SECONDS = int(os.getenv("AGENT_KEY_ROTATION_GRACE_SECONDS", "3600"))


# ---------------------------------------------------------------------------
# HTTPS server certificate (see Settings -> HTTPS Certificate, app/user_auth.py)
# ---------------------------------------------------------------------------
# Same two paths and same env var names as operations-manager/docker-entrypoint.sh
# uses to launch uvicorn, so the Settings page always reads/writes exactly the
# file uvicorn is actually serving from - never a copy that could drift out of
# sync with it. These live in a Docker named volume shared with the `ui`
# service's /etc/nginx/tls (see docker-compose.yml), so one uploaded
# certificate applies to both the dashboard and the API, and a self-signed
# fallback baked into the image still populates the volume on first boot.
TLS_CERT_FILE = os.getenv("TLS_CERT_FILE", "/app/tls/server.crt")
TLS_KEY_FILE = os.getenv("TLS_KEY_FILE", "/app/tls/server.key")
# Where the baked-in self-signed fallback is backed up the first time a real
# certificate is installed, so Settings -> HTTPS Certificate can offer
# "revert to the default self-signed certificate" without needing to
# regenerate one from scratch.
TLS_CERT_DEFAULT_BACKUP = TLS_CERT_FILE + ".default-backup"
TLS_KEY_DEFAULT_BACKUP = TLS_KEY_FILE + ".default-backup"
