"""
Couchbase Agent Operations Manager.

The FastAPI service that stands between an AI agent and the world of MCP
tool servers. An agent never talks to a downstream MCP server directly
here: it authenticates to this service, asks to *discover* tools for a
task (which runs a Couchbase RBAC + vector-search pre-filter, never a full
unfiltered tool dump), and asks this service to *invoke* whichever tool it
picked - which gets checked against Couchbase again before anything is
proxied downstream. Every discovery and invocation decision is written to
an append-only audit log in Couchbase.

Beyond the core discover/invoke gateway, this module also exposes the
admin surface the dashboard UI runs on: server registration, catalog
inspection, roles, the audit log, a derived stats/insights view, and the
MCP Tool Hijacking detection surface (quarantine/release actions plus a
background monitor that re-scans the catalog on a timer - see
app/hijack_detection.py).
"""
import asyncio
import base64
import binascii
import concurrent.futures
import functools
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import (
    agent_identity,
    agent_memory,
    agent_oidc,
    approvals,
    catalog_ingest,
    context_cache,
    embedding_models,
    evals,
    governance,
    guardrails,
    knowledge,
    knowledge_sets,
    hijack_detection,
    insights,
    llm_cache,
    memory_consolidation,
    rag_apps,
    mcp_client,
    sdk_packaging,
    server_auth,
    siem_forwarding,
    skill_packaging,
    tool_versioning,
    tracing,
    user_auth,
)
from app.catalog_ingest import ingest_all, ingest_server, rescan_all_tools, seed_servers
from app.couchbase_client import CouchbaseStore, set_siem_config_provider
from app.embeddings import ToolEmbeddings
from app.rbac_policy import ROLES, normalize_tool_policies
from config import (
    APPLIANCE_NAME,
    AUTH_SECRET_KEY,
    AUTH_SESSION_TTL_HOURS,
    CORS_ALLOWED_ORIGINS,
    COUCHBASE_CONFIG,
    DASHBOARD_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_ADMIN_USERNAME,
    EMBEDDING_CONFIG,
    EVAL_RUN_ON_CATALOG_CHANGE,
    AGENT_KEY_ROTATION_GRACE_SECONDS,
    AGENT_OIDC_SETTINGS_DOC,
    APPROVALS_SETTINGS_DOC,
    CONTEXT_CACHE_DEFAULTS,
    EVAL_SEED_STARTER_DATASET,
    GOVERNANCE_SETTINGS_DOC,
    GUARDRAILS_SETTINGS_DOC,
    KNOWLEDGE_CHUNK_CHARS,
    KNOWLEDGE_CHUNK_OVERLAP,
    KNOWLEDGE_MAX_UPLOAD_MB,
    MEMORY_SETTINGS_DOC,
    HIJACK_CHAIN_WINDOW_SECONDS,
    HIJACK_SCAN_INTERVAL_MINUTES,
    INSIGHTS_LOOKBACK_ENTRIES,
    LDAP_SETTINGS_DOC,
    LLM_API_KEYS,
    LLM_CACHE_DEFAULTS,
    SAMPLE_MCP_SERVERS_BASE_URL,
    SEED_API_KEYS,
    SIEM_SETTINGS_DOC,
    TRACE_LOOKBACK_RUNS,
    TRACING_ENABLED,
    TRACE_SAMPLE_RATE,
    WORKER_THREADS,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("operations-manager")

# asyncio keeps only a weak reference to a task, so a fire-and-forget
# create_task() whose result nobody holds can be garbage-collected before it
# finishes - for the background loops below, that would mean a sweeper or
# monitor silently stopping partway through a long-running deployment.
# Every background task is spawned through here so it stays referenced
# until it completes.
_background_tasks: set = set()


def spawn(coro) -> "asyncio.Task":
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


app = FastAPI(
    title=APPLIANCE_NAME,
    description="RBAC + Couchbase Vector Search pre-filtering gateway for MCP tool servers.",
    version="1.0.0",
)
# CORS: the dashboard session cookie makes this security-sensitive (see
# config.CORS_ALLOWED_ORIGINS) - a wildcard origin combined with
# allow_credentials=True would let *any* site's browser JS ride a logged-in
# admin's session cookie to this API. With no origins configured (the
# out-of-the-box case, since the dashboard is always same-origin through
# nginx - see ui/nginx.conf.template), credentialed cross-origin access is
# simply off; agent callers using a Bearer API key are entirely unaffected,
# since that's a header they set themselves; never something a browser
# attaches automatically the way it does a cookie.
if CORS_ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# Security response headers - CIS/PCI-DSS-4.0/NIST-SC-23-aligned defaults
# for every response this API sends. /docs and /redoc are excluded from the
# CSP because FastAPI's bundled Swagger/ReDoc UI loads its JS/CSS from a
# CDN - a strict default-src 'self' there would just break the docs page,
# not protect anything (it's dev/ops tooling, not an attacker-reachable
# surface any differently than the rest of the API).
# ---------------------------------------------------------------------------
_NO_CSP_PATH_PREFIXES = ("/docs", "/redoc", "/openapi.json")

# Every path an agent can reach: the four gateway surfaces plus the two
# download routes the SDK and the AI-assistant skills come from. This list is
# what makes limits opt-*out* rather than opt-in - a route added under one of
# these prefixes is bounded from the moment it exists, without whoever adds it
# having to remember. That is the difference that matters: the previous
# arrangement, where each route called the limiter itself, was correct for
# the routes that called it and silently unbounded for the ones that did not.
AGENT_PATH_PREFIXES = (
    "/v1/tools/discover",
    "/v1/tools/invoke",
    "/v1/llm/complete",
    "/v1/context/get",
    "/v1/context/set",
    "/v1/memory",
    "/v1/agent/",
    "/v1/sdk/",
    "/v1/skills/",
)


KNOWLEDGE_INGEST_TIMEOUT_SECONDS = int(os.getenv("KNOWLEDGE_INGEST_TIMEOUT_SECONDS", "600"))


async def _with_deadline(request: Request, call_next, timeout: int):
    """Run a request under a wall-clock ceiling, and give the worker back if
    it is hit.

    Deliberately writes nothing to Couchbase and records no span on the
    timeout path: this only runs when the appliance is already struggling to
    keep up, and the last thing a saturated worker pool needs is two more
    pieces of work per failure.
    """
    if not timeout:
        return await call_next(request)
    try:
        return await asyncio.wait_for(call_next(request), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "Request to %s exceeded the %ss request timeout", request.url.path, timeout
        )
        return JSONResponse(
            status_code=504,
            content={"detail": f"Request exceeded the {timeout}s request timeout"},
        )


# In measure-only mode ("enabled" but not "enforce") a caller over a limit
# is over it on every request until the window rolls - and a span per
# request for that doubled the trace volume at exactly the moment traffic
# was highest. A limit that blocks is still traced every time; one that is
# only measured is traced at most once per caller, path and interval.
SOFT_LIMIT_TRACE_INTERVAL_SECONDS = float(os.getenv("SOFT_LIMIT_TRACE_INTERVAL_SECONDS", "60"))
_soft_limit_traced: dict[tuple, float] = {}


def _should_trace_soft_limit(subject: str | None, path: str) -> bool:
    key = (subject, path)
    now = time.monotonic()
    if now - _soft_limit_traced.get(key, 0.0) < SOFT_LIMIT_TRACE_INTERVAL_SECONDS:
        return False
    if len(_soft_limit_traced) > 10000:
        _soft_limit_traced.clear()
    _soft_limit_traced[key] = now
    return True


@app.middleware("http")
async def agent_request_limits(request: Request, call_next):
    """The request-rate ceiling and the whole-request timeout, applied to
    every agent-reachable route.

    Request rate is counted exactly once, here, rather than in each route -
    so the count is right no matter how many internal steps a route takes,
    and no route can forget to be limited. Routes still apply the ceilings
    only they can know about: tool-call rate and the per-run tool ceiling on
    invoke, token and spend budgets on completions.

    Callers with no API key (the SDK and skill downloads, which by design
    cannot require one - an agent has to fetch the client before it has a
    key) are counted against their address instead. Weaker, but the
    alternative is leaving the only unauthenticated routes in the appliance
    as the one unbounded surface.
    """
    path = request.url.path
    if request.method == "OPTIONS":
        return await call_next(request)
    if not path.startswith(AGENT_PATH_PREFIXES):
        # Not an agent route, so none of the governance limits below apply to
        # it - but the whole-request ceiling does. The dashboard's own routes
        # were the one surface in the appliance with no ceiling at all, and
        # they are the expensive ones: /v1/dashboard, /v1/insights and
        # /v1/threat-detection each fan out into several N1QL queries, and the
        # UI re-runs them on a timer. With nothing bounding them, a slow
        # cluster left every one of those requests holding a worker until
        # nginx gave up at 60s - which is how a gradually slower appliance
        # ends up unreachable, login page included.
        if request.method == "POST" and path == "/v1/knowledge":
            # A document upload is parsed, chunked and embedded inline, which
            # for a document near the upload limit takes minutes, not
            # seconds; the dashboard ceiling would cut it off part-way.
            return await _with_deadline(request, call_next, KNOWLEDGE_INGEST_TIMEOUT_SECONDS)
        return await _with_deadline(request, call_next, DASHBOARD_REQUEST_TIMEOUT_SECONDS)

    # Resolve the caller once and stash it, so authenticate() inside the
    # route reuses this lookup instead of repeating it.
    role: str | None = None
    subject: str | None = None
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        credential = authorization.split(" ", 1)[1].strip()
        if credential and store.connected:
            try:
                identity, _reason = await resolve_caller(credential)
            except Exception:  # noqa: BLE001
                identity = None
            if identity:
                role, subject = identity["role"], identity["key_prefix"]
                # Stashed as a triple so the route's authenticate() reuses
                # the whole identity, not just the two fields it used to
                # need - scoping lives on the third element.
                request.state.agent_identity = (role, subject, identity)

    started = time.time()
    ctx = trace_context(request)
    limit_subject = subject or governance.anonymous_subject(
        request.client.host if request.client else None
    )

    try:
        verdicts = await check_limits(role, limit_subject, ["requests"], trace_id=ctx["trace_id"])
    except Exception as exc:  # noqa: BLE001
        # A limiter that cannot count must not take the gateway down with
        # it - log and let the call through, the same posture record_span
        # takes about its own failures.
        logger.warning("Rate-limit check failed for %s, allowing the call: %s", path, exc)
        verdicts = []

    blocking = governance.blocking_verdict(verdicts)
    exceeded = governance.exceeded_verdicts(verdicts)

    if exceeded and (blocking or _should_trace_soft_limit(limit_subject, path)):
        await record_span(
            ctx, kind="internal", name=f"limit check: {path}", started=started,
            status="error" if blocking else "ok", role=role, subject=subject,
            attributes={
                "aom.operation": "governance.check",
                "aom.path": path,
                "aom.limit_families": [v["family"] for v in exceeded],
                "aom.limit_reasons": [v["reason"] for v in exceeded if v.get("reason")],
                "aom.limit_blocked": bool(blocking),
                "aom.enforced": bool(governance_config.get("enforce")),
            },
        )

    if blocking:
        await store.log_access(
            action="request", role=role, subject_label=subject or limit_subject, query=None,
            tool_id=None, server_id=None, decision="DENY",
            reason=f"limit: {blocking['reason']}",
            latency_ms=int((time.time() - started) * 1000),
        )
        return JSONResponse(
            status_code=429,
            content={"detail": blocking["reason"]},
            headers=limit_headers(blocking),
        )
    if exceeded:
        logger.info("Limit exceeded but not enforced for %s (%s)", limit_subject, path)

    # The whole-request timeout. The two timeouts in the policy above bound
    # the calls this service makes; this one bounds the call made to it, so a
    # request that stalls somewhere neither of those covers still ends.
    timeout = int(governance_config.get("request_timeout_seconds") or 0)
    if not timeout:
        return await call_next(request)

    try:
        return await asyncio.wait_for(call_next(request), timeout=timeout)
    except asyncio.TimeoutError:
        latency_ms = int((time.time() - started) * 1000)
        logger.warning("Request to %s exceeded the %ss request timeout", path, timeout)
        await store.log_access(
            action="request", role=role, subject_label=subject or limit_subject, query=None,
            tool_id=None, server_id=None, decision="ERROR",
            reason=f"request exceeded the {timeout}s request timeout", latency_ms=latency_ms,
        )
        await record_span(
            ctx, kind="internal", name=f"timeout: {path}", started=started, status="error",
            role=role, subject=subject,
            attributes={"aom.operation": "governance.timeout", "aom.path": path,
                        "aom.timeout_seconds": timeout},
            error=f"request exceeded the {timeout}s request timeout",
        )
        return JSONResponse(
            status_code=504,
            content={"detail": f"Request exceeded the {timeout}s request timeout"},
        )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    # HSTS only makes sense once a client has actually reached us over TLS -
    # this appliance serves HTTPS by default but DISABLE_TLS=true remains a
    # supported plain-HTTP mode (see docker-entrypoint.sh), and sending
    # HSTS over plain HTTP is a no-op at best and a footgun at worst.
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not request.url.path.startswith(_NO_CSP_PATH_PREFIXES):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
    return response

# Paths reachable with no dashboard login session: the login/bootstrap flow
# itself, the health probe, and every agent-facing endpoint - those already
# authenticate their caller with a bearer API key via authenticate() above,
# which has nothing to do with a human's browser session cookie. Everything
# else under /v1 and /api is the dashboard's own admin surface (servers,
# catalog, roles, audit log, threat detection, insights, LLM caching
# policy, Settings) and requires a valid session.
UNPROTECTED_PATH_PREFIXES = (
    "/api/health",
    "/v1/auth/login",
    "/v1/auth/logout",
    "/v1/auth/bootstrap",
    "/v1/tools/discover",
    "/v1/tools/invoke",
    "/v1/llm/complete",
    "/v1/context/get",
    "/v1/context/set",
    "/v1/memory",
    # Agent-facing additions live under one namespace so a prefix never has
    # to mean "agent route" for some paths beneath it and "operator route"
    # for others - see AGENT_PATH_PREFIXES.
    "/v1/agent/",
    "/v1/sdk/",
    "/v1/skills/",
    "/docs",
    "/redoc",
    "/openapi.json",
)


@app.middleware("http")
async def require_dashboard_session(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or path.startswith(UNPROTECTED_PATH_PREFIXES):
        return await call_next(request)

    session = user_auth.decode_session_token(request.cookies.get(user_auth.SESSION_COOKIE_NAME))
    if not session:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    request.state.user = session
    response = await call_next(request)
    if request.method not in ("GET", "HEAD"):
        # A purge, sweep or settings change - drop cached dashboard views so
        # the page's follow-up refresh shows the result of the click.
        invalidate_dashboard_cache()
    return response


store = CouchbaseStore()
embeddings: ToolEmbeddings | None = None
ready = False
last_hijack_scan_at: str | None = None

# Startup steps that failed but were not fatal (see startup() below). Empty
# on a clean boot; each entry is one human-readable "step: reason". Reported
# by /api/health so the reason a degraded appliance is degraded is visible in
# the dashboard, rather than only in `docker logs`.
startup_failures: list[str] = []

# ---------------------------------------------------------------------------
# Short-lived response cache for the dashboard's heavy read endpoints.
#
# The Dashboard, Topology, Insights, Threat Detection, Traces, LLM Caching
# and Context Caching pages each run one or more full-window GROUP BY
# aggregates over the event logs. At demo-traffic volumes (hundreds of
# thousands of events per 24h) those scans cost seconds, and every open tab
# re-runs them on every 30s poll and every navigation. Serving the same
# answer for DASHBOARD_CACHE_SECONDS (default 10s) makes page navigation
# instant after the first load, and concurrent requests for the same view
# share one in-flight computation instead of each starting their own scan.
# Any dashboard write (purge, sweep, settings change - see
# require_dashboard_session) drops the cache so an operator never sees
# pre-change numbers right after clicking something. Set to 0 to disable.
# ---------------------------------------------------------------------------
DASHBOARD_CACHE_SECONDS = float(os.getenv("DASHBOARD_CACHE_SECONDS", "10"))
_response_cache: dict[tuple, tuple[float, int, Any]] = {}
_response_inflight: dict[tuple, "asyncio.Future"] = {}
_response_cache_generation = 0


def invalidate_dashboard_cache() -> None:
    global _response_cache_generation
    _response_cache_generation += 1
    _response_cache.clear()


def cached_dashboard_response(fn):
    """Cache a read-only GET route's result for DASHBOARD_CACHE_SECONDS,
    keyed by the route and its query parameters. functools.wraps keeps the
    original signature visible to FastAPI, so query-param parsing is
    unchanged."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if DASHBOARD_CACHE_SECONDS <= 0:
            return await fn(*args, **kwargs)
        key = (fn.__name__, tuple(sorted(kwargs.items())))
        cached = _response_cache.get(key)
        if cached and cached[0] > time.monotonic() and cached[1] == _response_cache_generation:
            return cached[2]
        pending = _response_inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        generation = _response_cache_generation
        task = asyncio.ensure_future(fn(*args, **kwargs))
        _response_inflight[key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if _response_inflight.get(key) is task:
                _response_inflight.pop(key, None)
        if generation == _response_cache_generation:
            now = time.monotonic()
            if len(_response_cache) >= 256:
                # Keys include query parameters, so the key space is
                # caller-controlled; drop expired entries (and, failing
                # that, everything) rather than let it grow for the life of
                # the process.
                for stale in [k for k, v in _response_cache.items() if v[0] <= now]:
                    _response_cache.pop(stale, None)
                if len(_response_cache) >= 256:
                    _response_cache.clear()
            _response_cache[key] = (now + DASHBOARD_CACHE_SECONDS, generation, result)
        return result

    return wrapper

# LLM response caching for agents (see app/llm_cache.py). The policy is
# user-editable from the LLM Caching page and persisted in Couchbase, so it
# is loaded on startup and kept in memory for the hot path - a cache lookup
# should never cost an extra round-trip just to read its own settings.
LLM_CACHE_SETTINGS_DOC = "settings::llm_cache"
llm_config: dict = llm_cache.normalize_config(LLM_CACHE_DEFAULTS)
llm_config_version: str = llm_cache.config_fingerprint(llm_config)
llm_catalog_version: str = ""
last_llm_sweep_at: str | None = None

# Context caching for agents (see app/context_cache.py). Same load-once
# convention as the LLM cache policy above - user-editable from the
# Context Cache page, persisted in Couchbase, kept in memory for the hot
# path so a context_get()/context_set() call never costs an extra
# round-trip just to read its own settings.
CONTEXT_CACHE_SETTINGS_DOC = "settings::context_cache"
context_config: dict = context_cache.normalize_config(CONTEXT_CACHE_DEFAULTS)
last_context_sweep_at: str | None = None
CONTEXT_INLINE_EVICTION_INTERVAL_SECONDS = float(os.getenv("CONTEXT_INLINE_EVICTION_INTERVAL_SECONDS", "30"))
_last_inline_context_eviction: float = 0.0

# RAG Applications (see app/rag_apps.py). Registered apps live in one
# settings document and are kept in memory for the query hot path, the same
# load-once convention as the cache policies above. knowledge_generation is
# part of every cached-retrieval key and is bumped on any Knowledge Base
# change; seeding it from the clock keeps keys from a previous process run
# from ever being mistaken for current ones.
RAG_APPS_SETTINGS_DOC = "settings::rag_apps"

# Knowledge sets (see app/knowledge_sets.py and app/embedding_models.py):
# one embedding model and one vector index per set. The built-in "default"
# set is synthesized from EMBEDDING_CONFIG rather than stored, so it always
# matches the model the appliance actually loaded.
KNOWLEDGE_SETS_SETTINGS_DOC = "settings::knowledge_sets"
knowledge_sets_registry: dict = {}
_set_embedders: dict = {}
rag_apps_registry: dict = {}
knowledge_generation: int = int(time.time())

# Local dashboard login (see app/user_auth.py). The LDAP policy is
# user-editable from Settings -> LDAP Authentication and persisted in
# Couchbase at settings::ldap, loaded into memory here the same way the LLM
# cache policy is - a login attempt should never cost an extra Couchbase
# round-trip just to find out whether LDAP is even enabled.
ldap_config: dict = user_auth.normalize_ldap_config(None)

# SIEM/log-forwarding destinations (see app/siem_forwarding.py). Same
# load-once-into-memory convention as ldap_config/llm_config above - the
# hot path here is every single audit log write (couchbase_client.log_access),
# so it must never cost a Couchbase round-trip just to find out whether
# forwarding is even enabled.
siem_config: dict = siem_forwarding.normalize_config(None)

# Rate limits, budgets and timeouts (see app/governance.py). Same
# load-once-into-memory convention as the three configs above, and for the
# sharpest version of the same reason: this one is read on *every*
# discover/invoke/complete, so a Couchbase round-trip to find out whether a
# limit applies would cost more than the limit saves.
governance_config: dict = governance.normalize_config(None)

# Input guardrails and PII policy (see app/guardrails.py). Read on every
# prompt and every tool invocation, so it is held in memory for the same
# reason the limits policy is.
guardrails_config: dict = guardrails.normalize_config(None)

# The human approval tier (see app/approvals.py). Off by default: parking
# calls for review in front of agents nobody has told is a way to look like
# an outage, so an operator turns it on deliberately.
approvals_config: dict = approvals.normalize_config(None)

# Memory consolidation (see app/memory_consolidation.py). Read on every
# memory write and recall, and by the background pass.
memory_config: dict = memory_consolidation.normalize_config(None)

# Federated agent identity (see app/agent_oidc.py). Read on any request
# carrying a JWT rather than an API key.
agent_oidc_config: dict = agent_oidc.normalize_config(None)
last_consolidation_at: str | None = None
last_consolidation_report: dict | None = None

# Set by the catalog-change trigger and read by the Evaluations page, so an
# operator can see that a gate ran without going looking for the run.
last_eval_gate_at: str | None = None


async def _startup_step(label: str, awaitable) -> bool:
    """Run one startup step; record and continue if it fails.

    Everything in startup()'s `if store.connected:` block reads or writes
    Couchbase, and any one of those raising used to propagate straight out of
    the startup event - to which uvicorn's answer is "Application startup
    failed. Exiting." Behind `restart: unless-stopped` that is a crash loop:
    the API never binds, so the dashboard cannot load to say what happened,
    and the actual reason (a bucket at its RAM quota returning
    key_value_temporary_failure on every write, in the case this was written
    for) is visible only to someone who thinks to run `docker logs`.

    An appliance that cannot reach its store is degraded, not dead. Coming up
    anyway means /api/health answers, the login page's API answers, and
    startup_failures says why - which is the difference between a five-minute
    diagnosis and an afternoon of guessing.
    """
    try:
        await awaitable
        return True
    except Exception as exc:  # noqa: BLE001
        detail = f"{label}: {exc}"
        startup_failures.append(detail)
        logger.error(
            "Startup step failed - continuing in degraded mode (%s). "
            "The appliance will serve /api/health and the login API, but this "
            "step's data is missing or stale until it succeeds.",
            detail,
        )
        return False


@app.on_event("startup")
async def startup():
    global embeddings, ready

    # Replace asyncio's default executor before anything schedules work on
    # it. Every Couchbase call in this app is an `asyncio.to_thread(...)`,
    # and the default pool is sized off CPU count (min(32, cpu+4) - often
    # just 8 in a container) even though this work is almost entirely I/O
    # wait. Under sustained dashboard polling that pool saturates and the
    # unbounded queue behind it never drains, which is what turns a slow
    # appliance into an unreachable one. See config.WORKER_THREADS.
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=WORKER_THREADS, thread_name_prefix="aom-worker"
        )
    )
    logger.info("Worker thread pool sized to %d thread(s)", WORKER_THREADS)

    logger.info("Loading local embedding model...")
    embeddings = ToolEmbeddings(EMBEDDING_CONFIG["model_name"])

    if AUTH_SECRET_KEY == "dev-only-insecure-secret-change-me":
        logger.warning(
            "AUTH_SECRET_KEY is unset - using the insecure built-in default. Dashboard login sessions and any "
            "stored LDAP bind password are only as safe as that well-known string. Set AUTH_SECRET_KEY (start.sh "
            "does this for you automatically) before relying on this outside local evaluation."
        )

    if COUCHBASE_CONFIG["password"] == "CouchbaseDemo123!":
        logger.warning(
            "COUCHBASE_PASSWORD is unset - using the well-known bundled demo password. Anyone who has ever read "
            "this project's README or .env.example knows it. Set COUCHBASE_USERNAME/COUCHBASE_PASSWORD to a real, "
            "unique credential (PCI DSS 4.0 Req. 8.3.1 / CIS 'no default credentials') before relying on this "
            "outside local evaluation - see .env.example."
        )

    logger.info("Connecting to Couchbase...")
    # A few attempts only: uvicorn does not answer anything - health checks
    # included - until this startup event returns, so a long blocking retry
    # loop here is what gets the container killed by its liveness probe
    # while Couchbase is still warming up. If Couchbase is not reachable yet,
    # come up degraded and keep retrying in the background instead.
    await store.connect(retries=STARTUP_CONNECT_ATTEMPTS)

    if store.connected:
        await couchbase_startup()
    else:
        startup_failures.append(
            f"{COUCHBASE_UNAVAILABLE_PREFIX}: not reachable yet - retrying every "
            f"{int(COUCHBASE_RECONNECT_INTERVAL_SECONDS)}s in the background"
        )
        spawn(couchbase_reconnect_loop())

    ready = True
    if startup_failures:
        logger.error(
            "Operations manager started DEGRADED - %d startup step(s) failed: %s",
            len(startup_failures), "; ".join(startup_failures),
        )
    else:
        logger.info("Operations manager ready (couchbase_connected=%s)", store.connected)


STARTUP_CONNECT_ATTEMPTS = int(os.getenv("COUCHBASE_STARTUP_CONNECT_ATTEMPTS", "3"))
COUCHBASE_RECONNECT_INTERVAL_SECONDS = float(os.getenv("COUCHBASE_RECONNECT_INTERVAL_SECONDS", "15"))
COUCHBASE_UNAVAILABLE_PREFIX = "couchbase connection"


async def couchbase_reconnect_loop():
    """Keep trying to reach Couchbase after a startup that could not.

    Previously a startup that exhausted its connect retries stayed
    disconnected for the life of the process - every page empty, every
    agent call failing - until someone restarted the container by hand,
    even though /api/health kept reporting 200. That is the normal
    situation after a node reboot, when Couchbase can take minutes to warm
    up a large bucket. Once connected, the rest of startup runs exactly as
    it would have."""
    while not store.connected:
        await asyncio.sleep(COUCHBASE_RECONNECT_INTERVAL_SECONDS)
        try:
            await store.connect(retries=1, delay_seconds=0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Background Couchbase reconnect failed: %s", exc)
    startup_failures[:] = [f for f in startup_failures if not f.startswith(COUCHBASE_UNAVAILABLE_PREFIX)]
    logger.info("Couchbase reachable - completing deferred startup")
    await couchbase_startup()


async def couchbase_startup():
    """Everything in startup that needs Couchbase: seeding, loading the
    stored policies, catalog ingest, and starting the background loops."""
    # Every await below touches Couchbase, and none of them is worth
    # refusing to start over - see _startup_step for why that used to
    # happen and what it cost.
    await _startup_step("seed demo agents", seed_demo_agents())

    await _startup_step("seed default admin account", seed_default_admin())
    await _startup_step("load LDAP config", load_ldap_config())
    await _startup_step("load SIEM config", load_siem_config())
    await _startup_step("load governance policy", load_governance_config())
    await _startup_step("load guardrails policy", load_guardrails_config())
    await _startup_step("load approvals policy", load_approvals_config())
    await _startup_step("load memory config", load_memory_config())
    await _startup_step("load agent OIDC config", load_agent_oidc_config())
    set_siem_config_provider(lambda: siem_config)
    # Ingestion needs the downstream timeout, and only the governance
    # policy knows it. A provider callable rather than a value so an
    # operator raising the timeout takes effect on the next ingest, not
    # the next restart.
    catalog_ingest.set_governance_provider(lambda: governance_config)

    await _startup_step("seed sample MCP servers", seed_servers(store, SAMPLE_MCP_SERVERS_BASE_URL))

    # Catalog ingest calls every registered MCP server, and one that is down
    # or slow costs its full downstream timeout. Awaited here, a few of
    # those kept uvicorn from answering its own health check for long
    # enough to be restarted by the liveness probe - and then to do the same
    # again on the next start. The stored catalog already serves discovery,
    # so the refresh runs in the background instead.
    spawn(_startup_catalog_ingest())
    spawn(hijack_monitor_loop())

    await _startup_step("load LLM cache config", load_llm_config())
    await _startup_step("refresh catalog version", refresh_catalog_version())
    spawn(llm_cache_sweeper_loop())

    await _startup_step("load context cache config", load_context_config())
    spawn(context_cache_sweeper_loop())

    await _startup_step("load imported embedding models", load_custom_embedding_models())
    await _startup_step("load knowledge sets", load_knowledge_sets())
    await _startup_step("load RAG applications", load_rag_apps())

    if EVAL_SEED_STARTER_DATASET:
        await _startup_step("seed starter eval dataset", seed_starter_dataset())

    spawn(memory_consolidation_loop())


async def _startup_catalog_ingest():
    global last_hijack_scan_at
    existing = await store.count_tools()
    logger.info("Ingesting registered/trusted MCP server catalogs into Couchbase (currently %d tool doc(s) stored)...", existing)
    if await _startup_step("ingest MCP tool catalogs", ingest_all(store, embeddings)):
        # ingest_all already ran the metadata-poisoning scan against every
        # tool it (re-)ingested, so this counts as the first monitor pass.
        # Only on success: a timestamp claiming a scan that never ran
        # would make the Threat Detection page lie about its own freshness.
        last_hijack_scan_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


    await _startup_step("refresh catalog version", refresh_catalog_version())


async def hijack_monitor_loop():
    """The MCP Tool Hijacking monitor: on a fixed interval, re-scan every
    already-ingested tool's stored description against the current pattern
    bank, with no MCP round-trip (see catalog_ingest.rescan_all_tools).
    This is what catches a tool ingested before hijack detection existed,
    or before a pattern-bank update - the ingest-time scan alone only ever
    sees each tool once, at the moment it's (re-)ingested."""
    global last_hijack_scan_at
    while True:
        await asyncio.sleep(HIJACK_SCAN_INTERVAL_MINUTES * 60)
        if not store.connected:
            continue
        try:
            changed = await rescan_all_tools(store)
            last_hijack_scan_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if changed:
                logger.info("Hijack monitor: %d tool(s) changed trust/hijack status on this pass", changed)
        except Exception as exc:  # noqa: BLE001
            logger.error("Hijack monitor pass failed: %s", exc)


async def seed_demo_agents():
    """Provision the three demo identities as real agent records.

    They exist so the appliance works on first boot, and they are published
    in the README - so the one thing they must not be is indistinguishable
    from a credential somebody deliberately issued. Recording them as
    agents marked `seeded` means they appear on the Agent Identities page
    with that label and a revoke button, rather than being invisible
    strings that can only be removed by editing `.env`.

    Idempotent on the key hash: an operator who revokes or deletes a demo
    agent does not get it back on the next restart.
    """
    for api_key, role in SEED_API_KEYS.items():
        existing = await store.get_key_doc(api_key)
        if existing and existing.get("doc_type") == "agent_key":
            continue
        if existing and existing.get("_seed_removed"):
            continue

        agent = agent_identity.build_agent(
            name=f"Demo {role}",
            role=role,
            owner="bundled sample",
            description=(
                "Seeded so the appliance is usable on first boot. This key is published in the "
                "README and .env.example - revoke it before this deployment is reachable by "
                "anything you care about."
            ),
            created_by="system",
            seeded=True,
        )
        key_doc = agent_identity.build_key_doc(agent=agent, api_key=api_key, label="seeded")
        agent["keys"].append({
            "key_hash": agent_identity.hash_key(api_key),
            "key_prefix": key_doc["key_prefix"],
            "status": "active",
            "created_at": key_doc["created_at"],
            "expires_at": None,
            "label": "seeded",
        })
        await store.upsert_key_doc(api_key, key_doc)
        await store.upsert_agent(agent)
        logger.info("Seeded demo agent '%s' (role=%s)", agent["name"], role)


async def seed_default_admin():
    """Provision the built-in `admin` account on first boot, with no
    password set yet - see POST /v1/auth/bootstrap. Idempotent: does
    nothing once that account already exists, so it never resets a
    password an operator has already chosen."""
    existing = await store.get_user(DEFAULT_ADMIN_USERNAME)
    if existing:
        return
    doc = user_auth.new_local_user_doc(role="admin", password_hash=None, source="local", must_change_password=True)
    await store.upsert_user(DEFAULT_ADMIN_USERNAME, doc)
    logger.info("Seeded default local account '%s' - password not yet set (first login sets it).", DEFAULT_ADMIN_USERNAME)


def _warn_if_ldap_tls_unverified(cfg: dict) -> None:
    """LDAPS/StartTLS with no corporate CA configured means ldap3 does not
    validate the directory's certificate at all (see
    user_auth.ldap_authenticate) - functionally equivalent to skipping TLS
    verification, which leaves the bind vulnerable to an on-path MITM
    presenting any certificate. Not escalated to a hard failure here: that
    would break existing working deployments the moment this code shipped,
    for admins who never had a reason to think about this. A loud warning
    at every load/save is the honest middle ground (NIST SC-8, PCI DSS 4.0
    Req. 4.2.1) - see also Settings -> LDAP Authentication for uploading one."""
    if cfg.get("enabled") and (cfg.get("use_ssl") or cfg.get("start_tls")) and not (cfg.get("ca_certificate") or "").strip():
        logger.warning(
            "LDAP is configured for LDAPS/StartTLS but no corporate CA certificate is installed - the directory "
            "server's certificate is NOT being validated (any certificate is accepted), which is vulnerable to an "
            "on-path attacker. Upload your directory's CA certificate under Settings -> LDAP Authentication."
        )


async def load_ldap_config():
    global ldap_config
    stored = await store.get_setting(LDAP_SETTINGS_DOC)
    ldap_config = user_auth.normalize_ldap_config(stored.get("config") if stored else None)
    logger.info("LDAP config loaded (enabled=%s host=%s)", ldap_config["enabled"], ldap_config["host"] or "-")
    _warn_if_ldap_tls_unverified(ldap_config)


async def save_ldap_config(cfg: dict):
    global ldap_config
    ldap_config = user_auth.normalize_ldap_config(cfg)
    await store.upsert_setting(
        LDAP_SETTINGS_DOC,
        {
            "doc_type": "settings",
            "setting_id": "ldap",
            "config": ldap_config,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    _warn_if_ldap_tls_unverified(ldap_config)


async def load_siem_config():
    global siem_config
    stored = await store.get_setting(SIEM_SETTINGS_DOC)
    siem_config = siem_forwarding.normalize_config(stored.get("config") if stored else None)
    enabled = [v for v, c in siem_config.items() if c.get("enabled")]
    logger.info("SIEM forwarding config loaded (enabled destinations: %s)", ", ".join(enabled) or "none")


async def save_siem_config(cfg: dict):
    global siem_config
    siem_config = siem_forwarding.normalize_config(cfg)
    await store.upsert_setting(
        SIEM_SETTINGS_DOC,
        {
            "doc_type": "settings",
            "setting_id": "siem",
            "config": siem_config,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def load_governance_config():
    global governance_config
    stored = await store.get_setting(GOVERNANCE_SETTINGS_DOC)
    governance_config = governance.normalize_config(stored.get("config") if stored else None)
    if not stored:
        await save_governance_config(governance_config)
    logger.info(
        "Limits policy loaded (enabled=%s enforce=%s requests/min=%s tool-calls/min=%s tokens/hr=%s spend/day=$%s)",
        governance_config["enabled"], governance_config["enforce"],
        governance_config["requests_per_minute"], governance_config["tool_calls_per_minute"],
        governance_config["tokens_per_hour"], governance_config["spend_per_day_usd"],
    )
    if governance_config["enabled"] and not governance_config["enforce"]:
        logger.info(
            "Limits are being measured, not enforced - every ceiling is evaluated and recorded, nothing is "
            "blocked. Turn on 'enforce' under Settings -> Limits & Budgets once the recorded usage shows "
            "what normal traffic actually consumes."
        )


async def save_governance_config(cfg: dict):
    global governance_config
    governance_config = governance.normalize_config(cfg)
    await store.upsert_setting(
        GOVERNANCE_SETTINGS_DOC,
        {
            "doc_type": "settings",
            "setting_id": "governance",
            "config": governance_config,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def load_guardrails_config():
    global guardrails_config
    stored = await store.get_setting(GUARDRAILS_SETTINGS_DOC)
    guardrails_config = guardrails.normalize_config(stored.get("config") if stored else None)
    if not stored:
        await save_guardrails_config(guardrails_config)
    logger.info(
        "Guardrails policy loaded (enabled=%s detectors=%s block_injection_at=%s never_cache_pii=%s)",
        guardrails_config["enabled"], ",".join(guardrails_config["detectors"]) or "none",
        guardrails_config["block_injection_at"], guardrails_config["never_cache_pii"],
    )


async def save_guardrails_config(cfg: dict):
    global guardrails_config
    guardrails_config = guardrails.normalize_config(cfg)
    await store.upsert_setting(
        GUARDRAILS_SETTINGS_DOC,
        {
            "doc_type": "settings",
            "setting_id": "guardrails",
            "config": guardrails_config,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def load_approvals_config():
    global approvals_config
    stored = await store.get_setting(APPROVALS_SETTINGS_DOC)
    approvals_config = approvals.normalize_config(stored.get("config") if stored else None)
    if not stored:
        await save_approvals_config(approvals_config)
    logger.info(
        "Approval tier loaded (enabled=%s threshold=%s explicit=%d ttl=%ss)",
        approvals_config["enabled"], approvals_config["require_at_risk_level"],
        len(approvals_config["require_for_tools"]), approvals_config["ttl_seconds"],
    )


async def save_approvals_config(cfg: dict):
    global approvals_config
    approvals_config = approvals.normalize_config(cfg)
    await store.upsert_setting(
        APPROVALS_SETTINGS_DOC,
        {
            "doc_type": "settings",
            "setting_id": "approvals",
            "config": approvals_config,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def load_memory_config():
    global memory_config
    stored = await store.get_setting(MEMORY_SETTINGS_DOC)
    memory_config = memory_consolidation.normalize_config(stored.get("config") if stored else None)
    if not stored:
        await save_memory_config(memory_config)
    logger.info(
        "Memory consolidation loaded (enabled=%s dedup=%s@%.3f rollup=%s every %sm)",
        memory_config["enabled"], memory_config["dedup_enabled"],
        memory_config["dedup_similarity_threshold"], memory_config["rollup_enabled"],
        memory_config["interval_minutes"],
    )


async def save_memory_config(cfg: dict):
    global memory_config
    memory_config = memory_consolidation.normalize_config(cfg)
    await store.upsert_setting(
        MEMORY_SETTINGS_DOC,
        {
            "doc_type": "settings",
            "setting_id": "memory",
            "config": memory_config,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def load_agent_oidc_config():
    global agent_oidc_config
    stored = await store.get_setting(AGENT_OIDC_SETTINGS_DOC)
    agent_oidc_config = agent_oidc.normalize_config(stored.get("config") if stored else None)
    if not stored:
        await save_agent_oidc_config(agent_oidc_config)
    if agent_oidc_config["enabled"]:
        problems = agent_oidc.config_problems(agent_oidc_config)
        if problems:
            logger.warning("Agent JWT authentication is enabled but incomplete: %s", "; ".join(problems))
        else:
            logger.info("Agent JWT authentication enabled (issuer=%s)", agent_oidc_config["issuer"])


async def save_agent_oidc_config(cfg: dict):
    global agent_oidc_config
    agent_oidc_config = agent_oidc.normalize_config(cfg)
    # Drop cached JWKS clients so a corrected URI takes effect now rather
    # than after the old client's lifespan expires.
    agent_oidc.reset_clients()
    await store.upsert_setting(
        AGENT_OIDC_SETTINGS_DOC,
        {
            "doc_type": "settings",
            "setting_id": "agent_oidc",
            "config": agent_oidc_config,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


AGENT_TOUCH_INTERVAL_SECONDS = float(os.getenv("AGENT_TOUCH_INTERVAL_SECONDS", "60"))
_agent_last_touch: dict[str, float] = {}


async def resolve_caller(credential: str) -> tuple[dict | None, str | None]:
    """Turn a bearer credential into an identity, whatever kind it is.

    One path for both API keys and federated JWTs, so everything downstream
    - rate limits, scoping, tracing, the audit log - sees the same shape and
    cannot end up applying to one kind of caller and not the other.

    Returns (identity, refusal_reason).
    """
    if not credential:
        return None, "no credential supplied"

    if agent_oidc.looks_like_jwt(credential):
        if not agent_oidc_config.get("enabled"):
            return None, "a JWT was presented but JWT authentication is not enabled"
        return await asyncio.to_thread(
            agent_oidc.validate, credential, agent_oidc_config, set(ROLES)
        )

    key_doc = await store.get_key_doc(credential)
    identity, reason = agent_identity.resolve(key_doc)
    if identity and not identity.get("legacy"):
        # "Last used" is what tells an operator whether a key can safely be
        # turned off. Off the response path, and a failure here costs a
        # stale timestamp rather than a failed call. At most once per agent
        # per AGENT_TOUCH_INTERVAL_SECONDS: it is a N1QL UPDATE, and running
        # one per authenticated request put a query-service round-trip on
        # every agent call for a timestamp nobody needs to the second.
        agent_id = identity["agent_id"]
        now = time.monotonic()
        if now - _agent_last_touch.get(agent_id, 0.0) >= AGENT_TOUCH_INTERVAL_SECONDS:
            _agent_last_touch[agent_id] = now
            if len(_agent_last_touch) > 10000:
                _agent_last_touch.clear()
            spawn(store.touch_agent(agent_id))
    return identity, reason


async def sync_agent_keys(agent: dict, ttl_seconds: int = 0):
    """Write an agent's role, scope and status through to its key
    documents.

    Authentication reads only the key document, so a change to the agent
    that did not reach its keys would be a change that quietly did not
    happen - a revoked agent whose keys still worked is the exact failure
    this module exists to prevent.
    """
    for key in agent.get("keys") or []:
        key_hash = key.get("key_hash")
        if not key_hash:
            continue
        doc = await store.get_key_doc_by_hash(key_hash)
        if not doc:
            continue
        doc["role"] = agent["role"]
        doc["allowed_tools"] = agent.get("allowed_tools") or []
        doc["agent_status"] = agent.get("status", "active")
        doc["agent_name"] = agent.get("name")
        doc["status"] = key.get("status", "active")
        doc["expires_at"] = key.get("expires_at") or agent.get("expires_at")
        await store.upsert_key_doc_by_hash(key_hash, doc, ttl_seconds=ttl_seconds)


async def memory_consolidation_loop():
    """The background pass. Same shape as the hijack monitor and the cache
    sweeper: a timer, a guard on connectivity, and errors logged rather
    than allowed to kill the task."""
    while True:
        await asyncio.sleep(max(300, int(memory_config.get("interval_minutes", 60)) * 60))
        if not store.connected or not memory_config.get("enabled"):
            continue
        try:
            report = await run_memory_consolidation(trigger="scheduled")
            if report["duplicates_merged"] or report["sessions_rolled_up"]:
                logger.info(
                    "Memory consolidation: merged %d duplicate(s), rolled up %d session(s), superseded %d entr(ies)",
                    report["duplicates_merged"], report["sessions_rolled_up"], report["entries_superseded"],
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Memory consolidation pass failed: %s", exc)


async def governed_completion(prompt: str, *, purpose: str) -> tuple[str, bool]:
    """Run a completion the appliance itself needs, through the appliance's
    own gateway rather than around it.

    Returns (text, cache_hit). Consolidation is the first internal caller,
    and routing it here is deliberate: the summary is cached like any other
    answer, costs are recorded against a named internal subject, and the
    call appears in the audit log. A memory layer that called a provider
    directly would be spending money outside the component this product
    sells as the control point - the exact objection AOM exists to answer,
    reintroduced from the inside.
    """
    cfg = dict(llm_config)
    subject = f"system::{purpose}"
    scope = llm_cache.scope_key(cfg, "admin", subject)
    eid = llm_cache.entry_id(cfg, cfg["provider"], cfg["model"], prompt, {}, scope)

    hit, _similarity, _reason = await _lookup_cache(cfg, cfg["provider"], cfg["model"], scope, eid, prompt)
    if hit:
        await store.log_llm_event({
            "doc_type": "llm_cache_event",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outcome": "hit_exact", "provider": cfg["provider"], "model": cfg["model"],
            "role": "admin", "subject": subject, "entry_id": hit["entry_id"],
            "namespace": cfg["namespace"], "scope_key": scope,
            "prompt_preview": redact_text_for_storage(prompt.strip()[:240]),
            "total_tokens": hit.get("total_tokens", 0), "tokens_saved": hit.get("total_tokens", 0),
            "cost_usd": 0.0, "cost_saved_usd": hit.get("cost_usd", 0.0), "latency_ms": 0,
            "reason": f"internal {purpose} served from cache",
        })
        return hit.get("response", ""), True

    started = time.time()
    result = await asyncio.to_thread(
        llm_cache.call_provider, cfg["provider"], cfg["model"], prompt, cfg, LLM_API_KEYS,
        int(governance_config.get("llm_timeout_seconds") or 60),
    )
    latency_ms = int((time.time() - started) * 1000)
    prompt_tokens = int(result["prompt_tokens"])
    completion_tokens = int(result["completion_tokens"])
    cost_usd = llm_cache.estimate_cost_usd(cfg["model"], prompt_tokens, completion_tokens)

    await _store_cache_entry(
        eid, cfg, cfg["provider"], cfg["model"], scope, prompt, result,
        prompt_tokens, completion_tokens, cost_usd, latency_ms,
    )
    await record_usage("admin", subject, tokens=prompt_tokens + completion_tokens, cost_usd=cost_usd)
    await store.log_llm_event({
        "doc_type": "llm_cache_event",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "outcome": "miss", "provider": cfg["provider"], "model": cfg["model"],
        "role": "admin", "subject": subject, "entry_id": eid,
        "namespace": cfg["namespace"], "scope_key": scope,
        "prompt_preview": redact_text_for_storage(prompt.strip()[:240]),
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": cost_usd, "latency_ms": latency_ms,
        "reason": f"internal {purpose}",
    })
    return result["text"], False


async def run_memory_consolidation(trigger: str = "manual", user_id: str | None = None) -> dict:
    """One consolidation pass: dedup, then rollup, then rescore importance.

    Ordered that way on purpose. Dedup first, so rollup summarizes facts
    rather than repetitions of them; importance last, so it scores the
    entries that actually survived.
    """
    global last_consolidation_at, last_consolidation_report
    cfg = memory_config
    report = memory_consolidation.empty_report()
    now = time.time()

    if user_id:
        users = [{"user_id": user_id}]
    else:
        users = await store.list_memory_users(limit=int(cfg["max_users_per_pass"]))

    superseded_ttl = int(cfg["retain_superseded_hours"]) * 3600

    for row in users:
        uid = row.get("user_id")
        if not uid:
            continue
        report["users_examined"] += 1
        entries = await store.list_memory_with_embeddings(uid)
        if not entries:
            continue

        # ---- 1. dedup ----------------------------------------------------
        if cfg.get("dedup_enabled"):
            # Types queued for rollup are left alone here - summarizing a
            # session is the right treatment for a transcript, and deduping
            # it first would collapse it below the rollup threshold.
            reserved = set(cfg.get("rollup_memory_types") or []) if cfg.get("rollup_enabled") else set()
            for group in memory_consolidation.find_duplicate_groups(
                entries, float(cfg["dedup_similarity_threshold"]), exclude_types=reserved
            ):
                try:
                    survivor = memory_consolidation.merge_group(group, now)
                    survivor_id = survivor["memory_id"]
                    await store.upsert_memory(survivor_id, _memory_write_doc(survivor))
                    for source in group:
                        if source.get("memory_id") == survivor_id:
                            continue
                        await store.upsert_memory(
                            source["memory_id"],
                            _memory_write_doc(memory_consolidation.mark_superseded(source, survivor_id, now)),
                            ttl_seconds=superseded_ttl,
                        )
                        report["entries_superseded"] += 1
                        report["duplicates_merged"] += 1
                    report["groups_merged"] += 1
                except Exception as exc:  # noqa: BLE001
                    report["errors"].append(f"dedup for {uid}: {exc}")
            entries = await store.list_memory_with_embeddings(uid)

        # ---- 2. rollup ---------------------------------------------------
        if cfg.get("rollup_enabled") and embeddings is not None:
            for session_id, group in memory_consolidation.find_rollup_sessions(entries, cfg, now):
                try:
                    prompt = memory_consolidation.build_rollup_prompt(group)
                    summary, cached = await governed_completion(prompt, purpose="memory-consolidation")
                    if cached:
                        report["cache_hits"] += 1
                    else:
                        report["llm_calls"] += 1
                    if not (summary or "").strip():
                        report["errors"].append(f"rollup for {uid}/{session_id}: empty summary")
                        continue
                    if guardrails_config.get("enabled") and guardrails_config.get("redact_memory"):
                        summary = redact_text_for_storage(summary)

                    doc = memory_consolidation.build_rollup_doc(
                        entries=group, summary=summary,
                        embedding=await embeddings.embed_async(summary), session_id=session_id, now=now,
                    )
                    rollup_id = agent_memory.new_memory_id(doc["user_id"])
                    doc["importance"] = memory_consolidation.score_importance(doc, now)
                    await store.upsert_memory(rollup_id, doc)
                    for source in group:
                        await store.upsert_memory(
                            source["memory_id"],
                            _memory_write_doc(memory_consolidation.mark_superseded(source, rollup_id, now)),
                            ttl_seconds=superseded_ttl,
                        )
                        report["entries_superseded"] += 1
                    report["sessions_rolled_up"] += 1
                except Exception as exc:  # noqa: BLE001
                    report["errors"].append(f"rollup for {uid}/{session_id}: {exc}")
            entries = await store.list_memory_with_embeddings(uid)

        # ---- 3. importance -----------------------------------------------
        if cfg.get("importance_enabled"):
            scores = {
                e["memory_id"]: memory_consolidation.score_importance(e, now)
                for e in entries if e.get("memory_id")
            }
            report["importance_updated"] += await store.set_memory_importance(scores)

    last_consolidation_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    last_consolidation_report = {**report, "trigger": trigger, "finished_at": last_consolidation_at}
    return report


def _memory_write_doc(entry: dict) -> dict:
    """Strip the synthetic `memory_id` that the N1QL views add via
    META().id before writing a document back - it is the key, not a field,
    and letting it into the body means every rewrite grows the document
    with a duplicate of its own ID."""
    return {k: v for k, v in entry.items() if k != "memory_id"}


def redact_for_storage(value):
    """Redact anything about to be persisted or traced. One helper so a new
    write path cannot quietly skip it - the caller's own response is never
    passed through here."""
    if not guardrails_config.get("enabled"):
        return value
    return guardrails.redact_structure(value, guardrails_config)


def redact_text_for_storage(text: str | None) -> str | None:
    if text is None or not guardrails_config.get("enabled"):
        return text
    return guardrails.redact(text, guardrails_config)


async def seed_starter_dataset():
    """Provision the bundled evaluation dataset on first boot, and only
    then - once it exists, an operator's edits (or its deletion) stick
    across restarts, the same way a seeded sample server does."""
    existing = await store.get_dataset(evals.STARTER_DATASET["dataset_id"])
    if existing:
        return
    await store.upsert_dataset(evals.normalize_dataset(dict(evals.STARTER_DATASET)))
    logger.info("Seeded starter evaluation dataset '%s'", evals.STARTER_DATASET["dataset_id"])


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
# Every gateway operation writes a span. A caller that sets the trace
# headers gets its calls stitched into one run; a caller that sets nothing
# still gets a complete single-request trace, because the alternative -
# tracing only well-behaved clients - would leave exactly the traffic worth
# investigating untraced.
def trace_context(request: Request | None) -> dict:
    if request is None:
        return {"trace_id": tracing.new_trace_id(), "parent_span_id": None,
                "agent_id": None, "session_id": None, "root": True}
    return tracing.trace_context_from_headers(request.headers)


def _trace_sampled(trace_id: str) -> bool:
    """Deterministic per-trace sampling decision (see TRACE_SAMPLE_RATE), so
    every span of one run gets the same answer."""
    if TRACE_SAMPLE_RATE >= 1.0:
        return True
    if TRACE_SAMPLE_RATE <= 0.0:
        return False
    bucket = int(hashlib.sha256((trace_id or "").encode("utf-8")).hexdigest()[:8], 16)
    return bucket / 0xFFFFFFFF < TRACE_SAMPLE_RATE


async def record_span(
    ctx: dict,
    *,
    kind: str,
    name: str,
    started: float,
    ended: float | None = None,
    status: str = "ok",
    role: str | None = None,
    subject: str | None = None,
    attributes: dict | None = None,
    error: str | None = None,
) -> str:
    """Write one span and fold it into its run summary. Returns the span ID
    so a caller can parent the next span to it.

    Tracing is strictly best-effort: a failure here is logged and swallowed
    rather than propagated, because a gateway that starts refusing tool
    calls when its own observability has a bad day is worse than one with a
    gap in its traces.

    The run summary is a read-modify-write, so two spans of the same run
    landing in the same instant can lose one increment. That is the right
    trade here - the alternative is a per-run lock on the hot path, and the
    summary is a navigational aid whose authoritative source (the spans
    themselves) is never lossy.
    """
    if not TRACING_ENABLED or not store.connected or store.traces is None:
        return ""
    if status != "error" and not _trace_sampled(ctx["trace_id"]):
        return ""
    try:
        span = tracing.build_span(
            trace_id=ctx["trace_id"],
            kind=kind,
            name=name,
            started_at=started,
            ended_at=ended,
            status=status,
            parent_span_id=ctx.get("parent_span_id"),
            role=role,
            subject=subject,
            agent_id=ctx.get("agent_id"),
            session_id=ctx.get("session_id"),
            attributes=attributes,
            error=error,
        )
        await store.write_span(span)
        existing = await store.get_run(ctx["trace_id"])
        await store.upsert_run(tracing.fold_span_into_run(existing or tracing.empty_run(ctx["trace_id"]), span))
        return span["span_id"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record span (%s/%s): %s", kind, name, exc)
        return ""


# ---------------------------------------------------------------------------
# Rate limits and budgets
# ---------------------------------------------------------------------------
# Two halves, because the two families of limit know their cost at
# different times. A request or a tool call costs exactly one of itself, so
# it is counted up front and judged immediately. Tokens and dollars are not
# known until the provider has answered, so the *pre* check reads what has
# already been spent this window and refuses a caller whose budget is
# already gone, and the *post* step adds what this call actually cost. The
# consequence is that a budget is enforced on the call after the one that
# exhausted it, which is the honest behaviour: the alternative is refusing
# work based on a guess at what it will cost.
async def check_limits(
    role: str | None,
    subject: str | None,
    families: list[str],
    trace_id: str | None = None,
) -> list[dict]:
    cfg = governance_config
    verdicts: list[dict] = []
    if not cfg.get("enabled") or governance.is_exempt(cfg, role):
        return verdicts

    for family in families:
        if not governance.limit_for(cfg, family):
            continue
        if family == "tool_calls_per_run":
            if not trace_id:
                continue
            used = await store.incr_counter(
                governance.run_counter_key(family, trace_id), 1, expiry_seconds=6 * 3600
            )
        elif family in ("tokens", "spend"):
            raw = await store.read_counter(governance.counter_key(family, subject or "unknown"))
            used = governance.from_spend_units(raw) if family == "spend" else raw
        else:
            used = await store.incr_counter(
                governance.counter_key(family, subject or "unknown"), 1,
                expiry_seconds=governance.window_expiry_seconds(family),
            )
        verdicts.append(governance.evaluate(cfg, family, used, role))

    return verdicts


async def record_usage(role: str | None, subject: str | None, *, tokens: int = 0, cost_usd: float = 0.0):
    """Add what a completed call actually consumed to the rolling budget
    counters."""
    cfg = governance_config
    if not cfg.get("enabled") or governance.is_exempt(cfg, role):
        return
    if tokens > 0 and cfg.get("tokens_per_hour"):
        await store.incr_counter(
            governance.counter_key("tokens", subject or "unknown"), int(tokens),
            expiry_seconds=governance.window_expiry_seconds("tokens"),
        )
    units = governance.to_spend_units(cost_usd)
    if units > 0 and cfg.get("spend_per_day_usd"):
        await store.incr_counter(
            governance.counter_key("spend", subject or "unknown"), units,
            expiry_seconds=governance.window_expiry_seconds("spend"),
        )


def limit_headers(verdict: dict) -> dict:
    headers = {
        "X-AOM-Refusal": "limit",
        "X-AOM-Limit": verdict.get("family") or "",
        "X-AOM-Limit-Value": str(verdict.get("limit") or ""),
    }
    if verdict.get("retry_after_seconds"):
        headers["Retry-After"] = str(verdict["retry_after_seconds"])
    return headers


async def enforce_or_log(
    verdicts: list[dict],
    ctx: dict,
    *,
    action: str,
    role: str,
    subject: str,
    tool_id: str | None = None,
    query: str | None = None,
    started: float,
):
    """Apply the blocking verdict, if there is one: audit it, trace it, and
    raise 429. Returns quietly when nothing is blocking, so a caller can
    treat this as a guard clause. Verdicts that were exceeded but not
    enforced still reach the trace - measuring a limit is only useful if
    what it would have done is visible."""
    exceeded = governance.exceeded_verdicts(verdicts)
    blocking = governance.blocking_verdict(verdicts)
    if not exceeded:
        return

    attributes = {
        "aom.limit_families": [v["family"] for v in exceeded],
        "aom.limit_reasons": [v["reason"] for v in exceeded if v.get("reason")],
        "aom.limit_blocked": bool(blocking),
        "aom.enforced": bool(governance_config.get("enforce")),
    }
    if tool_id:
        attributes["aom.tool_id"] = tool_id

    await record_span(
        ctx, kind="internal", name=f"limit check: {action}", started=started,
        status="error" if blocking else "ok", role=role, subject=subject,
        attributes={**attributes, "aom.operation": "governance.check"},
    )

    if not blocking:
        logger.info(
            "Limit exceeded but not enforced for subject %s (%s) - %s",
            subject, action, blocking or exceeded[0].get("reason"),
        )
        return

    latency_ms = int((time.time() - started) * 1000)
    await store.log_access(
        action=action, role=role, subject_label=subject, query=query,
        tool_id=tool_id, server_id=None, decision="DENY",
        reason=f"limit: {blocking['reason']}", latency_ms=latency_ms,
    )
    raise HTTPException(
        status_code=429,
        detail=blocking["reason"],
        headers=limit_headers(blocking),
    )


async def load_llm_config():
    """Read the stored cache policy, falling back to the .env bootstrap
    defaults the first time this appliance ever starts."""
    global llm_config, llm_config_version
    stored = await store.get_setting(LLM_CACHE_SETTINGS_DOC)
    llm_config = llm_cache.normalize_config(stored.get("config") if stored else LLM_CACHE_DEFAULTS)
    llm_config_version = llm_cache.config_fingerprint(llm_config)
    if not stored:
        await save_llm_config(llm_config)
    logger.info(
        "LLM cache policy loaded (provider=%s model=%s ttl=%ss semantic=%s)",
        llm_config["provider"], llm_config["model"], llm_config["ttl_seconds"], llm_config["semantic_enabled"],
    )


async def save_llm_config(cfg: dict):
    global llm_config, llm_config_version
    llm_config = llm_cache.normalize_config(cfg)
    llm_config_version = llm_cache.config_fingerprint(llm_config)
    await store.upsert_setting(
        LLM_CACHE_SETTINGS_DOC,
        {
            "doc_type": "settings",
            "setting_id": "llm_cache",
            "config": llm_config,
            "config_version": llm_config_version,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def load_context_config():
    """Read the stored context-cache policy, falling back to the .env
    bootstrap defaults the first time this appliance ever starts."""
    global context_config
    stored = await store.get_setting(CONTEXT_CACHE_SETTINGS_DOC)
    context_config = context_cache.normalize_config(stored.get("config") if stored else CONTEXT_CACHE_DEFAULTS)
    if not stored:
        await save_context_config(context_config)
    logger.info(
        "Context cache policy loaded (enabled=%s ttl=%ss scope=%s max_entries=%s)",
        context_config["enabled"], context_config["ttl_seconds"],
        context_config["cache_scope"], context_config["max_entries"],
    )


async def save_context_config(cfg: dict):
    global context_config
    context_config = context_cache.normalize_config(cfg)
    await store.upsert_setting(
        CONTEXT_CACHE_SETTINGS_DOC,
        {
            "doc_type": "settings",
            "setting_id": "context_cache",
            "config": context_config,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def refresh_catalog_version() -> str:
    """A fingerprint of the vetted tool catalog. Only consulted when the
    `invalidate_on_catalog_change` policy is on - an agent's answer can
    depend on which tools it was allowed to see, so a catalog change is a
    legitimate reason to stop reusing an answer produced before it."""
    global llm_catalog_version
    try:
        tools = await store.list_tools()
        material = "|".join(sorted(f"{t.get('tool_id')}:{t.get('trust_status')}" for t in tools))
        llm_catalog_version = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not compute catalog version: %s", exc)
    return llm_catalog_version


async def after_catalog_change(reason: str):
    """One entry point for "the vetted catalog just moved".

    Two things follow from a catalog change, for the same underlying
    reason: an agent's behaviour depends on which tools it was allowed to
    see. The LLM cache already knew this - an answer produced under a
    different catalog may no longer be the answer. The evaluation gate is
    the other half of it: the trajectories saved in a dataset were recorded
    against a particular catalog, so a change is exactly the moment to find
    out which of them still hold.

    Answering "you changed a tool's allowed roles - which saved
    trajectories does that break?" at the moment of the change, rather than
    in production a week later, is the whole point of hanging the gate here
    rather than on a timer.

    Deliberately fire-and-forget for the eval half: registering a server
    should not block on running a dataset against it, and a gate that made
    the Servers page feel slow would be turned off.
    """
    previous = llm_catalog_version
    await refresh_catalog_version()
    if previous and previous != llm_catalog_version and llm_config.get("invalidate_on_catalog_change"):
        removed = await sweep_llm_cache()
        if removed:
            logger.info("Catalog change (%s) invalidated %d cache entr(ies)", reason, removed)

    if EVAL_RUN_ON_CATALOG_CHANGE:
        spawn(run_eval_gate(trigger=f"catalog change: {reason}"))


async def run_eval_gate(trigger: str) -> list[dict]:
    """Run every dataset marked `run_on_catalog_change` and store the
    results with their comparison against the previous run. Returns the
    runs, so the manual "run now" route can reuse this and return them
    directly."""
    global last_eval_gate_at
    runs = []
    for dataset in await store.list_datasets():
        if not dataset.get("enabled"):
            continue
        if trigger.startswith("catalog change") and not dataset.get("run_on_catalog_change"):
            continue
        try:
            runs.append(await execute_eval_dataset(dataset, trigger=trigger))
        except Exception as exc:  # noqa: BLE001
            logger.error("Eval dataset '%s' failed to run: %s", dataset.get("dataset_id"), exc)
    last_eval_gate_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    regressed = [r for r in runs if (r.get("comparison") or {}).get("trend") == "regressed"]
    if regressed:
        logger.warning(
            "Evaluation gate (%s): %d dataset(s) regressed - %s",
            trigger, len(regressed),
            ", ".join(f"{r['dataset_id']} ({r['comparison']['summary']})" for r in regressed),
        )
    return runs


async def execute_eval_dataset(dataset: dict, trigger: str) -> dict:
    """Run one dataset against the live gateway.

    The three callables below are the *same* code the gateway serves, not a
    reimplementation of it: discovery goes through the RBAC + vector
    pre-filter, an invoke check applies the identical authorization rules,
    and a completion goes through the cache. An eval that tested a copy of
    those would pass while production broke, which is the failure mode this
    is meant to prevent.

    The invoke case is checked rather than executed. A dataset is run
    automatically on every catalog change, and running it must not have
    side effects on the customer's systems - asserting the *decision* is
    what the case is about anyway, and calling a real ticketing system a
    few hundred times a day to learn it is not.
    """
    async def discover_fn(role, query, top_k):
        if embeddings is None:
            raise RuntimeError("embedding model is not loaded")
        vector = await embeddings.embed_async(query or "")
        tools = await store.discover_tools(role, vector, top_k=int(top_k or 5))
        return [t.get("tool_id") for t in tools]

    async def invoke_fn(role, tool_id, arguments):
        tool = await store.get_tool(tool_id)
        if not tool:
            return "DENY", "tool not found in the vetted Couchbase catalog"
        if tool.get("drift_status") == "drifted":
            return "DENY", "quarantined: definition changed after approval"
        if tool.get("trust_status") != "trusted":
            return "DENY", f"tool trust_status is '{tool.get('trust_status')}'"
        if role not in (tool.get("allowed_roles") or []):
            return "DENY", f"role '{role}' is not in allowed_roles {tool.get('allowed_roles')}"
        return "ALLOW", "authorized"

    async def complete_fn(role, prompt):
        cfg = dict(llm_config)
        scope = llm_cache.scope_key(cfg, role, f"eval::{dataset.get('dataset_id')}")
        eid = llm_cache.entry_id(cfg, cfg["provider"], cfg["model"], prompt or "", {}, scope)
        hit, _similarity, _reason = await _lookup_cache(cfg, cfg["provider"], cfg["model"], scope, eid, prompt or "")
        if hit:
            return hit.get("response", "")
        result = await asyncio.to_thread(
            llm_cache.call_provider, cfg["provider"], cfg["model"], prompt or "", cfg, LLM_API_KEYS,
            int(governance_config.get("llm_timeout_seconds") or 60),
        )
        return result["text"]

    run = await evals.run_dataset(
        dataset,
        discover_fn=discover_fn,
        invoke_fn=invoke_fn,
        complete_fn=complete_fn,
        embed_fn=(embeddings.embed if embeddings is not None else None),
        trigger=trigger,
    )
    previous = await store.latest_eval_run(dataset["dataset_id"])
    run["comparison"] = evals.compare_runs(run, previous)
    run["catalog_version"] = llm_catalog_version
    await store.upsert_eval_run(run)
    await store.prune_eval_runs(dataset["dataset_id"])
    return run


def _sweep_window(cfg: dict) -> int:
    """How many entries a sweeper pass reads. It has to see more than
    max_entries or the overflow eviction below can never fire - a fixed
    10000 against the context cache's default cap of 20000 meant that cap
    was never enforced at all."""
    return min(200000, max(10000, int(cfg.get("max_entries") or 0) + 10000))


async def llm_cache_sweeper_loop():
    """Enforces the parts of the invalidation policy that nothing else would
    notice on its own: entries whose TTL, reuse limit, model, config or
    catalog fingerprint has gone stale, and overflow past `max_entries`
    under the configured eviction policy.

    Couchbase document expiry already reclaims TTL'd entries, so this is
    belt-and-braces for TTL - but it is the *only* thing that applies the
    other four rules to entries nobody happens to read again."""
    global last_llm_sweep_at
    while True:
        await asyncio.sleep(max(60, int(llm_config.get("sweep_interval_minutes", 5)) * 60))
        if not store.connected:
            continue
        try:
            removed = await sweep_llm_cache()
            last_llm_sweep_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if removed:
                logger.info("LLM cache sweeper removed %d entr(ies) on this pass", removed)
        except Exception as exc:  # noqa: BLE001
            logger.error("LLM cache sweep failed: %s", exc)


async def sweep_llm_cache() -> int:
    """One sweeper pass. Returns how many entries were removed."""
    await refresh_catalog_version()
    entries = await store.list_cache_entries(limit=_sweep_window(llm_config))
    now = time.time()
    removed = 0
    survivors = []
    for entry in entries:
        state, _reason = llm_cache.evaluate_entry(
            entry, llm_config, now=now,
            config_version=llm_config_version, catalog_version=llm_catalog_version,
        )
        if state == "invalid":
            if await store.delete_cache_entry(entry["entry_id"]):
                removed += 1
        else:
            survivors.append(entry)

    for entry_id in llm_cache.select_evictions(survivors, llm_config):
        if await store.delete_cache_entry(entry_id):
            removed += 1
    return removed


async def context_cache_sweeper_loop():
    """Same reasoning as llm_cache_sweeper_loop: Couchbase document expiry
    already reclaims TTL'd entries, but nothing else applies the eviction
    policy to entries nobody happens to read again."""
    global last_context_sweep_at
    while True:
        await asyncio.sleep(max(60, int(context_config.get("sweep_interval_minutes", 5)) * 60))
        if not store.connected:
            continue
        try:
            removed = await sweep_context_cache()
            last_context_sweep_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if removed:
                logger.info("Context cache sweeper removed %d entr(ies) on this pass", removed)
        except Exception as exc:  # noqa: BLE001
            logger.error("Context cache sweep failed: %s", exc)


async def sweep_context_cache() -> int:
    """One sweeper pass. Returns how many entries were removed."""
    entries = await store.list_context_entries(limit=_sweep_window(context_config))
    now = time.time()
    removed = 0
    survivors = []
    for entry in entries:
        state, _reason = context_cache.evaluate_entry(entry, context_config, now=now)
        if state == "invalid":
            if await store.delete_context_entry(entry["entry_id"]):
                removed += 1
        else:
            survivors.append(entry)

    for entry_id in context_cache.select_evictions(survivors, context_config):
        if await store.delete_context_entry(entry_id):
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class DiscoverRequest(BaseModel):
    query: str
    top_k: int = 5


class InvokeRequest(BaseModel):
    tool_id: str
    arguments: dict = {}
    # Set on the second call, after a human approved the first. Ignored for
    # tools that do not require approval.
    approval_id: str | None = None


class RegisterServerRequest(BaseModel):
    server_id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    label: str
    owner: str = "Unassigned"
    mcp_url: str
    trust_status: str = "trusted"  # "trusted" | "untrusted"
    default_allowed_roles: list[str] = Field(default_factory=list)
    # Reviewed per-tool policy, {tool_name: {allowed_roles, risk_level}} --
    # see rbac_policy.normalize_tool_policies / policy_for. Tools listed
    # here are classified at ingest instead of falling back to the
    # "unclassified", admin-only default.
    tool_policies: dict = Field(default_factory=dict)
    # How this gateway authenticates to the server. Raw dict, validated
    # server-side by server_auth.normalize_auth - same "the form is a
    # convenience, this is the boundary" convention as LdapConfigRequest.
    auth: dict = Field(default_factory=dict)
    # The credential itself, plaintext on the way in and encrypted at rest.
    # Never returned by any route.
    auth_secret: str | None = None


class ServerAuthRequest(BaseModel):
    auth: dict = Field(default_factory=dict)
    # Omitted or blank leaves the stored secret untouched, so saving the
    # form without re-typing the credential does not erase it.
    auth_secret: str | None = None
    # Explicit removal, since "leave it alone" and "delete it" cannot both
    # be spelled as an empty string.
    clear_secret: bool = False


class GovernanceConfigRequest(BaseModel):
    config: dict


class EvalDatasetRequest(BaseModel):
    # Raw dataset dict, re-validated by evals.normalize_dataset.
    dataset: dict


class EvalRunRequest(BaseModel):
    dataset_id: str | None = None


# -- Local dashboard login ---------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class BootstrapRequest(BaseModel):
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str = Field(..., pattern=r"^[a-zA-Z0-9._-]{2,64}$")
    password: str
    role: str = user_auth.DEFAULT_LOCAL_ROLE
    must_change_password: bool = True


class UpdateUserRequest(BaseModel):
    role: str | None = None
    active: bool | None = None
    password: str | None = None
    must_change_password: bool | None = None


class LdapConfigRequest(BaseModel):
    # Raw dict, same convention as LLMConfigRequest.config - validated
    # server-side by user_auth.normalize_ldap_config, not by this shape.
    # An included "bind_password" (plain text) sets/replaces the encrypted
    # secret; omitting it (or sending "") leaves the stored one unchanged.
    config: dict
    bind_password: str | None = None


class LdapTestRequest(BaseModel):
    username: str
    password: str


class LdapCaCertificateRequest(BaseModel):
    ca_certificate: str


class SiemVendorConfigRequest(BaseModel):
    # Raw per-field dict for one vendor, same "server validates, this is
    # just a bag of fields" convention as LdapConfigRequest.config. Any
    # secret field present here (see siem_forwarding.SECRET_FIELDS) is
    # plaintext and gets encrypted before storage; omitted/blank leaves the
    # secret already on file untouched.
    config: dict


class SiemTestRequest(BaseModel):
    config: dict | None = None  # optional unsaved edits to test before saving


class ServerCertificateRequest(BaseModel):
    cert_pem: str
    key_pem: str


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------
async def authenticate(authorization: str | None, request: Request | None = None) -> tuple[str, str]:
    """Resolve an `Authorization: Bearer <api_key>` header to (role,
    masked_subject_label). Raises HTTPException(401) if missing/invalid -
    every failure here is exactly the kind of thing an unauthenticated MCP
    setup has no equivalent check for at all.

    When `request` is passed and the rate-limit middleware already resolved
    this caller, that result is reused rather than repeating the lookup - the
    middleware has to resolve the key to know whose bucket to count against,
    and doing it twice per request would be a second KV get on the hot path
    for no new information."""
    cached = getattr(request.state, "agent_identity", None) if request is not None else None
    if cached:
        return cached[0], cached[1]
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="Missing Authorization: Bearer <api key or token> header"
        )
    credential = authorization.split(" ", 1)[1].strip()
    try:
        identity, reason = await resolve_caller(credential)
    except Exception as exc:  # noqa: BLE001
        # Authentication must never answer with a 500. An unexpected failure
        # here is still a failure to authenticate, and returning it as one
        # keeps the refusal path uniform - a caller cannot tell a broken
        # lookup from a bad credential, and an operator gets it in the log
        # either way.
        logger.error("Credential resolution failed unexpectedly: %s", exc)
        identity, reason = None, "credential could not be verified"
    if not identity:
        # The refusal reason is specific - revoked, expired, rotated past
        # its grace window - and it is recorded. A revocation that looked
        # identical to a typo in the audit log would be useless during the
        # incident it exists for.
        await store.log_access(
            action="authenticate", role=None, subject_label="unknown", query=None,
            tool_id=None, server_id=None, decision="DENY",
            reason=reason or "invalid credential", latency_ms=0,
        )
        raise HTTPException(status_code=401, detail=reason or "Invalid credential")
    if request is not None:
        request.state.agent_identity = (identity["role"], identity["key_prefix"], identity)
    return identity["role"], identity["key_prefix"]


def caller_identity(request: Request | None) -> dict:
    """The full identity for the current request, for the paths that need
    more than (role, subject) - scoping, above all."""
    cached = getattr(request.state, "agent_identity", None) if request is not None else None
    return cached[2] if cached and len(cached) > 2 else {}


def require_admin(request: Request) -> dict:
    """Dashboard-session counterpart to authenticate() above: require_dashboard_session
    (the app middleware) already guarantees request.state.user exists on any
    protected path, so this only adds the role check for the Settings
    surface (local accounts, LDAP config) - the parts of Settings the
    request explicitly scopes to admin users."""
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


# ---------------------------------------------------------------------------
# Health / roles
# ---------------------------------------------------------------------------
def _require_store() -> None:
    """A clear 503 instead of an unhandled 500 when Couchbase is not
    connected (e.g. still warming up after a restart - see
    couchbase_reconnect_loop)."""
    if not store.connected:
        raise HTTPException(
            status_code=503,
            detail="The appliance cannot reach Couchbase yet and is retrying - try again shortly.",
        )


@app.get("/api/health")
async def health():
    # Deliberately still 200 when degraded: this endpoint is the container's
    # own healthcheck, and an appliance that is up and able to explain what is
    # wrong with it must not be restarted out from under the operator reading
    # that explanation. `degraded` and `startup_failures` are what the
    # dashboard shows instead.
    return {
        "status": "ok" if ready else "starting",
        "appliance": APPLIANCE_NAME,
        "couchbase_connected": store.connected,
        "embeddings_ready": embeddings is not None,
        "degraded": bool(startup_failures),
        "startup_failures": list(startup_failures),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@app.get("/v1/roles")
async def roles():
    return {"roles": [{"id": rid, "description": desc} for rid, desc in ROLES.items()]}


# ---------------------------------------------------------------------------
# Developer SDK distribution (see Tools -> Developer SDK in the dashboard)
# ---------------------------------------------------------------------------
@app.get("/v1/sdk/info")
async def sdk_info():
    """Metadata for the Developer SDK download button - version, filename
    and size - without shipping the archive bytes themselves."""
    if not sdk_packaging.sdk_available():
        raise HTTPException(status_code=404, detail="Developer SDK is not bundled in this image")
    archive = sdk_packaging.build_sdk_archive()
    return {
        "version": sdk_packaging.sdk_version(),
        "filename": sdk_packaging.sdk_filename(),
        "size_bytes": len(archive),
    }


@app.get("/v1/sdk/download")
async def sdk_download():
    """Zips operations-manager/sdk/ on demand and serves it as an
    attachment, so the download always matches the SDK source shipped in
    this running image rather than a prebuilt artifact that can go stale."""
    if not sdk_packaging.sdk_available():
        raise HTTPException(status_code=404, detail="Developer SDK is not bundled in this image")
    archive = sdk_packaging.build_sdk_archive()
    filename = sdk_packaging.sdk_filename()
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# AI-assistant integration skills (Claude / ChatGPT / Gemini) - same
# integration knowledge as the Developer SDK guide, packaged for each
# assistant's own way of taking custom instructions. See
# operations-manager/skills/ and app/skill_packaging.py.
# ---------------------------------------------------------------------------
@app.get("/v1/skills/{platform}/info")
async def skill_info(platform: str):
    if platform not in skill_packaging.PLATFORMS:
        raise HTTPException(status_code=404, detail=f"Unknown skill platform '{platform}'")
    if not skill_packaging.skill_available(platform):
        raise HTTPException(status_code=404, detail=f"{skill_packaging.skill_label(platform)} is not bundled in this image")
    archive = skill_packaging.build_skill_archive(platform)
    return {
        "platform": platform,
        "label": skill_packaging.skill_label(platform),
        "version": sdk_packaging.sdk_version(),
        "filename": skill_packaging.skill_filename(platform),
        "size_bytes": len(archive),
    }


@app.get("/v1/skills/{platform}/download")
async def skill_download(platform: str):
    if platform not in skill_packaging.PLATFORMS:
        raise HTTPException(status_code=404, detail=f"Unknown skill platform '{platform}'")
    if not skill_packaging.skill_available(platform):
        raise HTTPException(status_code=404, detail=f"{skill_packaging.skill_label(platform)} is not bundled in this image")
    archive = skill_packaging.build_skill_archive(platform)
    filename = skill_packaging.skill_filename(platform)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Server registry
# ---------------------------------------------------------------------------
@app.get("/v1/servers")
async def list_servers_route():
    servers = await store.list_servers()
    tools = await store.list_tools()
    counts: dict[str, int] = {}
    for t in tools:
        sid = t.get("server_id")
        counts[sid] = counts.get(sid, 0) + 1
    public = []
    for s in servers:
        doc = public_server(s)
        doc["tool_count"] = counts.get(s.get("server_id"), 0)
        public.append(doc)
    return {"servers": public}


def public_server(server_doc: dict) -> dict:
    """A server document as the dashboard is allowed to see it: everything
    except the encrypted downstream credential, replaced by the fact that
    one is configured."""
    doc = {k: v for k, v in (server_doc or {}).items() if k != "auth_secret"}
    doc["auth"] = server_auth.public_auth(server_doc)
    return doc


@app.post("/v1/servers")
async def register_server(req: RegisterServerRequest):
    if req.trust_status not in ("trusted", "untrusted"):
        raise HTTPException(status_code=400, detail="trust_status must be 'trusted' or 'untrusted'")
    existing = await store.get_server(req.server_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Server '{req.server_id}' is already registered")

    unknown_roles = [r for r in req.default_allowed_roles if r not in ROLES]
    if unknown_roles:
        raise HTTPException(status_code=400, detail=f"Unknown role(s): {unknown_roles}")
    try:
        tool_policies = normalize_tool_policies(req.tool_policies)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    server_doc = {
        "server_id": req.server_id,
        "label": req.label,
        "owner": req.owner,
        "mcp_url": req.mcp_url,
        "trust_status": req.trust_status,
        "default_allowed_roles": req.default_allowed_roles,
        "tool_policies": tool_policies,
        "auth": server_auth.normalize_auth(req.auth),
        "seeded": False,
    }
    server_auth.store_secret(server_doc, req.auth_secret)
    await store.upsert_server(req.server_id, server_doc)

    ingested_tools = 0
    drifted = 0
    ingest_error = None
    if req.trust_status == "trusted" and embeddings is not None:
        try:
            summary = await ingest_server(store, embeddings, server_doc)
            ingested_tools = summary["tools"]
            drifted = summary["drifted"]
        except Exception as exc:  # noqa: BLE001
            ingest_error = str(exc)
            logger.error("Ingestion failed for newly registered server '%s': %s", req.server_id, exc)

    await after_catalog_change(f"server '{req.server_id}' registered")
    return {
        "server": public_server(server_doc),
        "ingested_tools": ingested_tools,
        "drifted_tools": drifted,
        "ingest_error": ingest_error,
    }


@app.put("/v1/servers/{server_id}/auth")
async def put_server_auth(server_id: str, req: ServerAuthRequest, request: Request):
    """Set how this gateway authenticates to one registered server. The
    credential is encrypted at rest with the same key that protects the
    LDAP bind password, and no route ever returns it - the Servers page is
    told only whether one is on file."""
    require_admin(request)
    server_doc = await store.get_server(server_id)
    if not server_doc:
        raise HTTPException(status_code=404, detail="Server not registered")

    server_doc["auth"] = server_auth.normalize_auth(req.auth)
    if req.clear_secret:
        server_auth.clear_secret(server_doc)
    else:
        server_auth.store_secret(server_doc, req.auth_secret)

    await store.upsert_server(server_id, server_doc)
    return {"server": public_server(server_doc)}


@app.post("/v1/servers/{server_id}/reingest")
async def reingest_server_route(server_id: str):
    server_doc = await store.get_server(server_id)
    if not server_doc:
        raise HTTPException(status_code=404, detail="Server not registered")
    if server_doc.get("trust_status") != "trusted":
        raise HTTPException(status_code=400, detail="Server is not trusted - mark it trusted before ingesting its catalog")
    if embeddings is None:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")
    try:
        summary = await ingest_server(store, embeddings, server_doc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ingestion failed: {exc}") from exc
    await after_catalog_change(f"server '{server_id}' re-ingested")
    return {
        "server_id": server_id,
        "ingested_tools": summary["tools"],
        "quarantined_tools": summary["quarantined"],
        "drifted_tools": summary["drifted"],
    }


@app.delete("/v1/servers/{server_id}")
async def delete_server_route(server_id: str):
    deleted = await store.delete_server(server_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Server not registered")
    tools_removed = await store.delete_tools_by_server(server_id)
    await after_catalog_change(f"server '{server_id}' removed")
    return {"deleted": True, "server_id": server_id, "tools_removed": tools_removed}


# ---------------------------------------------------------------------------
# Catalog / audit log
# ---------------------------------------------------------------------------
@app.get("/v1/catalog")
async def catalog():
    """Full transparency view of everything actually stored in Couchbase's
    tool registry, for the dashboard UI to display."""
    return {"tools": await store.list_tools()}


@app.get("/v1/agent/tools")
async def agent_tools(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """The agent-facing counterpart to /v1/catalog: every tool the caller
    could actually invoke, with its full `input_schema`, and nothing else.

    /v1/catalog is the dashboard's transparency view and sits behind a
    dashboard session, so an agent holding only an API key cannot read it.
    The SDK's `discover_mcp_tools()` and its `aom-mcp-server` bridge both
    need each tool's input schema (discover does not return it), so they
    read this instead. The filter is the same one invoke enforces - trusted,
    the caller's role in `allowed_roles`, and inside the agent's scope - so
    this never lists a tool invoke would refuse."""
    role, subject = await authenticate(authorization, request)
    if not store.connected:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")
    start = time.time()
    tools = [
        t for t in await store.list_tools()
        if t.get("trust_status") == "trusted" and role in (t.get("allowed_roles") or [])
    ]
    tools = agent_identity.filter_to_scope(caller_identity(request), tools)
    await store.log_access(
        action="list_tools", role=role, subject_label=subject, query=None,
        tool_id=None, server_id=None, decision="ALLOW",
        reason=f"{len(tools)} invokable tool(s) listed for role '{role}'",
        latency_ms=int((time.time() - start) * 1000),
    )
    return {"role": role, "tools": tools}


@app.get("/v1/audit-log")
async def audit_log(limit: int = 50):
    return {"entries": await store.recent_access_log(limit=min(limit, 200))}


# ---------------------------------------------------------------------------
# Discover / invoke
# ---------------------------------------------------------------------------
@app.post("/v1/tools/discover")
async def discover(
    req: DiscoverRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    role, subject = await authenticate(authorization, request)
    if not store.connected or embeddings is None:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")

    ctx = trace_context(request)
    start = time.time()

    vector = await embeddings.embed_async(req.query)
    tools = await store.discover_tools(role, vector, top_k=req.top_k)
    # Applied after the RBAC + vector pre-filter, never instead of it, so an
    # agent scope can only ever remove candidates the role already allowed.
    identity = caller_identity(request)
    scoped = agent_identity.filter_to_scope(identity, tools)
    scope_removed = len(tools) - len(scoped)
    tools = scoped
    latency_ms = int((time.time() - start) * 1000)

    await store.log_access(
        action="discover", role=role, subject_label=subject, query=redact_text_for_storage(req.query),
        tool_id=None, server_id=None, decision="ALLOW",
        reason=(
            f"{len(tools)} tool(s) matched RBAC+vector pre-filter for role '{role}'"
            + (f"; {scope_removed} removed by this agent's scope" if scope_removed else "")
        ),
        latency_ms=latency_ms,
    )
    # Discovery is where an agent's *intent* is legible - the query it asked
    # with, and the tools this appliance was willing to show it. Recorded as
    # an "internal" span rather than a tool call: nothing downstream ran.
    span_id = await record_span(
        ctx, kind="internal", name="discover", started=start, role=role, subject=subject,
        attributes={
            "aom.operation": "tools.discover",
            "aom.query": redact_text_for_storage(req.query),
            "aom.top_k": req.top_k,
            "aom.result_count": len(tools),
            "aom.tool_ids": [t.get("tool_id") for t in tools],
            "aom.agent_id": identity.get("agent_id"),
            "aom.scope_removed": scope_removed,
        },
    )
    return {
        "role": role,
        "tools": tools,
        "latency_ms": latency_ms,
        "trace": {"trace_id": ctx["trace_id"], "span_id": span_id},
    }


@app.post("/v1/tools/invoke")
async def invoke(
    req: InvokeRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    role, subject = await authenticate(authorization, request)
    ctx = trace_context(request)
    start = time.time()

    # Tool invocation gets its own ceilings on top of the general request
    # rate: it is the only thing here that reaches a downstream server, and
    # the per-run ceiling is what actually catches a loop - a runaway agent
    # stays comfortably inside a per-minute rate while making its fortieth
    # tool call of the same task.
    verdicts = await check_limits(
        role, subject, ["tool_calls", "tool_calls_per_run"], trace_id=ctx["trace_id"]
    )
    await enforce_or_log(
        verdicts, ctx, action="invoke", role=role, subject=subject, tool_id=req.tool_id, started=start
    )

    async def deny(
        reason: str, status: int, detail: str,
        server_id: str | None = None, refusal: str | None = None,
    ):
        latency_ms = int((time.time() - start) * 1000)
        await store.log_access(
            action="invoke", role=role, subject_label=subject, query=None, tool_id=req.tool_id,
            server_id=server_id, decision="DENY", reason=reason, latency_ms=latency_ms,
        )
        await record_span(
            ctx, kind="tool_call", name=f"invoke {req.tool_id}", started=start, status="error",
            role=role, subject=subject,
            attributes={
                "aom.tool_id": req.tool_id,
                "aom.server_id": server_id,
                "aom.decision": "DENY",
                "aom.reason": reason,
            },
            error=reason,
        )
        raise HTTPException(
            status_code=status, detail=detail,
            headers={"X-AOM-Refusal": refusal} if refusal else None,
        )

    # Tool arguments are caller-supplied text heading for a downstream
    # system, so unlike a flagged *response* there is still something worth
    # stopping. Scanned before the catalog lookup so a payload aimed at a
    # tool that does not exist is still recorded.
    guard = None
    if guardrails_config.get("enabled") and guardrails_config.get("scan_tool_arguments") and req.arguments:
        guard = guardrails.inspect_input(
            json.dumps(req.arguments, default=str), guardrails_config
        )
        if guard["blocked"]:
            await deny(
                f"guardrail: {guardrails.summarize(guard)}", 400,
                f"Tool arguments were refused by the guardrails policy: {guardrails.summarize(guard)}",
                refusal="guardrail",
            )

    tool = await store.get_tool(req.tool_id)
    if not tool:
        await deny(
            "tool not found in the vetted Couchbase catalog", 404,
            "Unknown tool - it is not in the vetted catalog",
        )

    # An agent scoped to a subset of its role's tools is refused here even
    # though the role would allow it. Checked before the RBAC test so the
    # audit log says which of the two actually stopped the call.
    identity = caller_identity(request)
    if not agent_identity.in_scope(identity, req.tool_id):
        await deny(
            f"tool '{req.tool_id}' is outside this agent's scope (role '{role}' allows it, this agent does not)",
            403,
            f"'{req.tool_id}' is not in this agent's allowed tools",
            server_id=tool.get("server_id"),
            refusal="scope",
        )

    # Second, independent authorization check - never trust that a client
    # only ever asks for tools it was shown by /v1/tools/discover.
    if tool.get("trust_status") != "trusted" or role not in (tool.get("allowed_roles") or []):
        # A tool quarantined for definition drift is refused for a different
        # reason than one that failed RBAC, and saying so is the difference
        # between an operator checking the Threat Detection page and one
        # editing a role that was never the problem.
        if tool.get("drift_status") == "drifted":
            reason = (
                f"tool '{req.tool_id}' is quarantined: its definition changed after it was approved "
                f"and has not been reviewed"
            )
        else:
            reason = (
                f"role '{role}' is not authorized for tool '{req.tool_id}' "
                f"(requires one of {tool.get('allowed_roles')})"
            )
        await deny(
            reason, 403, f"Role '{role}' is not authorized to invoke '{req.tool_id}'",
            server_id=tool.get("server_id"),
        )

    server_doc = await store.get_server(tool.get("server_id"))
    if not server_doc:
        await deny("owning server is not registered", 404, "Owning server is not registered",
                   server_id=tool.get("server_id"))

    # ---- the human approval tier ------------------------------------------
    # RBAC has said this role may do this. The remaining question is whether
    # this particular call should happen now, which only a person can answer.
    needs_approval, approval_reason = approvals.requires_approval(tool, role, approvals_config)
    if needs_approval:
        if req.approval_id:
            approval = await store.get_approval(req.approval_id)
            usable, why = approvals.redeem(
                approval, tool_id=req.tool_id, arguments=req.arguments or {}, role=role, subject=subject
            )
            if not usable:
                await deny(f"approval: {why}", 403, why, server_id=tool.get("server_id"), refusal="approval")
            await store.upsert_approval(approvals.consume(approval))
        else:
            pending = approvals.new_approval(
                tool_id=req.tool_id, arguments=req.arguments or {}, role=role, subject=subject,
                reason=approval_reason, trace_id=ctx["trace_id"],
                server_id=tool.get("server_id"), risk_level=tool.get("risk_level"),
                cfg=approvals_config,
            )
            await store.upsert_approval(pending, ttl_seconds=int(approvals_config["ttl_seconds"]))
            latency_ms = int((time.time() - start) * 1000)
            await store.log_access(
                action="invoke", role=role, subject_label=subject, query=None, tool_id=req.tool_id,
                server_id=tool.get("server_id"), decision="DENY",
                reason=f"held for human approval: {approval_reason}", latency_ms=latency_ms,
            )
            await record_span(
                ctx, kind="tool_call", name=f"invoke {req.tool_id}", started=start, role=role, subject=subject,
                attributes={
                    "aom.tool_id": req.tool_id,
                    "aom.server_id": tool.get("server_id"),
                    "aom.decision": "PENDING_APPROVAL",
                    "aom.approval_id": pending["approval_id"],
                    "aom.reason": approval_reason,
                },
            )
            # 202, not an error: the call has not failed, it is waiting.
            return JSONResponse(
                status_code=202,
                content={
                    "status": "pending_approval",
                    "approval": approvals.public_approval(pending),
                    "detail": (
                        f"This call needs a human decision ({approval_reason}). Poll "
                        f"GET /v1/approvals/{pending['approval_id']} and re-invoke with "
                        f"approval_id once it is approved."
                    ),
                    "trace": {"trace_id": ctx["trace_id"]},
                },
            )

    call_span = await record_span(
        ctx, kind="tool_call", name=f"invoke {req.tool_id}", started=start, role=role, subject=subject,
        attributes={
            "aom.tool_id": req.tool_id,
            "aom.server_id": tool.get("server_id"),
            "aom.decision": "ALLOW",
            "aom.risk_level": tool.get("risk_level"),
            "aom.arguments": sorted((req.arguments or {}).keys()),
            "aom.guardrail_pii": bool(guard and guard["pii"]["found"]),
            "aom.guardrail_injection": bool(guard and guard["injection"]["flagged"]),
            "aom.approval_id": req.approval_id,
            "aom.downstream_auth_mode": server_auth.normalize_auth(server_doc.get("auth"))["mode"],
        },
    )
    result_ctx = {**ctx, "parent_span_id": call_span or ctx.get("parent_span_id")}
    downstream_started = time.time()

    try:
        # The downstream server is told both who this call is for (a signed
        # role/subject assertion) and, if one is configured, the credential
        # it authenticates this gateway with - see app/server_auth.py for
        # why passing neither is the gap that makes least privilege stop at
        # the last hop.
        result = await mcp_client.call_tool(
            server_doc["mcp_url"],
            tool["name"],
            req.arguments,
            headers=server_auth.outbound_headers(
                server_doc, role=role, subject=subject,
                trace_id=ctx["trace_id"], tool_id=req.tool_id,
            ),
            timeout_seconds=governance_config.get("downstream_timeout_seconds"),
        )
        latency_ms = int((time.time() - start) * 1000)

        # Response payload poisoning can't be caught at ingest time - the
        # payload doesn't exist until the call happens - so it's scanned
        # here, on every successful invoke, and flagged rather than
        # withheld (see app/hijack_detection.py for why). The finding rides
        # on the audit-log entry itself so chain correlation in the
        # insights engine can pick it up without a second lookup.
        hijack = hijack_detection.scan_response_payload(result)
        reason = "invoked via scoped operations-manager proxy"
        if hijack["flagged"]:
            reason += f" - response flagged for possible prompt injection ({hijack['severity']})"
            logger.warning(
                "Response payload from '%s' flagged: %s", req.tool_id, [s["pattern_id"] for s in hijack["signals"]]
            )

        await store.log_access(
            action="invoke", role=role, subject_label=subject, query=None, tool_id=req.tool_id,
            server_id=tool.get("server_id"), decision="ALLOW", reason=reason, latency_ms=latency_ms,
            hijack_flagged=hijack["flagged"], hijack_severity=hijack["severity"], hijack_signals=hijack["signals"],
        )
        # The result is its own span, parented to the call. Keeping them
        # separate is what makes a poisoned *response* attributable
        # independently of the request that triggered it.
        await record_span(
            result_ctx, kind="tool_result", name=f"result {req.tool_id}", started=downstream_started,
            role=role, subject=subject,
            attributes={
                "aom.tool_id": req.tool_id,
                "aom.server_id": tool.get("server_id"),
                "aom.hijack_flagged": hijack["flagged"],
                "aom.hijack_severity": hijack["severity"],
                "aom.hijack_patterns": [sig["pattern_id"] for sig in hijack["signals"]],
            },
        )
        return {
            "role": role,
            "tool_id": req.tool_id,
            "result": result,
            "latency_ms": latency_ms,
            "hijack_warning": hijack if hijack["flagged"] else None,
            "trace": {"trace_id": ctx["trace_id"], "span_id": call_span},
        }
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.time() - start) * 1000)
        await store.log_access(
            action="invoke", role=role, subject_label=subject, query=None, tool_id=req.tool_id,
            server_id=tool.get("server_id"), decision="ERROR", reason=str(exc), latency_ms=latency_ms,
        )
        await record_span(
            result_ctx, kind="tool_result", name=f"result {req.tool_id}", started=downstream_started,
            status="error", role=role, subject=subject,
            attributes={"aom.tool_id": req.tool_id, "aom.server_id": tool.get("server_id")},
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=f"Downstream MCP call failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Threat detection (MCP Tool Hijacking)
# ---------------------------------------------------------------------------
@app.get("/v1/threat-detection")
@cached_dashboard_response
async def threat_detection():
    tools, log_entries = await asyncio.gather(
        store.list_tools(),
        store.recent_access_log(limit=INSIGHTS_LOOKBACK_ENTRIES, analytics=True),
    )
    tools_by_id = {t["tool_id"]: t for t in tools if t.get("tool_id")}

    quarantined = [t for t in tools if t.get("trust_status") == "quarantined"]
    flagged_responses = [e for e in log_entries if e.get("action") == "invoke" and e.get("hijack_flagged")][:50]
    chain_findings = hijack_detection.detect_hijack_chains(log_entries, tools_by_id, window_seconds=HIJACK_CHAIN_WINDOW_SECONDS)

    # Tools whose definition moved after approval. Reported separately from
    # the hijack list because they are a different problem with a different
    # remedy: a hijack finding asks "is this text an attack?", a drift
    # finding asks "is this the text you approved?" - and the answer to the
    # second can be no while the answer to the first is also no.
    drifted = [
        {
            "tool_id": t.get("tool_id"),
            "server_id": t.get("server_id"),
            "name": t.get("name"),
            "risk_level": t.get("risk_level"),
            "trust_status": t.get("trust_status"),
            "drift_status": t.get("drift_status"),
            "drift_changes": t.get("drift_changes") or [],
            "drift_detected_at": t.get("drift_detected_at"),
            "approved_at": t.get("approved_at"),
            "approved_definition": t.get("approved_definition"),
            "definition_version": t.get("definition_version"),
            "description": t.get("description"),
            "input_schema": t.get("input_schema"),
            "severity": tool_versioning.drift_severity(t.get("drift_changes") or []),
        }
        for t in tools if t.get("drift_status") == "drifted"
    ]

    return {
        "last_scan_at": last_hijack_scan_at,
        "scan_interval_minutes": HIJACK_SCAN_INTERVAL_MINUTES,
        "chain_window_seconds": HIJACK_CHAIN_WINDOW_SECONDS,
        "quarantined_tools": quarantined,
        "flagged_responses": flagged_responses,
        "chain_findings": chain_findings,
        "drifted_tools": drifted,
    }


@app.post("/v1/tools/{tool_id:path}/release")
async def release_tool_route(tool_id: str):
    """Admin action: release a tool from quarantine. Sets a manual
    override that survives future re-ingests and background rescans -
    without it, the next scan pass would just re-quarantine a tool whose
    description still matches a pattern.

    A release also re-approves the definition being released, moving the
    drift baseline to the text the admin was actually looking at. Without
    that, a tool quarantined for drift would be released and then
    immediately re-quarantined by the next ingest on the very same diff,
    and the admin's decision would mean nothing.
    """
    tool = await store.get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found in the catalog")
    tool["trust_status"] = "trusted"
    tool["hijack_manual_override"] = "trusted"
    tool_versioning.approve_current_definition(tool)
    await store.upsert_tool(tool_id, tool)
    await after_catalog_change(f"tool '{tool_id}' released")
    return {"tool_id": tool_id, "trust_status": "trusted", "approved_at": tool.get("approved_at")}


@app.post("/v1/tools/{tool_id:path}/quarantine")
async def quarantine_tool_route(tool_id: str):
    """Admin action: quarantine a tool by hand, even if the scanner didn't
    flag it - e.g. a signal an admin caught by reading the description
    that the pattern bank doesn't cover yet."""
    tool = await store.get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found in the catalog")
    tool["trust_status"] = "quarantined"
    tool["hijack_manual_override"] = "quarantined"
    await store.upsert_tool(tool_id, tool)
    await after_catalog_change(f"tool '{tool_id}' quarantined")
    return {"tool_id": tool_id, "trust_status": "quarantined"}


@app.post("/v1/tools/{tool_id:path}/clear-override")
async def clear_override_route(tool_id: str):
    """Remove a manual release/quarantine override and let the scanner
    decide this tool's trust_status fresh, from its current stored
    description."""
    tool = await store.get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found in the catalog")
    tool.pop("hijack_manual_override", None)
    drift_status = tool.get("drift_status")
    tool = hijack_detection.apply_metadata_scan(tool, {})
    # Clearing an override hands the decision back to the scanner, which has
    # no opinion about drift - so an un-reviewed definition change must be
    # re-applied here, or "let the scanner decide" would quietly re-trust a
    # tool nobody has looked at.
    if drift_status == "drifted":
        tool["drift_status"] = drift_status
        tool["trust_status"] = "quarantined"
    await store.upsert_tool(tool_id, tool)
    await after_catalog_change(f"tool '{tool_id}' override cleared")
    return {"tool_id": tool_id, "trust_status": tool["trust_status"], "drift_status": tool.get("drift_status")}


# ---------------------------------------------------------------------------
# Dashboard / insights
# ---------------------------------------------------------------------------
@app.get("/v1/insights")
@cached_dashboard_response
async def insights_route():
    servers, tools, log_entries, eval_runs = await asyncio.gather(
        store.list_servers(),
        store.list_tools(),
        store.recent_access_log(limit=INSIGHTS_LOOKBACK_ENTRIES, analytics=True),
        store.list_eval_runs(limit=25),
    )
    # Only the most recent run per dataset can be a live regression - an
    # older one has already been superseded by whatever ran after it.
    latest_by_dataset: dict[str, dict] = {}
    for run in eval_runs:
        latest_by_dataset.setdefault(run.get("dataset_id"), run)
    return {
        "findings": insights.compute_insights(
            servers, tools, log_entries, eval_runs=list(latest_by_dataset.values())
        )
    }


@app.get("/v1/dashboard")
@cached_dashboard_response
async def dashboard():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Independent reads - run concurrently instead of stacking their latencies.
    servers, tools, log_entries, decision_rows = await asyncio.gather(
        store.list_servers(),
        store.list_tools(),
        store.recent_access_log(limit=INSIGHTS_LOOKBACK_ENTRIES, analytics=True),
        store.access_log_decision_aggregate_since(cutoff),
    )

    findings = insights.compute_insights(servers, tools, log_entries)
    actions = insights.action_breakdown(log_entries)
    hourly = insights.hourly_volume(log_entries, buckets=12)

    # "Access Events (24h)" and its Allow/Deny/Error breakdown need a real
    # COUNT(1) over the full 24h window, not a count over the most-recent
    # INSIGHTS_LOOKBACK_ENTRIES rows - once true 24h volume exceeds that
    # cap (the common case once a demo has been running a while), counting
    # from the capped sample just reports the cap back as a permanently
    # flat number instead of the actual total. See
    # couchbase_client.access_log_decision_aggregate_since (fetched
    # concurrently above as decision_rows).
    decision_counts = {row.get("decision"): int(row.get("count") or 0) for row in decision_rows}
    decisions = {
        "ALLOW": decision_counts.get("ALLOW", 0),
        "DENY": decision_counts.get("DENY", 0),
        "ERROR": decision_counts.get("ERROR", 0),
    }

    events_24h = sum(decisions.values())
    deny_rate_pct = round((decisions["DENY"] / events_24h) * 100, 1) if events_24h else 0.0

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "events_examined": len(log_entries),
        "summary": {
            "registered_servers": len(servers),
            "trusted_servers": sum(1 for s in servers if s.get("trust_status") == "trusted"),
            "tools_ingested": len(tools),
            "quarantined_tools": sum(1 for t in tools if t.get("trust_status") == "quarantined"),
            "roles": len(ROLES),
            "access_events_24h": events_24h,
            "deny_rate_pct": deny_rate_pct,
            "open_findings": len(findings),
        },
        "decision_breakdown": decisions,
        "action_breakdown": actions,
        "hourly_volume": hourly,
        "top_findings": findings[:5],
    }


@app.get("/v1/topology")
@cached_dashboard_response
async def topology(window_hours: int = 24):
    """Live connectivity graph for the Dashboard: which RBAC roles have
    actually reached which MCP tool servers and which LLM providers in the
    last `window_hours` - not just what's registered, but what's active.
    Two GROUP BY aggregates (never a raw per-event fetch - see
    couchbase_client.access_log_role_server_aggregate_since /
    llm_role_provider_aggregate_since) plus the code-defined role/provider
    catalogs and the server registry."""
    window_hours = max(1, min(window_hours, 24 * 7))
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - window_hours * 3600))

    servers, tool_agg_rows, llm_agg_rows = await asyncio.gather(
        store.list_servers(),
        store.access_log_role_server_aggregate_since(since),
        store.llm_role_provider_aggregate_since(since),
    )

    server_calls: dict[str, int] = {}
    server_last: dict[str, str] = {}
    agent_tool_calls: dict[str, int] = {}
    agent_last: dict[str, str] = {}
    edges_agent_server = []
    for row in tool_agg_rows:
        role, server_id = row.get("role"), row.get("server_id")
        count, last_at = int(row.get("count") or 0), row.get("last_at")
        edges_agent_server.append({"role": role, "server_id": server_id, "count": count, "last_at": last_at})
        server_calls[server_id] = server_calls.get(server_id, 0) + count
        server_last[server_id] = max(last_at or "", server_last.get(server_id, ""))
        agent_tool_calls[role] = agent_tool_calls.get(role, 0) + count
        agent_last[role] = max(last_at or "", agent_last.get(role, ""))

    provider_calls: dict[str, int] = {}
    provider_last: dict[str, str] = {}
    agent_llm_calls: dict[str, int] = {}
    edges_agent_llm = []
    for row in llm_agg_rows:
        role, provider = row.get("role"), row.get("provider")
        count, last_at = int(row.get("count") or 0), row.get("last_at")
        edges_agent_llm.append({"role": role, "provider": provider, "count": count, "last_at": last_at})
        provider_calls[provider] = provider_calls.get(provider, 0) + count
        provider_last[provider] = max(last_at or "", provider_last.get(provider, ""))
        agent_llm_calls[role] = agent_llm_calls.get(role, 0) + count
        agent_last[role] = max(last_at or "", agent_last.get(role, ""))

    agents = [
        {
            "role": role_id,
            "description": description,
            "tool_calls": agent_tool_calls.get(role_id, 0),
            "llm_calls": agent_llm_calls.get(role_id, 0),
            "last_active_at": agent_last.get(role_id) or None,
        }
        for role_id, description in ROLES.items()
    ]

    server_nodes = [
        {
            "server_id": s["server_id"],
            "label": s.get("label") or s["server_id"],
            "owner": s.get("owner"),
            "trust_status": s.get("trust_status"),
            "tool_count": s.get("tool_count", 0),
            "calls": server_calls.get(s["server_id"], 0),
            "last_active_at": server_last.get(s["server_id"]) or None,
        }
        for s in servers
    ]

    llm_providers = [
        {
            "provider": key,
            "label": spec.get("label", key),
            "vendor": spec.get("vendor", key),
            "configured": bool((LLM_API_KEYS.get(key) or "").strip()),
            "caching_enabled": bool(llm_config.get("enabled")),
            "is_default": key == llm_config.get("provider"),
            "calls": provider_calls.get(key, 0),
            "last_active_at": provider_last.get(key) or None,
        }
        for key, spec in llm_cache.PROVIDERS.items()
    ]

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_hours": window_hours,
        "agents": agents,
        "servers": server_nodes,
        "llm_providers": llm_providers,
        "edges": {"agent_server": edges_agent_server, "agent_llm": edges_agent_llm},
    }


# ---------------------------------------------------------------------------
# LLM response caching for agents
# ---------------------------------------------------------------------------
# Same shape as the tool gateway: the agent authenticates here, the
# operations manager decides, and every decision is recorded. The difference
# is that the decision is "has this already been answered?" - and when the
# answer is yes, no tokens leave the building.
class LLMCompleteRequest(BaseModel):
    prompt: str
    provider: str | None = None
    model: str | None = None
    namespace: str | None = None
    bypass_cache: bool = False
    # False = exact-match only for this call: no semantic lookup and no
    # embedding stored. For prompts that embed data (a query result, a
    # document), where a near-identical prompt can carry different facts.
    # None/True follow the cache policy.
    semantic: bool | None = None
    params: dict = Field(default_factory=dict)


class LLMConfigRequest(BaseModel):
    config: dict


class PurgeCacheRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    namespace: str | None = None


@app.get("/v1/llm/providers")
async def llm_providers():
    """The selectable LLMs - Claude, ChatGPT and Gemini - with their models
    and list-price estimates, plus whether each one has an API key
    configured. Keys themselves are never returned."""
    return {"providers": llm_cache.provider_catalog(LLM_API_KEYS)}


@app.get("/v1/llm/config")
async def get_llm_config():
    return {
        "config": llm_config,
        "config_version": llm_config_version,
        "defaults": llm_cache.DEFAULT_CACHE_CONFIG,
        "cache_scopes": list(llm_cache.CACHE_SCOPES),
        "eviction_policies": list(llm_cache.EVICTION_POLICIES),
        "last_sweep_at": last_llm_sweep_at,
        "cached_entries": await store.count_cache_entries(),
    }


@app.put("/v1/llm/config")
async def put_llm_config(req: LLMConfigRequest):
    """Save the cache policy. Every value is re-validated server-side (see
    llm_cache.normalize_config) - the setup form is a convenience, not the
    boundary.

    If the change moves the config fingerprint and `invalidate_on_config_change`
    is on, an immediate sweep runs so the user sees the invalidation they just
    asked for rather than waiting for the next timer tick."""
    previous_version = llm_config_version
    previous_model = llm_config.get("model")
    await save_llm_config(req.config)

    config_changed = llm_config.get("invalidate_on_config_change") and llm_config_version != previous_version
    model_changed = llm_config.get("invalidate_on_model_change") and llm_config.get("model") != previous_model
    invalidated = await sweep_llm_cache() if (config_changed or model_changed) else 0
    return {
        "config": llm_config,
        "config_version": llm_config_version,
        "entries_invalidated": invalidated,
    }


@app.post("/v1/llm/complete")
async def llm_complete(
    req: LLMCompleteRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    role, subject = await authenticate(authorization, request)
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    if not store.connected:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")

    ctx = trace_context(request)
    # The token and spend budgets are checked here, before the provider is
    # called: a caller whose hourly tokens are already gone is refused
    # rather than allowed one more expensive miss. A cache *hit* costs
    # nothing and is still refused when the budget is out, which is
    # deliberate - a budget that quietly keeps serving is not a budget, and
    # a caller past its ceiling should find that out at a moment that is
    # cheap for everyone.
    limit_verdicts = await check_limits(
        role, subject, ["tokens", "spend"], trace_id=ctx["trace_id"]
    )
    await enforce_or_log(
        limit_verdicts, ctx, action="complete", role=role, subject=subject,
        query=req.prompt[:200], started=time.time(),
    )

    cfg = dict(llm_config)
    if req.namespace:
        cfg["namespace"] = req.namespace
        cfg = llm_cache.normalize_config(cfg)
    if req.semantic is False:
        # Applied after normalize_config so it only ever narrows the policy.
        cfg["semantic_enabled"] = False

    provider = req.provider or cfg["provider"]
    if provider not in llm_cache.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}' (expected one of {list(llm_cache.PROVIDERS)})")
    model = req.model or (cfg["model"] if provider == cfg["provider"] else llm_cache.PROVIDERS[provider]["default_model"])
    if model not in llm_cache.PROVIDERS[provider]["models"]:
        raise HTTPException(status_code=400, detail=f"Model '{model}' is not offered by provider '{provider}'")

    start = time.time()
    scope = llm_cache.scope_key(cfg, role, subject)
    eid = llm_cache.entry_id(cfg, provider, model, req.prompt, req.params, scope)

    # Guardrails run before the cache is consulted, because whether the
    # prompt carries personal data decides whether it may be cached at all.
    prompt_guard = None
    if guardrails_config.get("enabled") and guardrails_config.get("scan_prompts"):
        prompt_guard = guardrails.inspect_input(req.prompt, guardrails_config)
        if prompt_guard["blocked"]:
            await store.log_access(
                action="complete", role=role, subject_label=subject,
                query=redact_text_for_storage(req.prompt[:240]), tool_id=None, server_id=None,
                decision="DENY", reason=f"guardrail: {guardrails.summarize(prompt_guard)}",
                latency_ms=int((time.time() - start) * 1000),
            )
            raise HTTPException(
                status_code=400,
                detail=f"Prompt was refused by the guardrails policy: {guardrails.summarize(prompt_guard)}",
                headers={"X-AOM-Refusal": "guardrail"},
            )

    bypass_reason = None
    if not cfg.get("enabled"):
        bypass_reason = "caching is disabled in the current policy"
    elif req.bypass_cache:
        bypass_reason = "caller requested bypass_cache"
    elif (
        prompt_guard and prompt_guard["pii"]["found"] and guardrails_config.get("never_cache_pii")
    ):
        # Not "cache the redacted version": a cache is only useful while a
        # hit and a miss return the same answer, and redacting an entry
        # breaks that in one direction while storing the original leaks one
        # caller's personal data to the next. So this prompt is simply never
        # cached. See app/guardrails.py.
        kinds = sorted({m["detector"] for m in prompt_guard["pii"]["matches"]})
        bypass_reason = f"prompt contains personal data ({', '.join(kinds)}) - never cached"
    else:
        bypass_reason = llm_cache.is_bypassed(req.prompt, cfg, role)

    # ---- read path -------------------------------------------------------
    if not bypass_reason:
        hit, similarity, invalidation = await _lookup_cache(cfg, provider, model, scope, eid, req.prompt)
        if hit:
            latency_ms = int((time.time() - start) * 1000)
            updated = await _record_cache_hit(hit, cfg, similarity is not None)
            saved_ms = max(0, int(hit.get("origin_latency_ms") or 0) - latency_ms)
            outcome = "hit_semantic" if similarity is not None else "hit_exact"
            # All-time counters (never windowed, never expire) - see
            # CouchbaseStore.increment_lifetime_stats for why the dashboard's
            # headline savings figures, and the per-model Cost saved/Cost
            # spent columns, need a separate, non-plateauing source.
            await store.increment_lifetime_stats(
                tokens_saved_delta=int(hit.get("total_tokens", 0) or 0),
                cost_saved_delta=float(hit.get("cost_usd", 0.0) or 0.0),
                provider=provider,
                model=model,
            )
            await store.log_llm_event({
                "doc_type": "llm_cache_event",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "outcome": outcome,
                "provider": provider,
                "model": model,
                "role": role,
                "subject": subject,
                "entry_id": hit["entry_id"],
                "namespace": cfg["namespace"],
                "scope_key": scope,
                "similarity": similarity,
                "prompt_preview": req.prompt.strip()[:240],
                "prompt_tokens": hit.get("prompt_tokens", 0),
                "completion_tokens": hit.get("completion_tokens", 0),
                "total_tokens": hit.get("total_tokens", 0),
                "tokens_saved": hit.get("total_tokens", 0),
                "cost_usd": 0.0,
                "cost_saved_usd": hit.get("cost_usd", 0.0),
                "latency_ms": latency_ms,
                "latency_saved_ms": saved_ms,
                "reason": f"served from cache ({outcome.replace('hit_', '')} match)",
            })
            await record_span(
                ctx, kind="llm", name=f"{provider}:{model}", started=start, role=role, subject=subject,
                attributes={
                    "gen_ai.system": provider,
                    "gen_ai.request.model": model,
                    "gen_ai.usage.input_tokens": hit.get("prompt_tokens", 0),
                    "gen_ai.usage.output_tokens": hit.get("completion_tokens", 0),
                    "gen_ai.prompt": redact_text_for_storage(req.prompt.strip()[:2000]),
                    "aom.cache_status": outcome,
                    "aom.cache_entry_id": hit["entry_id"],
                    "aom.similarity": similarity,
                    "aom.cost_usd": 0.0,
                    "aom.tokens_saved": hit.get("total_tokens", 0),
                    "aom.namespace": cfg["namespace"],
                },
            )
            return {
                "provider": provider,
                "model": model,
                "role": role,
                "response": hit.get("response", ""),
                "cache": {
                    "status": outcome,
                    "entry_id": hit["entry_id"],
                    "similarity": similarity,
                    "hit_count": updated,
                    "created_at": hit.get("created_at"),
                    "reason": invalidation,
                },
                "usage": {
                    "prompt_tokens": hit.get("prompt_tokens", 0),
                    "completion_tokens": hit.get("completion_tokens", 0),
                    "total_tokens": hit.get("total_tokens", 0),
                },
                "cost_usd": 0.0,
                "tokens_saved": hit.get("total_tokens", 0),
                "cost_saved_usd": hit.get("cost_usd", 0.0),
                "latency_ms": latency_ms,
                "stub": bool(hit.get("stub")),
                "trace": {"trace_id": ctx["trace_id"]},
            }

    # ---- miss path -------------------------------------------------------
    try:
        result = await asyncio.to_thread(
            llm_cache.call_provider, provider, model, req.prompt, cfg, LLM_API_KEYS,
            int(governance_config.get("llm_timeout_seconds") or 60),
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.time() - start) * 1000)
        await store.log_llm_event({
            "doc_type": "llm_cache_event",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outcome": "error", "provider": provider, "model": model, "role": role, "subject": subject,
            "namespace": cfg["namespace"], "scope_key": scope,
            "prompt_preview": req.prompt.strip()[:240],
            "latency_ms": latency_ms, "reason": str(exc)[:400],
        })
        await record_span(
            ctx, kind="llm", name=f"{provider}:{model}", started=start, status="error",
            role=role, subject=subject,
            attributes={
                "gen_ai.system": provider,
                "gen_ai.request.model": model,
                "aom.cache_status": "error",
                "aom.namespace": cfg["namespace"],
            },
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=f"{llm_cache.PROVIDERS[provider]['label']} call failed: {exc}") from exc

    latency_ms = int((time.time() - start) * 1000)
    prompt_tokens = int(result["prompt_tokens"])
    completion_tokens = int(result["completion_tokens"])
    total_tokens = prompt_tokens + completion_tokens
    cost_usd = llm_cache.estimate_cost_usd(model, prompt_tokens, completion_tokens)

    # The prompt can be clean and the answer not - a lookup by customer ID
    # returns the customer. Checked after the call for the same reason
    # response poisoning is: the text does not exist until then.
    if not bypass_reason and guardrails_config.get("enabled") and guardrails_config.get("never_cache_pii"):
        response_pii = guardrails.scan_pii(result.get("text") or "", guardrails_config)
        if response_pii["found"]:
            kinds = sorted({m["detector"] for m in response_pii["matches"]})
            bypass_reason = f"answer contains personal data ({', '.join(kinds)}) - never cached"

    if not bypass_reason:
        await _store_cache_entry(
            eid, cfg, provider, model, scope, req.prompt, result,
            prompt_tokens, completion_tokens, cost_usd, latency_ms,
            override=(provider != cfg["provider"] or model != cfg["model"]),
        )
        await store.increment_lifetime_stats(cost_spent_delta=cost_usd, provider=provider, model=model)

    await store.log_llm_event({
        "doc_type": "llm_cache_event",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "outcome": "bypass" if bypass_reason else "miss",
        "provider": provider, "model": model, "role": role, "subject": subject,
        "entry_id": None if bypass_reason else eid,
        "namespace": cfg["namespace"], "scope_key": scope, "similarity": None,
        "prompt_preview": redact_text_for_storage(req.prompt.strip()[:240]),
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens,
        "tokens_saved": 0, "cost_usd": cost_usd, "cost_saved_usd": 0.0,
        "latency_ms": latency_ms, "latency_saved_ms": 0,
        "reason": bypass_reason or "no cached answer - called the provider and stored the result",
    })

    # What this call actually consumed, added to the rolling budget counters.
    # A cache hit deliberately adds nothing: the whole argument for the cache
    # is that the tokens were never spent, so charging a caller for them
    # would make the budget disagree with the invoice.
    await record_usage(role, subject, tokens=total_tokens, cost_usd=cost_usd)

    await record_span(
        ctx, kind="llm", name=f"{provider}:{model}", started=start, role=role, subject=subject,
        attributes={
            "gen_ai.system": provider,
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": prompt_tokens,
            "gen_ai.usage.output_tokens": completion_tokens,
            "gen_ai.prompt": redact_text_for_storage(req.prompt.strip()[:2000]),
            "aom.guardrail_pii": bool(prompt_guard and prompt_guard["pii"]["found"]),
            "aom.cache_status": "bypass" if bypass_reason else "miss",
            "aom.cache_entry_id": None if bypass_reason else eid,
            "aom.cost_usd": cost_usd,
            "aom.namespace": cfg["namespace"],
            "aom.stub": bool(result.get("stub")),
        },
    )

    return {
        "provider": provider,
        "model": model,
        "role": role,
        "response": result["text"],
        "cache": {
            "status": "bypass" if bypass_reason else "miss",
            "entry_id": None if bypass_reason else eid,
            "similarity": None,
            "hit_count": 0,
            "created_at": None,
            "reason": bypass_reason,
        },
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "cost_usd": cost_usd,
        "tokens_saved": 0,
        "cost_saved_usd": 0.0,
        "latency_ms": latency_ms,
        "stub": bool(result.get("stub")),
        "trace": {"trace_id": ctx["trace_id"]},
    }


async def _lookup_cache(cfg: dict, provider: str, model: str, scope: str, eid: str, prompt: str):
    """Exact first (one KV get on a deterministic ID), then the semantic
    fallback if it's enabled. Anything the policy says is invalid is deleted
    on the spot rather than left to the sweeper - a read that noticed the
    problem is the cheapest place to fix it."""
    entry = await store.get_cache_entry(eid)
    if entry:
        state, reason = llm_cache.evaluate_entry(
            entry, cfg, config_version=llm_config_version, catalog_version=llm_catalog_version
        )
        if state in ("fresh", "stale"):
            return entry, None, reason
        await store.delete_cache_entry(eid)

    if not cfg.get("semantic_enabled") or embeddings is None:
        return None, None, None

    try:
        vector = await embeddings.embed_async(prompt)
    except ValueError:
        return None, None, None

    candidates = await store.semantic_cache_lookup(
        provider, model, scope, cfg["namespace"], vector, top_k=int(cfg["semantic_candidates"])
    )
    threshold = float(cfg["similarity_threshold"])
    for candidate in candidates:
        if candidate["similarity"] < threshold:
            break
        entry = await store.get_cache_entry(candidate["entry_id"])
        if not entry:
            continue
        state, reason = llm_cache.evaluate_entry(
            entry, cfg, config_version=llm_config_version, catalog_version=llm_catalog_version
        )
        if state in ("fresh", "stale"):
            return entry, candidate["similarity"], reason
        await store.delete_cache_entry(candidate["entry_id"])
    return None, None, None


async def _record_cache_hit(entry: dict, cfg: dict, semantic: bool) -> int:
    """Bump the hit counters and the running savings total on the entry.

    Re-written with the *remaining* TTL as its expiry, never a fresh one: a
    popular entry must still age out on schedule, otherwise a hot prompt
    would never be re-verified against the provider."""
    now = time.time()
    entry["hit_count"] = int(entry.get("hit_count") or 0) + 1
    entry["last_hit_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if semantic:
        entry["semantic_hits"] = int(entry.get("semantic_hits") or 0) + 1
    else:
        entry["exact_hits"] = int(entry.get("exact_hits") or 0) + 1
    entry["tokens_saved"] = int(entry.get("tokens_saved") or 0) + int(entry.get("total_tokens") or 0)
    entry["cost_saved_usd"] = round(float(entry.get("cost_saved_usd") or 0.0) + float(entry.get("cost_usd") or 0.0), 6)

    ttl = int(cfg.get("ttl_seconds") or 0)
    remaining = 0
    if ttl:
        age = now - llm_cache.parse_timestamp(entry.get("created_at"))
        remaining = max(1, int(ttl + int(cfg.get("stale_while_revalidate_seconds") or 0) - age))
    await store.upsert_cache_entry(entry["entry_id"], entry, ttl_seconds=remaining)
    return entry["hit_count"]


async def _store_cache_entry(
    eid, cfg, provider, model, scope, prompt, result,
    prompt_tokens, completion_tokens, cost_usd, latency_ms, override=False,
):
    embedding = None
    if cfg.get("semantic_enabled") and embeddings is not None:
        try:
            embedding = await embeddings.embed_async(prompt)
        except ValueError:
            embedding = None

    text = result["text"]
    doc = {
        "doc_type": "llm_cache_entry",
        "entry_id": eid,
        "provider": provider,
        "model": model,
        "scope_key": scope,
        "namespace": cfg["namespace"],
        "prompt": prompt,
        "prompt_preview": prompt.strip()[:240],
        "response": text,
        "response_preview": (text or "").strip()[:240],
        "embedding": embedding,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": cost_usd,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_hit_at": None,
        "hit_count": 0,
        "exact_hits": 0,
        "semantic_hits": 0,
        "tokens_saved": 0,
        "cost_saved_usd": 0.0,
        "origin_latency_ms": latency_ms,
        "config_version": llm_config_version,
        "catalog_version": llm_catalog_version,
        # True when this call named a provider/model other than the policy's
        # default. Such entries survive a change to the selected model - see
        # llm_cache.evaluate_entry.
        "override": bool(override),
        "stub": bool(result.get("stub")),
    }
    ttl = int(cfg.get("ttl_seconds") or 0)
    expiry = ttl + int(cfg.get("stale_while_revalidate_seconds") or 0) if ttl else 0
    await store.upsert_cache_entry(eid, doc, ttl_seconds=expiry)


@app.get("/v1/llm/cache")
async def list_llm_cache(limit: int = 100):
    """Cache contents with each entry's live policy verdict attached, so the
    table shows what the gateway would actually do with it right now."""
    entries = await store.list_cache_entries(limit=min(limit, 500))
    now = time.time()
    for entry in entries:
        state, reason = llm_cache.evaluate_entry(
            entry, llm_config, now=now,
            config_version=llm_config_version, catalog_version=llm_catalog_version,
        )
        entry["state"] = state
        entry["state_reason"] = reason
        entry["age_seconds"] = int(max(0, now - llm_cache.parse_timestamp(entry.get("created_at"))))
    return {"entries": entries, "count": len(entries), "total_entries": await store.count_cache_entries()}


@app.post("/v1/llm/cache/purge")
async def purge_llm_cache(req: PurgeCacheRequest):
    """Manual invalidation: everything, or narrowed to one provider, model
    or namespace."""
    removed = await store.purge_cache(provider=req.provider, model=req.model, namespace=req.namespace)
    return {"purged": removed, "provider": req.provider, "model": req.model, "namespace": req.namespace}


@app.post("/v1/llm/cache/sweep")
async def sweep_llm_cache_route():
    """Run the invalidation sweeper now instead of waiting for the timer."""
    global last_llm_sweep_at
    removed = await sweep_llm_cache()
    last_llm_sweep_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"removed": removed, "last_sweep_at": last_llm_sweep_at}


@app.delete("/v1/llm/cache/{entry_id:path}")
async def delete_llm_cache_entry(entry_id: str):
    if not await store.delete_cache_entry(entry_id):
        raise HTTPException(status_code=404, detail="Cache entry not found")
    return {"deleted": True, "entry_id": entry_id}


@app.get("/v1/llm/dashboard")
@cached_dashboard_response
async def llm_dashboard():
    # All-time totals - never windowed, never reset by the event log's TTL.
    # See CouchbaseStore.increment_lifetime_stats/get_lifetime_stats. Its
    # per-model breakdown feeds the aggregate below; all four reads run
    # concurrently.

    # ONE GROUP BY query covers the 24h summary/donut, the "Savings by
    # provider & model" breakdown, AND (via its trailing 12h of hour
    # buckets) the trend chart - see CouchbaseStore.
    # llm_dashboard_aggregate_since / llm_cache.build_dashboard_aggregate
    # for why three separate full-window scans became one. Also runs as a
    # pure index scan (no per-document KV fetch) via idx_llm_cache_log_agg
    # (couchbase-init/init.sh) - at real throughput (~230k+ events/24h)
    # that combination is most of what made this route slow.
    window_hours = 24
    trend_hours = 12
    window_since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - window_hours * 3600))
    lifetime, agg_rows, recent_events, cached_entries = await asyncio.gather(
        store.get_lifetime_stats(),
        store.llm_dashboard_aggregate_since(window_since),
        store.recent_llm_events(limit=50),
        store.count_cache_entries(),
    )
    dashboard_data = llm_cache.build_dashboard_aggregate(
        agg_rows, trend_hours=trend_hours, lifetime_by_model=lifetime.get("by_model")
    )

    # recent_events ("Recent cache events") is a small fixed-count fetch,
    # fetched concurrently with the aggregate above.

    provider_spec = llm_cache.PROVIDERS[llm_config["provider"]]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "events_examined": dashboard_data["summary"]["requests"],
        "tokens_saved_total": int(lifetime.get("tokens_saved_total") or 0),
        "cost_saved_usd_total": round(float(lifetime.get("cost_saved_usd_total") or 0.0), 4),
        "enabled": llm_config["enabled"],
        "provider": llm_config["provider"],
        "provider_label": provider_spec["label"],
        "model": llm_config["model"],
        "api_key_configured": bool((LLM_API_KEYS.get(llm_config["provider"]) or "").strip()),
        "semantic_enabled": llm_config["semantic_enabled"],
        "similarity_threshold": llm_config["similarity_threshold"],
        "ttl_seconds": llm_config["ttl_seconds"],
        "cached_entries": cached_entries,
        "max_entries": llm_config["max_entries"],
        "last_sweep_at": last_llm_sweep_at,
        "summary": dashboard_data["summary"],
        "hourly": dashboard_data["hourly"],
        "model_breakdown": dashboard_data["model_breakdown"],
        "recent_events": recent_events,
    }




# ---------------------------------------------------------------------------
# Context caching for agents
# ---------------------------------------------------------------------------
# Same shape as LLM response caching above, for any agent-fetched context or
# data - not model completions - see app/context_cache.py's module
# docstring for what's different (exact-key matching only, latency instead
# of tokens/cost) and why. Any agent using the SDK's context_get()/
# context_set() lands here, not just one demo - that is the whole point of
# giving this its own gateway instead of leaving every agent to cache
# against its own private Couchbase bucket, invisible to this appliance.
class ContextGetRequest(BaseModel):
    key: str
    namespace: str | None = None


class ContextSetRequest(BaseModel):
    key: str
    value: Any
    namespace: str | None = None
    ttl_seconds: int | None = None
    source_latency_ms: int | None = None


class ContextConfigRequest(BaseModel):
    config: dict


class PurgeContextCacheRequest(BaseModel):
    namespace: str | None = None
    agent: str | None = None


@app.get("/v1/context/config")
async def get_context_config():
    return {
        "config": context_config,
        "defaults": context_cache.DEFAULT_CACHE_CONFIG,
        "cache_scopes": list(context_cache.CACHE_SCOPES),
        "eviction_policies": list(context_cache.EVICTION_POLICIES),
        "last_sweep_at": last_context_sweep_at,
        "cached_entries": await store.count_context_entries(),
    }


@app.put("/v1/context/config")
async def put_context_config(req: ContextConfigRequest):
    """Save the context cache policy. Every value is re-validated
    server-side (see context_cache.normalize_config) - the setup form is a
    convenience, not the boundary."""
    await save_context_config(req.config)
    return {"config": context_config}


@app.post("/v1/context/get")
async def context_get(
    req: ContextGetRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Look up one cached value by key - exact match only (see
    app/context_cache.py for why semantic matching doesn't apply to
    opaque agent-supplied values)."""
    role, subject = await authenticate(authorization, request)
    if not req.key or not req.key.strip():
        raise HTTPException(status_code=400, detail="key is required")
    if not store.connected:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")

    ctx = trace_context(request)
    start = time.time()
    cfg = dict(context_config)
    if req.namespace:
        cfg["namespace"] = req.namespace
        cfg = context_cache.normalize_config(cfg)
    namespace = cfg["namespace"]
    scope = context_cache.scope_key(cfg, role, subject)
    eid = context_cache.entry_id(cfg, namespace, scope, req.key)

    entry = None
    if cfg.get("enabled"):
        entry = await store.get_context_entry(eid)
        if entry:
            state, _reason = context_cache.evaluate_entry(entry, cfg)
            if state != "fresh":
                await store.delete_context_entry(eid)
                entry = None

    latency_ms = int((time.time() - start) * 1000)

    if entry:
        entry["hit_count"] = int(entry.get("hit_count") or 0) + 1
        entry["last_hit_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ttl = entry.get("ttl_seconds")
        await store.upsert_context_entry(eid, entry, ttl_seconds=int(ttl) if ttl else 0)
        saved_ms = max(0, int(entry.get("origin_latency_ms") or 0) - latency_ms)
        await store.increment_context_lifetime_stats(hit=True, latency_saved_ms=saved_ms)
        await store.log_context_event({
            "doc_type": "context_cache_event",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outcome": "hit",
            "namespace": namespace, "scope_key": scope, "subject": subject, "role": role,
            "key_preview": context_cache.value_preview(req.key, 120),
            "latency_ms": latency_ms, "latency_saved_ms": saved_ms, "value_bytes": 0,
        })
        await record_span(
            ctx, kind="internal", name="context cache: hit", started=start, role=role, subject=subject,
            attributes={
                "aom.operation": "context_cache.get", "aom.cache_status": "hit",
                "aom.namespace": namespace, "aom.cache_entry_id": eid,
                "aom.latency_saved_ms": saved_ms,
            },
        )
        return {
            "hit": True,
            "value": entry.get("value"),
            "cache": {
                "status": "hit", "entry_id": eid, "hit_count": entry["hit_count"],
                "created_at": entry.get("created_at"),
            },
            "latency_ms": latency_ms,
            "trace": {"trace_id": ctx["trace_id"]},
        }

    await store.increment_context_lifetime_stats(hit=False)
    await store.log_context_event({
        "doc_type": "context_cache_event",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "outcome": "miss",
        "namespace": namespace, "scope_key": scope, "subject": subject, "role": role,
        "key_preview": context_cache.value_preview(req.key, 120),
        "latency_ms": latency_ms, "latency_saved_ms": 0, "value_bytes": 0,
    })
    await record_span(
        ctx, kind="internal", name="context cache: miss", started=start, role=role, subject=subject,
        attributes={"aom.operation": "context_cache.get", "aom.cache_status": "miss", "aom.namespace": namespace},
    )
    return {
        "hit": False,
        "value": None,
        "cache": {"status": "miss", "entry_id": eid, "hit_count": 0, "created_at": None},
        "latency_ms": latency_ms,
        "trace": {"trace_id": ctx["trace_id"]},
    }


@app.post("/v1/context/set")
async def context_set(
    req: ContextSetRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Store one value the agent just fetched from its own data source, so
    the next context_get() for the same key is a Couchbase KV get instead
    of a live fetch. `source_latency_ms` (how long the real fetch took) is
    what makes a future hit's "latency avoided" figure meaningful - see
    app/context_cache.py's module docstring."""
    role, subject = await authenticate(authorization, request)
    if not req.key or not req.key.strip():
        raise HTTPException(status_code=400, detail="key is required")
    if not store.connected:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")
    if not context_config.get("enabled"):
        raise HTTPException(status_code=409, detail="Context caching is disabled in the current policy")

    ctx = trace_context(request)
    start = time.time()
    cfg = dict(context_config)
    if req.namespace:
        cfg["namespace"] = req.namespace
        cfg = context_cache.normalize_config(cfg)
    namespace = cfg["namespace"]
    scope = context_cache.scope_key(cfg, role, subject)
    eid = context_cache.entry_id(cfg, namespace, scope, req.key)

    value_bytes = context_cache.value_size_bytes(req.value)
    if value_bytes > int(cfg.get("max_value_bytes") or 0):
        raise HTTPException(
            status_code=413,
            detail=f"value is {value_bytes} bytes, over the {cfg['max_value_bytes']}-byte policy limit",
        )

    ttl_seconds = req.ttl_seconds if req.ttl_seconds is not None else int(cfg.get("ttl_seconds") or 0)
    doc = {
        "doc_type": "context_cache_entry",
        "entry_id": eid,
        "namespace": namespace,
        "scope_key": scope,
        "subject": subject,
        "role": role,
        "key_preview": context_cache.value_preview(req.key, 120),
        "value": req.value,
        "value_preview": context_cache.value_preview(req.value),
        "value_bytes": value_bytes,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_hit_at": None,
        "hit_count": 0,
        "ttl_seconds": ttl_seconds,
        "origin_latency_ms": int(req.source_latency_ms or 0),
    }
    await store.upsert_context_entry(eid, doc, ttl_seconds=ttl_seconds)

    latency_ms = int((time.time() - start) * 1000)
    await store.log_context_event({
        "doc_type": "context_cache_event",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "outcome": "write",
        "namespace": namespace, "scope_key": scope, "subject": subject, "role": role,
        "key_preview": doc["key_preview"],
        "latency_ms": latency_ms, "latency_saved_ms": 0, "value_bytes": value_bytes,
    })
    await record_span(
        ctx, kind="internal", name="context cache: write", started=start, role=role, subject=subject,
        attributes={
            "aom.operation": "context_cache.set", "aom.namespace": namespace,
            "aom.cache_entry_id": eid, "aom.value_bytes": value_bytes,
        },
    )

    # Overflow eviction checked here too, not just from the periodic
    # sweeper - a burst of writes is exactly when max_entries gets
    # exceeded, and the cheapest place to notice is the write that pushed
    # it over.
    # Inline over-capacity eviction, throttled. This used to run on every
    # write once the cache was full - a COUNT plus a fetch-and-sort of up to
    # 20,000 entries per context.set - which under sustained agent traffic
    # kept Couchbase and the worker pool busy and slowed every dashboard
    # page with it. The periodic sweeper (sweep_context_cache) applies the
    # same policy; this just stops a burst overshooting max_entries for long.
    global _last_inline_context_eviction
    if (
        int(cfg.get("max_entries") or 0)
        and time.monotonic() - _last_inline_context_eviction >= CONTEXT_INLINE_EVICTION_INTERVAL_SECONDS
    ):
        _last_inline_context_eviction = time.monotonic()
        total = await store.count_context_entries()
        if total > int(cfg["max_entries"]):
            entries = await store.list_context_entries(limit=min(total, 20000))
            for stale_id in context_cache.select_evictions(entries, cfg):
                await store.delete_context_entry(stale_id)

    return {
        "stored": True,
        "entry_id": eid,
        "ttl_seconds": ttl_seconds,
        "latency_ms": latency_ms,
        "trace": {"trace_id": ctx["trace_id"]},
    }


@app.get("/v1/context/cache")
async def list_context_cache(limit: int = 100):
    """Cache contents with each entry's live policy verdict attached, so
    the table shows what the gateway would actually do with it right now."""
    entries = await store.list_context_entries(limit=min(limit, 500))
    now = time.time()
    for entry in entries:
        state, reason = context_cache.evaluate_entry(entry, context_config, now=now)
        entry["state"] = state
        entry["state_reason"] = reason
        entry["age_seconds"] = int(max(0, now - context_cache.parse_timestamp(entry.get("created_at"))))
    return {"entries": entries, "count": len(entries), "total_entries": await store.count_context_entries()}


@app.post("/v1/context/cache/purge")
async def purge_context_cache_route(req: PurgeContextCacheRequest):
    """Manual invalidation: everything, or narrowed to one namespace or
    calling agent."""
    removed = await store.purge_context_cache(namespace=req.namespace, agent=req.agent)
    return {"purged": removed, "namespace": req.namespace, "agent": req.agent}


@app.post("/v1/context/cache/sweep")
async def sweep_context_cache_route():
    """Run the invalidation sweeper now instead of waiting for the timer."""
    global last_context_sweep_at
    removed = await sweep_context_cache()
    last_context_sweep_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"removed": removed, "last_sweep_at": last_context_sweep_at}


@app.delete("/v1/context/cache/{entry_id:path}")
async def delete_context_cache_entry(entry_id: str):
    if not await store.delete_context_entry(entry_id):
        raise HTTPException(status_code=404, detail="Cache entry not found")
    return {"deleted": True, "entry_id": entry_id}


@app.get("/v1/context/dashboard")
@cached_dashboard_response
async def context_dashboard():
    # All-time totals - never windowed. See CouchbaseStore.
    # get_context_lifetime_stats/increment_context_lifetime_stats.

    # ONE GROUP BY query covers the 24h summary, the "Traffic by agent &
    # namespace" breakdown, AND (via its trailing 12h of hour buckets) the
    # trend chart - see CouchbaseStore.context_dashboard_aggregate_since /
    # context_cache.build_dashboard_aggregate.
    window_hours = 24
    trend_hours = 12
    window_since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - window_hours * 3600))
    lifetime, agg_rows, recent_events, cached_entries = await asyncio.gather(
        store.get_context_lifetime_stats(),
        store.context_dashboard_aggregate_since(window_since),
        store.recent_context_events(limit=50),
        store.count_context_entries(),
    )
    dashboard_data = context_cache.build_dashboard_aggregate(agg_rows, trend_hours=trend_hours, lifetime=lifetime)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "enabled": context_config["enabled"],
        "ttl_seconds": context_config["ttl_seconds"],
        "cache_scope": context_config["cache_scope"],
        "cached_entries": cached_entries,
        "max_entries": context_config["max_entries"],
        "last_sweep_at": last_context_sweep_at,
        "lookups_total": dashboard_data["lookups_total"],
        "hits_total": dashboard_data["hits_total"],
        "latency_saved_ms_total": dashboard_data["latency_saved_ms_total"],
        "summary": dashboard_data["summary"],
        "hourly": dashboard_data["hourly"],
        "agent_breakdown": dashboard_data["agent_breakdown"],
        "recent_events": recent_events,
    }


# ---------------------------------------------------------------------------
# Agent memory (see app/agent_memory.py; storage/search in couchbase_client.py)
# ---------------------------------------------------------------------------
class AddMemoryRequest(BaseModel):
    user_id: str
    content: str
    session_id: str | None = None
    memory_type: str = agent_memory.DEFAULT_MEMORY_TYPE
    metadata: dict = Field(default_factory=dict)
    ttl_seconds: int = 0


class SearchMemoryRequest(BaseModel):
    user_id: str
    query: str
    session_id: str | None = None
    memory_type: str | None = None
    top_k: int = 5


class ClearMemoryRequest(BaseModel):
    user_id: str
    session_id: str | None = None


@app.post("/v1/memory")
async def add_memory(req: AddMemoryRequest, authorization: str | None = Header(default=None)):
    """Store one memory entry for `user_id`, embedded for later semantic
    recall via POST /v1/memory/search. Authenticated exactly like
    discover/invoke/complete - any valid API key may write memory, scoped
    by the `user_id` it names rather than by RBAC role."""
    role, subject = await authenticate(authorization)
    if not req.content or not req.content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    if not store.connected or embeddings is None:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")

    start = time.time()
    memory_id = agent_memory.new_memory_id(req.user_id)

    # Memory is the longest-lived text this appliance holds - it outlives
    # sessions by design - so it is the write path where redaction matters
    # most. The entry is embedded from the redacted text too, so recall
    # cannot be made to work by matching on the personal data itself.
    content = req.content
    metadata = req.metadata
    if guardrails_config.get("enabled") and guardrails_config.get("redact_memory"):
        content = redact_text_for_storage(req.content)
        metadata = redact_for_storage(req.metadata)

    embedding_text = agent_memory.build_embedding_text(content, metadata)
    embedding = await embeddings.embed_async(embedding_text)
    doc = agent_memory.build_memory_doc(
        user_id=req.user_id, content=content, embedding=embedding,
        session_id=req.session_id, memory_type=req.memory_type, metadata=metadata,
        role=role, subject_label=subject,
    )
    await store.upsert_memory(memory_id, doc, ttl_seconds=req.ttl_seconds)
    latency_ms = int((time.time() - start) * 1000)

    await store.log_access(
        action="memory_add", role=role, subject_label=subject, query=redact_text_for_storage(req.content[:240]),
        tool_id=None, server_id=None, decision="ALLOW",
        reason=f"stored {doc['memory_type']} memory for user '{req.user_id}'", latency_ms=latency_ms,
    )
    return {"memory_id": memory_id, "user_id": req.user_id, "memory_type": doc["memory_type"], "created_at": doc["created_at"]}


@app.get("/v1/memory")
async def list_memory_route(
    user_id: str, session_id: str | None = None, memory_type: str | None = None, limit: int = 100,
    authorization: str | None = Header(default=None),
):
    """Chronological listing for one user (optionally narrowed to a
    session or memory type) - what a fresh agent turn re-hydrates before
    reasoning, or what a debugging session inspects directly."""
    role, subject = await authenticate(authorization)
    if not store.connected:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")
    entries = await store.list_memory(user_id, session_id=session_id, memory_type=memory_type, limit=min(limit, 500))
    await store.log_access(
        action="memory_list", role=role, subject_label=subject, query=None, tool_id=None, server_id=None,
        decision="ALLOW", reason=f"listed {len(entries)} memory entr(ies) for user '{user_id}'", latency_ms=0,
    )
    return {"user_id": user_id, "entries": entries, "count": len(entries)}


@app.post("/v1/memory/search")
async def search_memory_route(
    req: SearchMemoryRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Semantic recall: the memory entries for `user_id` whose content is
    closest to `query`, not just the most recent ones - the same vector
    kNN pattern discover() runs over the tool catalog, scoped to one user's
    memory instead of one role's tools."""
    role, subject = await authenticate(authorization, request)
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query is required")
    if not store.connected or embeddings is None:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")

    ctx = trace_context(request)
    start = time.time()

    vector = await embeddings.embed_async(req.query)
    memory_type = agent_memory.normalize_memory_type(req.memory_type) if req.memory_type else None
    results = await store.search_memory(
        req.user_id, vector, session_id=req.session_id, memory_type=memory_type, top_k=req.top_k,
    )
    latency_ms = int((time.time() - start) * 1000)

    await store.log_access(
        action="memory_search", role=role, subject_label=subject,
        query=redact_text_for_storage(req.query), tool_id=None, server_id=None,
        decision="ALLOW", reason=f"{len(results)} memory match(es) for user '{req.user_id}'", latency_ms=latency_ms,
    )
    # A memory recall is the agent doing something on its own account
    # rather than calling out, so it is an "internal" span - and recording
    # it is what lets one query answer "which memory was in context when
    # the agent picked that tool?", which is the join no observability
    # vendor can do because they never see the memory.
    # Recording that an entry was actually used is the strongest signal
    # importance scoring has. Fire-and-forget: one UPDATE, off the response
    # path, and a failure costs a slightly stale score rather than a failed
    # recall.
    if memory_config.get("enabled") and memory_config.get("track_recall"):
        recalled = [r.get("memory_id") for r in results if r.get("memory_id")]
        if recalled:
            spawn(store.bump_recall_counts(recalled))

    await record_span(
        ctx, kind="internal", name=f"memory recall: {req.user_id}", started=start, role=role, subject=subject,
        attributes={
            "aom.operation": "memory.search",
            "aom.query": redact_text_for_storage(req.query),
            "aom.user_id": req.user_id,
            "aom.session_id": req.session_id,
            "aom.memory_type": memory_type,
            "aom.result_count": len(results),
            "aom.memory_ids": [r.get("memory_id") for r in results],
        },
    )
    return {
        "user_id": req.user_id,
        "entries": results,
        "latency_ms": latency_ms,
        "trace": {"trace_id": ctx["trace_id"]},
    }


@app.delete("/v1/memory/{memory_id:path}")
async def delete_memory_route(memory_id: str, authorization: str | None = Header(default=None)):
    role, subject = await authenticate(authorization)
    deleted = await store.delete_memory(memory_id)
    await store.log_access(
        action="memory_delete", role=role, subject_label=subject, query=None, tool_id=None, server_id=None,
        decision="ALLOW" if deleted else "ERROR",
        reason="deleted" if deleted else "memory entry not found", latency_ms=0,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"deleted": True, "memory_id": memory_id}


@app.post("/v1/memory/clear")
async def clear_memory_route(req: ClearMemoryRequest, authorization: str | None = Header(default=None)):
    """Bulk-wipe a user's memory, or just one session of it - e.g. an agent
    clearing short-term conversational memory at session end while leaving
    that user's durable profile memories untouched."""
    role, subject = await authenticate(authorization)
    removed = await store.clear_memory(req.user_id, session_id=req.session_id)
    await store.log_access(
        action="memory_clear", role=role, subject_label=subject, query=None, tool_id=None, server_id=None,
        decision="ALLOW", reason=f"cleared {removed} memory entr(ies) for user '{req.user_id}'", latency_ms=0,
    )
    return {"user_id": req.user_id, "cleared": removed}


# ---------------------------------------------------------------------------
# Local dashboard login (see app/user_auth.py). Distinct from authenticate()
# above: that resolves an agent's bearer API key to an RBAC role; everything
# below resolves a person's username/password (local or LDAP) to a signed
# session cookie that require_dashboard_session (the app middleware near the
# top of this file) then requires on every other /v1 and /api route.
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """Best-effort real client IP for login-lockout accounting. nginx sets
    X-Forwarded-For (see ui/nginx.conf.template); request.client.host alone
    would only ever show the ui container's address, since this API is
    always reached through that reverse proxy in the bundled compose
    stack. Only the first hop is trusted here (this app has exactly one
    known reverse proxy in front of it) - not a general trusted-proxy chain
    parser."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _log_login_attempt(username: str, decision: str, reason: str) -> None:
    await store.log_access(
        action="dashboard_login", role=None, subject_label=username, query=None,
        tool_id=None, server_id=None, decision=decision, reason=reason, latency_ms=0,
    )


async def _finish_login(username: str, response: Response, request: Request):
    doc = await store.get_user(username)
    if not doc:
        raise HTTPException(status_code=500, detail="Account vanished mid-login")
    token = user_auth.create_session_token(username, doc.get("role", user_auth.DEFAULT_LOCAL_ROLE))
    response.set_cookie(
        key=user_auth.SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=AUTH_SESSION_TTL_HOURS * 3600,
        path="/",
    )
    doc["last_login_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await store.upsert_user(username, doc)
    return doc


@app.get("/v1/auth/bootstrap-status")
async def bootstrap_status():
    """Tells the login page which form to render: the one-time "set the
    admin password" form (a fresh install, or one where it was never
    completed) or the normal username/password login form."""
    _require_store()
    admin_doc = await store.get_user(DEFAULT_ADMIN_USERNAME)
    needs_setup = bool(admin_doc) and not admin_doc.get("password_hash")
    return {"needs_setup": needs_setup, "username": DEFAULT_ADMIN_USERNAME}


@app.post("/v1/auth/bootstrap")
async def bootstrap(req: BootstrapRequest, request: Request, response: Response):
    """Sets the default admin account's password the first time anyone
    reaches the login page. Refuses once a password already exists - after
    that, POST /v1/auth/login (or a password reset from Settings) is the
    only way in."""
    admin_doc = await store.get_user(DEFAULT_ADMIN_USERNAME)
    if not admin_doc:
        raise HTTPException(status_code=503, detail="Not ready yet - try again shortly.")
    if admin_doc.get("password_hash"):
        raise HTTPException(status_code=409, detail="The admin password has already been set. Use the login form.")
    policy_error = user_auth.password_policy_error(req.password, username=DEFAULT_ADMIN_USERNAME)
    if policy_error:
        raise HTTPException(status_code=400, detail=policy_error)

    admin_doc["password_hash"] = user_auth.hash_password(req.password)
    admin_doc["must_change_password"] = False
    admin_doc["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await store.upsert_user(DEFAULT_ADMIN_USERNAME, admin_doc)

    doc = await _finish_login(DEFAULT_ADMIN_USERNAME, response, request)
    return {"user": user_auth.public_user(DEFAULT_ADMIN_USERNAME, doc)}


@app.post("/v1/auth/login")
async def login(req: LoginRequest, request: Request, response: Response):
    _require_store()
    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(status_code=400, detail="Username and password are required.")

    ip = _client_ip(request)
    locked, retry_after = user_auth.login_lockout_status(username, ip)
    if locked:
        await _log_login_attempt(username, "DENY", f"locked out ({retry_after}s remaining)")
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed login attempts. Try again in about {max(1, retry_after // 60)} minute(s).",
            headers={"Retry-After": str(retry_after)},
        )

    user_doc = await store.get_user(username)

    if user_doc and user_doc.get("source", "local") == "local":
        if not user_doc.get("password_hash"):
            raise HTTPException(
                status_code=409,
                detail="This account has no password set yet - use the setup form instead.",
            )
        if not user_doc.get("active", True):
            await _log_login_attempt(username, "DENY", "account disabled")
            raise HTTPException(status_code=403, detail="This account has been disabled.")
        if not user_auth.verify_password(req.password, user_doc.get("password_hash")):
            user_auth.record_failed_login(username, ip)
            await _log_login_attempt(username, "DENY", "invalid password")
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        user_auth.record_successful_login(username, ip)
        await _log_login_attempt(username, "ALLOW", "local password login")
        doc = await _finish_login(username, response, request)
        return {"user": user_auth.public_user(username, doc)}

    # No local account by that name (or it's a previously-provisioned LDAP
    # shadow record) - try the directory if one is configured.
    if ldap_config.get("enabled"):
        success, detail, is_admin = await user_auth.ldap_authenticate(ldap_config, username, req.password)
        if not success:
            user_auth.record_failed_login(username, ip)
            await _log_login_attempt(username, "DENY", f"LDAP: {detail}")
            raise HTTPException(status_code=401, detail=detail)
        if user_doc and user_doc.get("active") is False:
            await _log_login_attempt(username, "DENY", "account disabled")
            raise HTTPException(status_code=403, detail="This account has been disabled.")

        role = "admin" if is_admin else user_auth.DEFAULT_LOCAL_ROLE
        shadow = user_auth.new_local_user_doc(role=role, password_hash=None, source="ldap")
        if user_doc:
            shadow["created_at"] = user_doc.get("created_at", shadow["created_at"])
            shadow["active"] = user_doc.get("active", True)
        await store.upsert_user(username, shadow)
        user_auth.record_successful_login(username, ip)
        await _log_login_attempt(username, "ALLOW", "LDAP login")
        doc = await _finish_login(username, response, request)
        return {"user": user_auth.public_user(username, doc)}

    user_auth.record_failed_login(username, ip)
    await _log_login_attempt(username, "DENY", "no such account")
    raise HTTPException(status_code=401, detail="Invalid username or password.")


@app.post("/v1/auth/logout")
async def logout(response: Response):
    response.delete_cookie(user_auth.SESSION_COOKIE_NAME, path="/")
    return {"logged_out": True}


@app.get("/v1/auth/me")
async def auth_me(request: Request):
    session = getattr(request.state, "user", None)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    doc = await store.get_user(session["username"])
    if not doc:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    return {"user": user_auth.public_user(session["username"], doc)}


@app.post("/v1/auth/change-password")
async def change_password(req: ChangePasswordRequest, request: Request):
    """Self-service password change - also clears must_change_password, so
    this is what an account created with a forced reset uses to satisfy it."""
    session = getattr(request.state, "user", None)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = session["username"]
    doc = await store.get_user(username)
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")
    if doc.get("source") != "local":
        raise HTTPException(status_code=400, detail="This account authenticates via LDAP - there is no local password to change.")
    if not user_auth.verify_password(req.current_password, doc.get("password_hash")):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    policy_error = user_auth.password_policy_error(req.new_password, username=username)
    if policy_error:
        raise HTTPException(status_code=400, detail=policy_error)

    doc["password_hash"] = user_auth.hash_password(req.new_password)
    doc["must_change_password"] = False
    doc["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await store.upsert_user(username, doc)
    return {"user": user_auth.public_user(username, doc)}


@app.get("/v1/auth/roles")
async def auth_roles():
    return {"roles": [{"id": rid, "description": desc} for rid, desc in user_auth.UI_ROLES.items()]}


# -- Settings -> Accounts & Roles (admin only) -------------------------------

@app.get("/v1/auth/users")
async def list_users(request: Request):
    require_admin(request)
    docs = await store.list_users()
    return {"users": [user_auth.public_user(d["username"], d) for d in docs]}


@app.post("/v1/auth/users")
async def create_user(req: CreateUserRequest, request: Request):
    require_admin(request)
    if req.role not in user_auth.UI_ROLES:
        raise HTTPException(status_code=400, detail=f"Unknown role '{req.role}'")
    if await store.get_user(req.username):
        raise HTTPException(status_code=409, detail=f"An account named '{req.username}' already exists.")
    policy_error = user_auth.password_policy_error(req.password, username=req.username)
    if policy_error:
        raise HTTPException(status_code=400, detail=policy_error)

    doc = user_auth.new_local_user_doc(
        role=req.role,
        password_hash=user_auth.hash_password(req.password),
        source="local",
        must_change_password=req.must_change_password,
    )
    await store.upsert_user(req.username, doc)
    return {"user": user_auth.public_user(req.username.lower(), doc)}


@app.put("/v1/auth/users/{username}")
async def update_user(username: str, req: UpdateUserRequest, request: Request):
    admin = require_admin(request)
    doc = await store.get_user(username)
    if not doc:
        raise HTTPException(status_code=404, detail="Account not found")

    is_default_admin = username.lower() == DEFAULT_ADMIN_USERNAME.lower()
    is_self = username.lower() == admin["username"].lower()

    if req.active is False and (is_default_admin or is_self):
        raise HTTPException(
            status_code=400,
            detail="You cannot disable the default admin account or your own account." if is_default_admin else "You cannot disable your own account.",
        )

    if req.role is not None:
        if req.role not in user_auth.UI_ROLES:
            raise HTTPException(status_code=400, detail=f"Unknown role '{req.role}'")
        if is_default_admin and req.role != "admin":
            raise HTTPException(status_code=400, detail="The default admin account must keep the admin role.")
        doc["role"] = req.role

    if req.active is not None:
        doc["active"] = req.active

    if req.password is not None:
        if doc.get("source") != "local":
            raise HTTPException(status_code=400, detail="This account authenticates via LDAP - it has no local password to set.")
        policy_error = user_auth.password_policy_error(req.password, username=username)
        if policy_error:
            raise HTTPException(status_code=400, detail=policy_error)
        doc["password_hash"] = user_auth.hash_password(req.password)

    if req.must_change_password is not None:
        doc["must_change_password"] = req.must_change_password

    doc["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await store.upsert_user(username, doc)
    return {"user": user_auth.public_user(username.lower(), doc)}


@app.delete("/v1/auth/users/{username}")
async def delete_user(username: str, request: Request):
    admin = require_admin(request)
    if username.lower() == DEFAULT_ADMIN_USERNAME.lower():
        raise HTTPException(status_code=400, detail="The default admin account cannot be deleted.")
    if username.lower() == admin["username"].lower():
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    deleted = await store.delete_user(username)
    if not deleted:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"deleted": True, "username": username.lower()}


# -- Settings -> LDAP Authentication (admin only) ----------------------------

@app.get("/v1/auth/ldap-config")
async def get_ldap_config(request: Request):
    require_admin(request)
    return {"config": user_auth.public_ldap_config(ldap_config)}


@app.put("/v1/auth/ldap-config")
async def put_ldap_config(req: LdapConfigRequest, request: Request):
    """Save the LDAP policy. bind_password is only present in the request
    body when the admin actually typed a new one in that field - omitted or
    blank leaves the encrypted secret already on file untouched, so this
    form never has to round-trip (or even know) the current secret."""
    require_admin(request)
    merged = {**ldap_config, **req.config}
    merged.pop("bind_password_encrypted", None)  # never accepted directly from the client
    normalized = user_auth.normalize_ldap_config(merged)
    if normalized["ca_certificate"]:
        try:
            user_auth.parse_ca_certificate(normalized["ca_certificate"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Corporate CA certificate: {exc}")
    if req.bind_password:
        normalized["bind_password_encrypted"] = user_auth.encrypt_secret(req.bind_password)
    else:
        normalized["bind_password_encrypted"] = ldap_config.get("bind_password_encrypted", "")
    await save_ldap_config(normalized)
    return {"config": user_auth.public_ldap_config(ldap_config)}


@app.post("/v1/auth/ldap-config/validate-ca")
async def validate_ca_certificate(req: LdapCaCertificateRequest, request: Request):
    """Parse (but don't save) a pasted/uploaded corporate CA certificate so
    the Settings page can show its subject/issuer/expiry immediately -
    before the admin commits to Save."""
    require_admin(request)
    try:
        info = user_auth.parse_ca_certificate(req.ca_certificate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"valid": True, "info": info}


@app.post("/v1/auth/ldap-config/test")
async def test_ldap_config(req: LdapTestRequest, request: Request):
    """Test the *saved* LDAP config (Save first, then Test) against one
    real set of credentials - service-account bind, user search, and user
    bind, exactly like a real login attempt would exercise."""
    require_admin(request)
    success, detail, is_admin = await user_auth.ldap_authenticate(ldap_config, req.username, req.password)
    return {"success": success, "detail": detail, "would_be_admin": is_admin}


# -- Settings -> Audit Log Forwarding (SIEM) (admin only) --------------------
# Six named destinations for the Audit Log's append-only Couchbase stream -
# see app/siem_forwarding.py for the per-vendor adapters and the encryption
# convention (same Fernet-at-rest scheme as the LDAP bind password above).

@app.get("/v1/siem/config")
async def get_siem_config(request: Request):
    require_admin(request)
    return {
        "config": siem_forwarding.public_config(siem_config),
        "status": siem_forwarding.status_snapshot(),
        "vendors": siem_forwarding.VENDOR_LABELS,
    }


@app.put("/v1/siem/config/{vendor}")
async def put_siem_config(vendor: str, req: SiemVendorConfigRequest, request: Request):
    """Save one destination's config. Non-secret fields overwrite outright;
    any secret field present in req.config (plaintext) is encrypted and
    replaces the stored secret, and one omitted/blank leaves the existing
    stored secret untouched - see siem_forwarding.apply_secrets."""
    require_admin(request)
    if vendor not in siem_forwarding.DEFAULT_DESTINATIONS:
        raise HTTPException(status_code=404, detail=f"Unknown SIEM destination '{vendor}'")
    merged_full = {**siem_config, vendor: {**siem_config.get(vendor, {}), **req.config}}
    normalized = siem_forwarding.normalize_config(merged_full)
    normalized[vendor] = siem_forwarding.apply_secrets(vendor, normalized[vendor], req.config)
    await save_siem_config(normalized)
    return {"config": siem_forwarding.public_config(siem_config)}


@app.post("/v1/siem/test/{vendor}")
async def test_siem_config(vendor: str, req: SiemTestRequest, request: Request):
    """Send one synthetic test event to this destination right now, using
    the saved config plus any unsaved edits from the settings form - so an
    admin can validate before committing Save."""
    require_admin(request)
    if vendor not in siem_forwarding.DEFAULT_DESTINATIONS:
        raise HTTPException(status_code=404, detail=f"Unknown SIEM destination '{vendor}'")
    candidate_full = {**siem_config, vendor: {**siem_config.get(vendor, {}), **(req.config or {})}}
    normalized = siem_forwarding.normalize_config(candidate_full)
    vendor_cfg = siem_forwarding.apply_secrets(vendor, normalized[vendor], req.config or {})
    result = await asyncio.to_thread(siem_forwarding.test_one, vendor, vendor_cfg)
    return result


# -- Settings -> HTTPS Certificate (admin only) ------------------------------
# Separate feature from the LDAP corporate CA above - see user_auth.py's
# section comment for the distinction. This installs the certificate nginx
# and uvicorn present to browsers, not one this appliance trusts outbound.

@app.get("/v1/auth/tls-cert")
async def get_tls_cert(request: Request):
    require_admin(request)
    return {
        "info": user_auth.current_server_certificate_info(),
        "can_revert": user_auth.can_revert_server_certificate(),
    }


@app.post("/v1/auth/tls-cert/validate")
async def validate_tls_cert(req: ServerCertificateRequest, request: Request):
    """Parse and cross-check a certificate/key pair without installing them,
    so the Settings page can preview subject/issuer/expiry/SANs and catch a
    mismatched key before the admin commits to Install."""
    require_admin(request)
    try:
        info = user_auth.validate_server_key_pair(req.cert_pem, req.key_pem)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"valid": True, "info": info}


@app.put("/v1/auth/tls-cert")
async def put_tls_cert(req: ServerCertificateRequest, request: Request):
    """Install a real certificate/key pair, replacing the self-signed
    fallback for both the dashboard and this API. Written straight to the
    files uvicorn/nginx serve from (see config.TLS_CERT_FILE/TLS_KEY_FILE) -
    neither picks up the change until operations-manager and ui are
    restarted, since TLS listeners don't hot-reload a swapped cert file."""
    require_admin(request)
    try:
        info = user_auth.install_server_certificate(req.cert_pem, req.key_pem)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"info": info, "can_revert": True, "restart_required": True}


@app.post("/v1/auth/tls-cert/revert")
async def revert_tls_cert(request: Request):
    """Restore the original baked-in self-signed certificate, undoing a
    previous Install. Also requires a restart to take effect."""
    require_admin(request)
    try:
        info = user_auth.revert_server_certificate()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"info": info, "can_revert": False, "restart_required": True}


# ---------------------------------------------------------------------------
# Agent run traces
# ---------------------------------------------------------------------------
# The audit log's counterpart: not "was this allowed?" but "what did this
# agent do, in what order, and where did it go wrong?". See app/tracing.py.
@app.get("/v1/traces")
@cached_dashboard_response
async def list_traces(limit: int = 100, role: str | None = None, status: str | None = None, window_hours: int = 24):
    since = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(window_hours, 24 * 30)))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    # Independent reads - run concurrently rather than stacking their
    # latencies, since each can individually approach QUERY_TIMEOUT_SECONDS
    # and sequential awaits here can add up past DASHBOARD_REQUEST_TIMEOUT_SECONDS.
    runs, aggregate = await asyncio.gather(
        store.list_runs(limit=min(limit, TRACE_LOOKBACK_RUNS), role=role, status=status),
        store.trace_aggregate_since(since),
    )

    totals = {
        "runs": sum(int(r.get("runs") or 0) for r in aggregate),
        "errors": sum(int(r.get("errors") or 0) for r in aggregate),
        "tokens": sum(int(r.get("tokens") or 0) for r in aggregate),
        "cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in aggregate), 4),
        "cache_hits": sum(int(r.get("cache_hits") or 0) for r in aggregate),
        "hijack_flags": sum(int(r.get("hijack_flags") or 0) for r in aggregate),
        "limit_blocks": sum(int(r.get("limit_blocks") or 0) for r in aggregate),
    }
    return {
        "runs": runs,
        "by_role": aggregate,
        "totals": totals,
        "window_hours": window_hours,
        "tracing_enabled": TRACING_ENABLED,
        "span_kinds": list(tracing.SPAN_KINDS),
    }


@app.get("/v1/traces/{trace_id}")
async def get_trace(trace_id: str):
    """One run and every span in it, oldest first - the timeline.

    The correlated context is assembled here rather than left to the
    client: the tools this run actually touched, with their current catalog
    state. That join - the run's behaviour against the catalog it ran
    against - is the thing that only works because both live in the same
    cluster, and it is the reason for putting traces in Couchbase rather
    than shipping them somewhere else.
    """
    run = await store.get_run(trace_id)
    spans = await store.list_spans(trace_id)
    if not run and not spans:
        raise HTTPException(status_code=404, detail="No such trace")

    tool_ids = sorted({
        (span.get("attributes") or {}).get("aom.tool_id")
        for span in spans
        if (span.get("attributes") or {}).get("aom.tool_id")
    })
    tools = []
    for tool_id in tool_ids:
        tool = await store.get_tool(tool_id)
        if tool:
            tools.append({
                "tool_id": tool.get("tool_id"),
                "server_id": tool.get("server_id"),
                "risk_level": tool.get("risk_level"),
                "trust_status": tool.get("trust_status"),
                "drift_status": tool.get("drift_status"),
                "allowed_roles": tool.get("allowed_roles"),
                "definition_version": tool.get("definition_version"),
            })

    return {"run": run, "spans": spans, "tools": tools}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
@app.get("/v1/evals")
async def list_evals():
    datasets = await store.list_datasets()
    runs = await store.list_eval_runs(limit=50)
    latest_by_dataset: dict[str, dict] = {}
    for run in runs:
        latest_by_dataset.setdefault(run.get("dataset_id"), run)
    for dataset in datasets:
        dataset["latest_run"] = latest_by_dataset.get(dataset.get("dataset_id"))
    return {
        "datasets": datasets,
        "runs": runs,
        "case_kinds": list(evals.CASE_KINDS),
        "run_on_catalog_change": EVAL_RUN_ON_CATALOG_CHANGE,
        "last_gate_at": last_eval_gate_at,
    }


@app.put("/v1/evals/datasets")
async def put_eval_dataset(req: EvalDatasetRequest, request: Request):
    require_admin(request)
    dataset = evals.normalize_dataset(req.dataset)
    if not dataset["cases"]:
        raise HTTPException(status_code=400, detail="A dataset needs at least one case")
    existing = await store.get_dataset(dataset["dataset_id"])
    if existing:
        dataset["created_at"] = existing.get("created_at") or dataset["created_at"]
    await store.upsert_dataset(dataset)
    return {"dataset": dataset}


@app.delete("/v1/evals/datasets/{dataset_id}")
async def delete_eval_dataset(dataset_id: str, request: Request):
    require_admin(request)
    if not await store.delete_dataset(dataset_id):
        raise HTTPException(status_code=404, detail="No such dataset")
    return {"deleted": True, "dataset_id": dataset_id}


@app.post("/v1/evals/run")
async def run_evals(req: EvalRunRequest, request: Request):
    """Run one dataset, or every enabled dataset, right now. Awaited rather
    than backgrounded, unlike the catalog-change gate: someone clicked a
    button and is waiting for the answer."""
    require_admin(request)
    if not store.connected:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")

    if req.dataset_id:
        dataset = await store.get_dataset(req.dataset_id)
        if not dataset:
            raise HTTPException(status_code=404, detail="No such dataset")
        return {"runs": [await execute_eval_dataset(dataset, trigger="manual")]}

    return {"runs": await run_eval_gate(trigger="manual")}


@app.get("/v1/evals/runs/{run_id:path}")
async def get_eval_run(run_id: str):
    run = await store.get_eval_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="No such run")
    return {"run": run}


# ---------------------------------------------------------------------------
# Limits and budgets
# ---------------------------------------------------------------------------
@app.get("/v1/governance/config")
async def get_governance_config(request: Request):
    """The policy, plus what every known caller has actually consumed in the
    current window. Usage next to the ceiling is the whole point: nobody can
    set a sensible limit without knowing what normal traffic looks like,
    which is also why `enforce` defaults off."""
    require_admin(request)

    usage = []
    for api_key, seeded_role in SEED_API_KEYS.items():
        subject = f"...{api_key[-4:]}"
        entry = {"subject": subject, "role": seeded_role, "limits": []}
        for family in ("requests", "tool_calls", "tokens", "spend"):
            if not governance.limit_for(governance_config, family):
                continue
            raw = await store.read_counter(governance.counter_key(family, subject))
            used = governance.from_spend_units(raw) if family == "spend" else raw
            entry["limits"].append(governance.evaluate(governance_config, family, used, seeded_role))
        usage.append(entry)

    return {
        "config": governance.public_config(governance_config),
        "defaults": governance.DEFAULT_GOVERNANCE_CONFIG,
        "window_seconds": governance.WINDOW_SECONDS,
        "roles": list(ROLES),
        "usage": usage,
    }


@app.put("/v1/governance/config")
async def put_governance_config(req: GovernanceConfigRequest, request: Request):
    require_admin(request)
    await save_governance_config(req.config)
    return {"config": governance.public_config(governance_config)}


# ---------------------------------------------------------------------------
# Human approval tier
# ---------------------------------------------------------------------------
class ApprovalDecisionRequest(BaseModel):
    approved: bool
    note: str | None = None


class ApprovalConfigRequest(BaseModel):
    config: dict


class GuardrailsConfigRequest(BaseModel):
    config: dict


@app.get("/v1/agent/approvals/{approval_id}")
async def get_approval_status(approval_id: str, authorization: str | None = Header(default=None)):
    """What a waiting agent polls. Authenticated with the agent's own API
    key, and it may only see approvals raised for itself - an approval ID is
    otherwise a guessable handle on somebody else's pending work."""
    role, subject = await authenticate(authorization)
    approval = await store.get_approval(approval_id)
    if not approval or approval.get("subject") != subject or approval.get("role") != role:
        # Deliberately the same answer for "does not exist" and "not yours",
        # so the endpoint cannot be used to discover that an approval ID is
        # real.
        raise HTTPException(status_code=404, detail="No such approval")
    return {"approval": approvals.public_approval(approval)}


@app.get("/v1/approvals")
async def list_approvals_route(status: str | None = None, limit: int = 100):
    """The reviewer's queue. Dashboard-session protected, unlike the polling
    route above - this one returns the arguments a reviewer has to read to
    make a decision."""
    pending = await store.list_approvals(status=status, limit=min(limit, 200))
    return {
        "approvals": pending,
        "config": approvals_config,
        "statuses": list(approvals.STATUSES),
        "pending_count": await store.count_pending_approvals(),
    }


@app.post("/v1/approvals/{approval_id}/decide")
async def decide_approval(approval_id: str, req: ApprovalDecisionRequest, request: Request):
    user = require_admin(request)
    approval = await store.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="No such approval - it may have expired")
    if approval.get("status") != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"This approval is already '{approval.get('status')}' and cannot be decided again",
        )

    decided = approvals.decide(
        approval, approved=req.approved, username=user.get("username"), note=req.note, cfg=approvals_config
    )
    # An approved decision has to outlive the original pending TTL long
    # enough for the agent to redeem it, so the grace window is added to the
    # document's remaining life rather than inherited from it.
    ttl = int(approvals_config["grace_seconds"]) if req.approved else int(approvals_config["ttl_seconds"])
    await store.upsert_approval(decided, ttl_seconds=ttl)

    await store.log_access(
        action="approval", role=approval.get("role"), subject_label=approval.get("subject"), query=None,
        tool_id=approval.get("tool_id"), server_id=approval.get("server_id"),
        decision="ALLOW" if req.approved else "DENY",
        reason=f"{'approved' if req.approved else 'denied'} by {user.get('username')}"
               + (f": {req.note}" if req.note else ""),
        latency_ms=0,
    )
    return {"approval": decided}


@app.get("/v1/approvals-config")
async def get_approvals_config(request: Request):
    require_admin(request)
    return {
        "config": approvals_config,
        "defaults": approvals.DEFAULT_APPROVAL_CONFIG,
        "risk_levels": list(approvals.RISK_LEVELS),
        "roles": list(ROLES),
    }


@app.put("/v1/approvals-config")
async def put_approvals_config(req: ApprovalConfigRequest, request: Request):
    require_admin(request)
    await save_approvals_config(req.config)
    return {"config": approvals_config}


# ---------------------------------------------------------------------------
# Guardrails and PII
# ---------------------------------------------------------------------------
@app.get("/v1/guardrails/config")
async def get_guardrails_config(request: Request):
    require_admin(request)
    return {
        "config": guardrails_config,
        "defaults": guardrails.DEFAULT_GUARDRAILS_CONFIG,
        "detectors": [
            {"id": d.id, "label": d.label, "severity": d.severity} for d in guardrails.DETECTORS
        ],
        "block_levels": list(guardrails.BLOCK_LEVELS),
    }


@app.put("/v1/guardrails/config")
async def put_guardrails_config(req: GuardrailsConfigRequest, request: Request):
    require_admin(request)
    await save_guardrails_config(req.config)
    return {"config": guardrails_config}


class GuardrailsTestRequest(BaseModel):
    text: str


@app.post("/v1/guardrails/test")
async def test_guardrails(req: GuardrailsTestRequest, request: Request):
    """Run the current policy against a sample. The only honest way to set a
    detector list is to see what it does to text that looks like yours -
    a policy nobody has tested against real-shaped input is a guess."""
    require_admin(request)
    result = guardrails.inspect_input(req.text or "", guardrails_config)
    return {
        "pii": result["pii"],
        "injection": result["injection"],
        "blocked": result["blocked"],
        "redacted": result["redacted"],
        "summary": guardrails.summarize(result),
    }


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------
class KnowledgeIngestRequest(BaseModel):
    title: str
    content: str
    # "text" for anything the browser could read as text; "base64" for a
    # binary format (a PDF) that has to survive a JSON round-trip intact.
    encoding: str = "text"
    filename: str | None = None
    source: str | None = None
    allowed_roles: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    # Which knowledge set (and so which embedding model and index) this
    # document goes into. See app/knowledge_sets.py.
    set_id: str = "default"


class KnowledgeSearchRequest(BaseModel):
    query: str
    top_k: int = 5
    document_id: str | None = None
    set_id: str = "default"


class KnowledgeSetRequest(BaseModel):
    name: str
    model_id: str
    set_id: str | None = None
    description: str = ""


@app.get("/v1/knowledge")
async def list_knowledge(request: Request):
    documents = await store.list_knowledge_documents()
    for d in documents:
        d.setdefault("set_id", knowledge_sets.DEFAULT_SET_ID)
    return {
        "documents": documents,
        "sets": public_knowledge_sets(documents),
        "default_set_id": knowledge_sets.DEFAULT_SET_ID,
        "chunk_count": await store.count_knowledge_chunks(),
        "roles": list(ROLES),
        "chunk_chars": KNOWLEDGE_CHUNK_CHARS,
        "chunk_overlap": KNOWLEDGE_CHUNK_OVERLAP,
        "max_upload_mb": KNOWLEDGE_MAX_UPLOAD_MB,
        "supported_extensions": list(knowledge.SUPPORTED_EXTENSIONS),
    }


@app.post("/v1/knowledge")
async def ingest_knowledge(req: KnowledgeIngestRequest, request: Request):
    """Ingest one document: extract, chunk, embed, store. Dashboard-session
    protected - who may *read* a document is an RBAC decision made here, so
    who may *add* one has to be an operator rather than any agent holding an
    API key."""
    require_admin(request)
    if not store.connected or embeddings is None:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")

    unknown_roles = [r for r in req.allowed_roles if r not in ROLES]
    if unknown_roles:
        raise HTTPException(status_code=400, detail=f"Unknown role(s): {unknown_roles}")
    if not req.allowed_roles:
        raise HTTPException(
            status_code=400,
            detail="Pick at least one role that may retrieve this document - a document no role can read is "
                   "indexed, embedded and permanently invisible.",
        )
    kset = get_knowledge_set(req.set_id)

    if len(req.content.encode("utf-8")) > KNOWLEDGE_MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Document is larger than the {KNOWLEDGE_MAX_UPLOAD_MB}MB limit")

    if req.encoding == "base64":
        try:
            raw: bytes | str = base64.b64decode(req.content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="content was not valid base64") from exc
    elif req.encoding == "text":
        raw = req.content
    else:
        raise HTTPException(status_code=400, detail="encoding must be 'text' or 'base64'")

    try:
        # PDF parsing is CPU-bound and can take seconds on a large file -
        # off the event loop, like embedding.
        text, text_format = await asyncio.to_thread(knowledge.extract_text, raw, req.filename or req.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    text = knowledge.normalize_text(text)
    if not text:
        raise HTTPException(status_code=400, detail="Nothing readable in that document once it was parsed")

    chunks = knowledge.chunk_text(text, KNOWLEDGE_CHUNK_CHARS, KNOWLEDGE_CHUNK_OVERLAP)
    if not chunks:
        raise HTTPException(status_code=400, detail="That document produced no chunks")

    document_id = knowledge.new_document_id(req.title)
    start = time.time()
    try:
        try:
            vectors = await embedder_for(kset).embed_documents(
                [knowledge.build_embedding_text(req.title, chunk) for chunk in chunks]
            )
        except (RuntimeError, OSError) as exc:
            # A hosted provider refusing, or a local model that couldn't be
            # downloaded - the operator's problem to fix, not a server bug.
            raise HTTPException(
                status_code=502,
                detail=f"Embedding with {kset['model_id']} failed: {exc}",
            ) from exc
        for index, (chunk, embedding) in enumerate(zip(chunks, vectors)):
            chunk_doc = knowledge.build_chunk_doc(
                document_id=document_id, document_title=req.title, index=index,
                content=chunk, embedding=embedding, allowed_roles=req.allowed_roles,
            )
            if kset["set_id"] != knowledge_sets.DEFAULT_SET_ID:
                chunk_doc[kset["vector_field"]] = chunk_doc.pop("embedding")
                chunk_doc["set_id"] = kset["set_id"]
            await store.upsert_knowledge_chunk(chunk_doc)
    except BaseException:
        # A failure or a request timeout part-way through would otherwise
        # leave chunks that retrieval still returns but that belong to no
        # listed document - so nobody could see or delete them.
        spawn(store.delete_knowledge_document(document_id))
        raise

    doc = knowledge.build_document_doc(
        document_id=document_id, title=req.title, source=req.source or req.filename or "upload",
        text_format=text_format, allowed_roles=req.allowed_roles, chunk_count=len(chunks),
        char_count=len(text), fingerprint=knowledge.content_fingerprint(text),
        uploaded_by=(getattr(request.state, "user", {}) or {}).get("username"), metadata=req.metadata,
    )
    doc["set_id"] = kset["set_id"]
    doc["embedding_model"] = kset["model_id"]
    await store.upsert_knowledge_document(doc)
    bump_knowledge_generation()

    logger.info(
        "Ingested knowledge document '%s' (%d chunk(s), %d chars) in %dms",
        document_id, len(chunks), len(text), int((time.time() - start) * 1000),
    )
    return {"document": doc}


@app.delete("/v1/knowledge/{document_id}")
async def delete_knowledge(document_id: str, request: Request):
    require_admin(request)
    removed = await store.delete_knowledge_document(document_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No such document")
    bump_knowledge_generation()
    return {"deleted": True, "document_id": document_id, "documents_removed": removed}


@app.post("/v1/agent/knowledge/search")
async def search_knowledge_route(
    req: KnowledgeSearchRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Retrieval, governed exactly like tool discovery: one Couchbase Search
    request combining vector kNN with an RBAC pre-filter, so a chunk outside
    the caller's role is never a candidate however well it matches."""
    role, subject = await authenticate(authorization, request)
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query is required")
    if not store.connected or embeddings is None:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")

    kset = get_knowledge_set(req.set_id)
    ctx = trace_context(request)
    start = time.time()
    try:
        vector = await embedder_for(kset).embed_query(req.query)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"Embedding with {kset['model_id']} failed: {exc}") from exc
    results = await store.search_knowledge(
        role, vector, top_k=max(1, min(req.top_k, 25)), document_id=req.document_id,
        index_name=kset["index_name"], vector_field=kset["vector_field"],
    )
    # Each set has its own index, so this only ever drops something if an
    # index were misconfigured - the same belt-and-braces re-check the role
    # gets above.
    results = [r for r in results if r.get("set_id", knowledge_sets.DEFAULT_SET_ID) == kset["set_id"]]
    latency_ms = int((time.time() - start) * 1000)

    await store.log_access(
        action="knowledge_search", role=role, subject_label=subject,
        query=redact_text_for_storage(req.query), tool_id=None, server_id=None, decision="ALLOW",
        reason=f"{len(results)} chunk(s) matched RBAC+vector pre-filter for role '{role}'",
        latency_ms=latency_ms,
    )
    await record_span(
        ctx, kind="internal", name="knowledge retrieval", started=start, role=role, subject=subject,
        attributes={
            "aom.operation": "knowledge.search",
            "aom.knowledge_set": kset["set_id"],
            "aom.embedding_model": kset["model_id"],
            "aom.query": redact_text_for_storage(req.query),
            "aom.result_count": len(results),
            "aom.document_ids": sorted({r.get("document_id") for r in results if r.get("document_id")}),
        },
    )
    return {
        "role": role,
        "set_id": kset["set_id"],
        "results": results,
        "latency_ms": latency_ms,
        "trace": {"trace_id": ctx["trace_id"]},
    }


# ---------------------------------------------------------------------------
# Agent memory management
# ---------------------------------------------------------------------------
# The operator's view of what agents remember. Deliberately *not* under
# /v1/memory: that prefix is the agent-facing surface (no dashboard session,
# rate limited), and these routes are the opposite of both.
class MemoryEditRequest(BaseModel):
    content: str | None = None
    memory_type: str | None = None
    metadata: dict | None = None


class MemoryForgetRequest(BaseModel):
    user_id: str


class MemoryConfigRequest(BaseModel):
    config: dict


class MemoryConsolidateRequest(BaseModel):
    user_id: str | None = None


class MemoryAdminSearchRequest(BaseModel):
    user_id: str
    query: str
    top_k: int = 10
    memory_type: str | None = None


@app.get("/v1/agent-memory/users")
async def memory_users(request: Request):
    """Who has memories, and how much. The page opens here rather than on a
    list of entries, because "which user" is the only question an operator
    can answer before seeing anything."""
    return {
        "users": await store.list_memory_users(),
        "stats": await store.memory_stats(),
        "config": memory_config,
        "last_consolidation_at": last_consolidation_at,
        "last_report": last_consolidation_report,
        "memory_types": list(agent_memory.MEMORY_TYPES),
    }


@app.get("/v1/agent-memory/entries")
async def memory_entries(
    request: Request,
    user_id: str,
    session_id: str | None = None,
    memory_type: str | None = None,
    status: str | None = None,
    limit: int = 200,
):
    """One user's memories. `status` unset means everything, superseded
    included - seeing what consolidation replaced, and what it replaced it
    with, is the reason superseding beats deleting."""
    entries = await store.list_memory(
        user_id, session_id=session_id, memory_type=memory_type,
        limit=min(limit, 500), status=status,
    )
    for entry in entries:
        entry["importance_band"] = memory_consolidation.importance_band(float(entry.get("importance") or 0.0))
    return {"user_id": user_id, "entries": entries}


@app.post("/v1/agent-memory/search")
async def memory_admin_search(req: MemoryAdminSearchRequest, request: Request):
    """Semantic search over one user's memory, as an operator rather than
    as an agent - same vector recall the agent gets, without needing that
    agent's API key."""
    if not store.connected or embeddings is None:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")
    vector = await embeddings.embed_async(req.query)
    results = await store.search_memory(
        req.user_id, vector, memory_type=req.memory_type, top_k=max(1, min(req.top_k, 50))
    )
    return {"user_id": req.user_id, "entries": results}


@app.put("/v1/agent-memory/entries/{memory_id:path}")
async def edit_memory(memory_id: str, req: MemoryEditRequest, request: Request):
    """Correct what an agent believes.

    Editing the content re-embeds it, because an entry whose text and
    vector disagree is worse than either - it would be retrieved for the
    old meaning and then assert the new one.
    """
    require_admin(request)
    entry = await store.get_memory(memory_id)
    if not entry:
        raise HTTPException(status_code=404, detail="No such memory entry")

    if req.content is not None:
        content = req.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="content cannot be empty - delete the entry instead")
        if guardrails_config.get("enabled") and guardrails_config.get("redact_memory"):
            content = redact_text_for_storage(content)
        entry["content"] = content[:agent_memory.MAX_CONTENT_CHARS]
        if embeddings is not None:
            entry["embedding"] = await embeddings.embed_async(
                agent_memory.build_embedding_text(entry["content"], entry.get("metadata"))
            )
    if req.memory_type is not None:
        entry["memory_type"] = agent_memory.normalize_memory_type(req.memory_type)
    if req.metadata is not None:
        entry["metadata"] = agent_memory.sanitize_metadata(
            redact_for_storage(req.metadata) if guardrails_config.get("redact_memory") else req.metadata
        )

    entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["edited_by"] = (getattr(request.state, "user", {}) or {}).get("username")
    await store.upsert_memory(memory_id, _memory_write_doc(entry))

    await store.log_access(
        action="memory_edit", role=None, subject_label=entry.get("subject"), query=None,
        tool_id=None, server_id=None, decision="ALLOW",
        reason=f"memory '{memory_id}' edited by {entry['edited_by']}", latency_ms=0,
    )
    return {"memory_id": memory_id, "entry": _memory_write_doc({k: v for k, v in entry.items() if k != "embedding"})}


@app.delete("/v1/agent-memory/entries/{memory_id:path}")
async def delete_memory_admin(memory_id: str, request: Request):
    require_admin(request)
    if not await store.delete_memory(memory_id):
        raise HTTPException(status_code=404, detail="No such memory entry")
    await store.log_access(
        action="memory_delete", role=None, subject_label=None, query=None, tool_id=None, server_id=None,
        decision="ALLOW", reason=f"memory '{memory_id}' deleted from the Agent Memory page", latency_ms=0,
    )
    return {"deleted": True, "memory_id": memory_id}


@app.post("/v1/agent-memory/forget")
async def forget_user_route(req: MemoryForgetRequest, request: Request):
    """Erase everything remembered about one person, superseded entries
    included. This is the route a data-subject erasure request runs
    through, so it is audited by design and cannot be undone."""
    user = require_admin(request)
    if not req.user_id or not req.user_id.strip():
        raise HTTPException(status_code=400, detail="user_id is required")
    removed = await store.forget_user(req.user_id)
    await store.log_access(
        action="memory_forget", role=None, subject_label=req.user_id, query=None,
        tool_id=None, server_id=None, decision="ALLOW",
        reason=f"{removed} memor(ies) for '{req.user_id}' erased by {user.get('username')}", latency_ms=0,
    )
    logger.info("Erased %d memor(ies) for user '%s' at operator request", removed, req.user_id)
    return {"forgotten": True, "user_id": req.user_id, "entries_removed": removed}


@app.get("/v1/agent-memory/config")
async def get_memory_config(request: Request):
    require_admin(request)
    return {
        "config": memory_config,
        "defaults": memory_consolidation.DEFAULT_MEMORY_CONFIG,
        "memory_types": list(agent_memory.MEMORY_TYPES),
        "importance_weights": memory_consolidation.IMPORTANCE_WEIGHTS,
        "last_consolidation_at": last_consolidation_at,
        "last_report": last_consolidation_report,
    }


@app.put("/v1/agent-memory/config")
async def put_memory_config(req: MemoryConfigRequest, request: Request):
    require_admin(request)
    await save_memory_config(req.config)
    return {"config": memory_config}


@app.post("/v1/agent-memory/consolidate")
async def consolidate_now(req: MemoryConsolidateRequest, request: Request):
    """Run a pass immediately. Awaited rather than backgrounded, unlike the
    timer: someone pressed a button and is waiting to see what it did."""
    require_admin(request)
    if not store.connected:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")
    report = await run_memory_consolidation(trigger="manual", user_id=req.user_id)
    return {"report": report, "last_consolidation_at": last_consolidation_at}


# ---------------------------------------------------------------------------
# Agent identities
# ---------------------------------------------------------------------------
# The inbound counterpart to app/server_auth.py. That module answers "what
# does a downstream server learn about who this call is for"; this one
# answers "who is calling us, and may they still".
class CreateAgentRequest(BaseModel):
    name: str
    role: str
    owner: str = ""
    description: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    # ISO-8601, or omitted for a credential that does not expire.
    expires_at: str | None = None


class UpdateAgentRequest(BaseModel):
    name: str | None = None
    owner: str | None = None
    description: str | None = None
    role: str | None = None
    allowed_tools: list[str] | None = None
    expires_at: str | None = None
    status: str | None = None


class RotateAgentRequest(BaseModel):
    # False leaves the previous key working for the configured grace
    # window; True kills it now, which is what a suspected leak needs.
    revoke_immediately: bool = False


class AgentOidcConfigRequest(BaseModel):
    config: dict


async def _issue_key(agent: dict, label: str = "") -> str:
    """Mint a key, write its document, and record its hash on the agent.
    The raw key is returned to the caller once and never stored."""
    api_key = agent_identity.generate_key()
    key_doc = agent_identity.build_key_doc(agent=agent, api_key=api_key, label=label)
    await store.upsert_key_doc(api_key, key_doc)
    agent.setdefault("keys", []).append({
        "key_hash": agent_identity.hash_key(api_key),
        "key_prefix": key_doc["key_prefix"],
        "status": "active",
        "created_at": key_doc["created_at"],
        "expires_at": key_doc["expires_at"],
        "label": label,
    })
    return api_key


@app.get("/v1/agents")
async def list_agents_route(request: Request):
    require_admin(request)
    agents = [agent_identity.public_agent(a) for a in await store.list_agents()]
    return {
        "agents": agents,
        "roles": [{"id": r, "description": d} for r, d in ROLES.items()],
        "rotation_grace_seconds": AGENT_KEY_ROTATION_GRACE_SECONDS,
        "oidc": {
            "enabled": agent_oidc_config["enabled"],
            "issuer": agent_oidc_config["issuer"],
            "problems": agent_oidc.config_problems(agent_oidc_config),
        },
    }


@app.post("/v1/agents")
async def create_agent(req: CreateAgentRequest, request: Request):
    """Issue an agent and its first key.

    The key is returned exactly once, in this response. It is stored only
    as a SHA-256, so it cannot be shown again - which is the property that
    makes rotation and revocation the answer to a lost key rather than
    "look it up".
    """
    user = require_admin(request)
    if req.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Unknown role '{req.role}'")

    role_tools = {
        t.get("tool_id") for t in await store.list_tools()
        if req.role in (t.get("allowed_roles") or [])
    }
    scope = agent_identity.normalize_scope(req.allowed_tools, role_tools)
    if req.allowed_tools and not scope:
        raise HTTPException(
            status_code=400,
            detail=(
                f"None of those tools are available to role '{req.role}', so that scope would "
                f"leave this agent with nothing it can call."
            ),
        )

    agent = agent_identity.build_agent(
        name=req.name, role=req.role, owner=req.owner, description=req.description,
        allowed_tools=scope, expires_at=req.expires_at, created_by=user.get("username"),
    )
    api_key = await _issue_key(agent, label="initial")
    await store.upsert_agent(agent)

    await store.log_access(
        action="agent_issue", role=req.role, subject_label=agent_identity.key_prefix(api_key), query=None,
        tool_id=None, server_id=None, decision="ALLOW",
        reason=f"agent '{agent['name']}' issued by {user.get('username')}", latency_ms=0,
    )
    return {
        "agent": agent_identity.public_agent(agent),
        "api_key": api_key,
        "notice": "This key is shown once and stored only as a hash. Save it now; it cannot be retrieved.",
    }


@app.put("/v1/agents/{agent_id}")
async def update_agent(agent_id: str, req: UpdateAgentRequest, request: Request):
    user = require_admin(request)
    agent = await store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="No such agent")

    if req.role is not None:
        if req.role not in ROLES:
            raise HTTPException(status_code=400, detail=f"Unknown role '{req.role}'")
        agent["role"] = req.role
    if req.allowed_tools is not None:
        role_tools = {
            t.get("tool_id") for t in await store.list_tools()
            if agent["role"] in (t.get("allowed_roles") or [])
        }
        agent["allowed_tools"] = agent_identity.normalize_scope(req.allowed_tools, role_tools)
    for field in ("name", "owner", "description"):
        value = getattr(req, field)
        if value is not None:
            agent[field] = value[:400]
    if req.expires_at is not None:
        agent["expires_at"] = req.expires_at or None
    if req.status is not None:
        if req.status not in agent_identity.AGENT_STATUSES:
            raise HTTPException(status_code=400, detail="status must be 'active' or 'revoked'")
        agent["status"] = req.status

    # Every change has to reach the key documents, because those are what
    # authentication actually reads.
    await sync_agent_keys(agent)
    await store.upsert_agent(agent)

    await store.log_access(
        action="agent_update", role=agent["role"], subject_label=agent_id, query=None,
        tool_id=None, server_id=None, decision="ALLOW",
        reason=f"agent '{agent['name']}' updated by {user.get('username')}", latency_ms=0,
    )
    return {"agent": agent_identity.public_agent(agent)}


@app.post("/v1/agents/{agent_id}/rotate")
async def rotate_agent_key(agent_id: str, req: RotateAgentRequest, request: Request):
    """Issue a replacement key.

    By default the previous key keeps working for the configured grace
    window and then expires on its own - a Couchbase document TTL, so there
    is no sweeper and no state that can outlive its window. Rotation that
    caused an outage would be rotation nobody performed, which is how keys
    end up years old.
    """
    user = require_admin(request)
    agent = await store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="No such agent")

    grace = 0 if req.revoke_immediately else AGENT_KEY_ROTATION_GRACE_SECONDS
    expiry = agent_identity.rotation_expiry(grace) if grace else None

    for key in agent.get("keys") or []:
        if key.get("status") != "active":
            continue
        key["status"] = "revoked" if req.revoke_immediately else "rotated"
        key["expires_at"] = expiry
        doc = await store.get_key_doc_by_hash(key["key_hash"])
        if doc:
            doc["status"] = key["status"]
            doc["expires_at"] = expiry
            # The document outlives its grace window by a minute so a
            # request landing on the boundary reads an expired key and is
            # told so, rather than reading nothing and being told the key
            # was never valid.
            await store.upsert_key_doc_by_hash(
                key["key_hash"], doc,
                ttl_seconds=(grace + 60) if grace else agent_identity.REVOCATION_TOMBSTONE_SECONDS,
            )

    api_key = await _issue_key(agent, label="rotated")
    await store.upsert_agent(agent)

    await store.log_access(
        action="agent_rotate", role=agent["role"], subject_label=agent_id, query=None,
        tool_id=None, server_id=None, decision="ALLOW",
        reason=(
            f"key rotated by {user.get('username')}; previous key "
            + ("revoked immediately" if req.revoke_immediately else f"valid for a further {grace}s")
        ),
        latency_ms=0,
    )
    return {
        "agent": agent_identity.public_agent(agent),
        "api_key": api_key,
        "previous_key_valid_until": expiry,
        "notice": "This key is shown once and stored only as a hash. Save it now; it cannot be retrieved.",
    }


@app.post("/v1/agents/{agent_id}/revoke")
async def revoke_agent(agent_id: str, request: Request):
    """Turn an agent off now. Every key it holds stops working on the next
    request - no restart, no config edit, which is what was missing."""
    user = require_admin(request)
    agent = await store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="No such agent")

    agent["status"] = "revoked"
    for key in agent.get("keys") or []:
        key["status"] = "revoked"
    # Written through as revoked rather than deleted, so a caller still
    # holding the key is told it was revoked instead of that it never
    # existed. The documents carry a TTL and clean themselves up.
    await sync_agent_keys(agent, ttl_seconds=agent_identity.REVOCATION_TOMBSTONE_SECONDS)
    await store.upsert_agent(agent)

    await store.log_access(
        action="agent_revoke", role=agent.get("role"), subject_label=agent_id, query=None,
        tool_id=None, server_id=None, decision="DENY",
        reason=f"agent '{agent['name']}' revoked by {user.get('username')}", latency_ms=0,
    )
    logger.warning("Agent '%s' (%s) revoked by %s", agent["name"], agent_id, user.get("username"))
    return {"agent": agent_identity.public_agent(agent)}


@app.delete("/v1/agents/{agent_id}")
async def delete_agent_route(agent_id: str, request: Request):
    user = require_admin(request)
    agent = await store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="No such agent")
    for key in agent.get("keys") or []:
        await store.delete_key_doc_by_hash(key["key_hash"])
    await store.delete_agent(agent_id)
    await store.log_access(
        action="agent_delete", role=agent.get("role"), subject_label=agent_id, query=None,
        tool_id=None, server_id=None, decision="ALLOW",
        reason=f"agent '{agent.get('name')}' deleted by {user.get('username')}", latency_ms=0,
    )
    return {"deleted": True, "agent_id": agent_id}


@app.get("/v1/agents-oidc/config")
async def get_agent_oidc_config(request: Request):
    require_admin(request)
    return {
        "config": agent_oidc_config,
        "defaults": agent_oidc.DEFAULT_OIDC_CONFIG,
        "problems": agent_oidc.config_problems(agent_oidc_config),
        "roles": list(ROLES),
        "algorithms": list(agent_oidc.DEFAULT_ALGORITHMS),
    }


@app.put("/v1/agents-oidc/config")
async def put_agent_oidc_config(req: AgentOidcConfigRequest, request: Request):
    require_admin(request)
    await save_agent_oidc_config(req.config)
    return {"config": agent_oidc_config, "problems": agent_oidc.config_problems(agent_oidc_config)}


class AgentOidcTestRequest(BaseModel):
    token: str


@app.post("/v1/agents-oidc/test")
async def test_agent_oidc(req: AgentOidcTestRequest, request: Request):
    """Validate a real token against the saved configuration.

    An IdP integration that has never been tested against a token the IdP
    actually mints is a guess, and the failure mode is every agent getting
    401 at the worst possible moment.
    """
    require_admin(request)
    identity, reason = await asyncio.to_thread(
        agent_oidc.validate, req.token.strip(), agent_oidc_config, set(ROLES)
    )
    return {
        "valid": bool(identity),
        "reason": reason,
        "role": (identity or {}).get("role"),
        "subject": (identity or {}).get("claims_subject"),
    }


# ---------------------------------------------------------------------------
# RAG Applications (see app/rag_apps.py)
# ---------------------------------------------------------------------------
class RagAppRequest(BaseModel):
    # Raw dict, validated by rag_apps.normalize_app - same "the form is a
    # convenience, this is the boundary" convention as the other policies.
    app: dict
    # Registration only: issue the app its own Agent Identity (role = the
    # first allowed role) and return its key once.
    issue_key: bool = True


class RagQueryRequest(BaseModel):
    question: str
    # Narrow (never widen) the app's own top_k for one call.
    top_k: int | None = None
    # Skip both caches for one call - e.g. to compare against a fresh answer.
    bypass_cache: bool = False


async def load_rag_apps():
    global rag_apps_registry
    stored = await store.get_setting(RAG_APPS_SETTINGS_DOC)
    apps = {}
    for app_id, raw in ((stored or {}).get("apps") or {}).items():
        try:
            apps[app_id] = rag_apps.normalize_app(raw, set(ROLES), existing=raw)
        except ValueError as exc:
            logger.warning("Skipping invalid stored RAG application '%s': %s", app_id, exc)
    rag_apps_registry = apps
    logger.info("RAG applications loaded (%d registered)", len(apps))


async def save_rag_apps():
    await store.upsert_setting(
        RAG_APPS_SETTINGS_DOC,
        {"doc_type": "settings", "setting_id": "rag_apps", "apps": rag_apps_registry, "updated_at": rag_apps.now_iso()},
    )


def bump_knowledge_generation():
    """Any Knowledge Base change makes every cached RAG retrieval unreachable
    (the generation is part of the Context Cache key), so an app never serves
    chunks from a document that was deleted or misses one that was added."""
    global knowledge_generation
    knowledge_generation += 1


def _with_trace_headers(request: Request, trace_id: str, agent_id: str | None) -> Request:
    """A copy of `request` carrying an explicit trace ID, so the retrieval,
    cache and LLM calls a RAG query makes internally all land in one trace
    instead of each minting its own."""
    drop = {tracing.TRACE_ID_HEADER.encode(), tracing.AGENT_ID_HEADER.encode()}
    headers = [(k, v) for k, v in request.scope.get("headers", []) if k.lower() not in drop]
    headers.append((tracing.TRACE_ID_HEADER.encode(), trace_id.encode()))
    if agent_id:
        headers.append((tracing.AGENT_ID_HEADER.encode(), agent_id.encode()))
    scope = dict(request.scope)
    scope["headers"] = headers
    return Request(scope, request.receive)


@app.get("/v1/rag/apps")
async def list_rag_apps(request: Request):
    apps = sorted(rag_apps_registry.values(), key=lambda a: a.get("created_at") or "", reverse=True)
    llm_events, context_events = await asyncio.gather(
        store.recent_llm_events(limit=2000), store.recent_context_events(limit=2000)
    )
    activity = rag_apps.activity_by_app([a["app_id"] for a in apps], llm_events, context_events)
    return {
        "apps": [dict(a, activity=activity.get(a["app_id"])) for a in apps],
        "roles": list(ROLES),
        "knowledge_generation": knowledge_generation,
        "activity_window_events": 2000,
    }


@app.post("/v1/rag/apps")
async def register_rag_app(req: RagAppRequest, request: Request):
    user = require_admin(request)
    try:
        app_doc = rag_apps.normalize_app(req.app, set(ROLES))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if app_doc["app_id"] in rag_apps_registry:
        raise HTTPException(status_code=409, detail=f"RAG application '{app_doc['app_id']}' already exists")
    provider = app_doc.get("provider")
    if provider and provider not in llm_cache.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}'")
    if provider and app_doc.get("model") and app_doc["model"] not in llm_cache.PROVIDERS[provider]["models"]:
        raise HTTPException(status_code=400, detail=f"Model '{app_doc['model']}' is not offered by provider '{provider}'")

    get_knowledge_set(app_doc["set_id"])
    app_doc["created_at"] = app_doc["updated_at"] = rag_apps.now_iso()
    app_doc["created_by"] = user.get("username")

    api_key = None
    if req.issue_key:
        issued = await create_agent(
            CreateAgentRequest(
                name=f"RAG app: {app_doc['name']}",
                role=app_doc["allowed_roles"][0],
                owner=app_doc["owner"],
                description=f"Issued for RAG application '{app_doc['app_id']}'.",
            ),
            request,
        )
        api_key = issued["api_key"]
        app_doc["agent_id"] = issued["agent"].get("agent_id")

    rag_apps_registry[app_doc["app_id"]] = app_doc
    await save_rag_apps()
    logger.info("RAG application '%s' registered by %s", app_doc["app_id"], user.get("username"))
    return {
        "app": app_doc,
        "api_key": api_key,
        "notice": "This key is shown once and stored only as a hash. Save it now; it cannot be retrieved."
        if api_key else None,
    }


@app.put("/v1/rag/apps/{app_id}")
async def update_rag_app(app_id: str, req: RagAppRequest, request: Request):
    require_admin(request)
    existing = rag_apps_registry.get(app_id)
    if not existing:
        raise HTTPException(status_code=404, detail="No such RAG application")
    try:
        app_doc = rag_apps.normalize_app(req.app, set(ROLES), existing=existing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_knowledge_set(app_doc["set_id"])
    app_doc["updated_at"] = rag_apps.now_iso()
    rag_apps_registry[app_id] = app_doc
    await save_rag_apps()
    return {"app": app_doc}


@app.delete("/v1/rag/apps/{app_id}")
async def delete_rag_app(app_id: str, request: Request):
    """Remove an application and revoke the Agent Identity it was issued, so
    its key stops working on the next request rather than lingering."""
    require_admin(request)
    existing = rag_apps_registry.pop(app_id, None)
    if not existing:
        raise HTTPException(status_code=404, detail="No such RAG application")
    await save_rag_apps()
    revoked = False
    if existing.get("agent_id"):
        try:
            await revoke_agent(existing["agent_id"], request)
            revoked = True
        except HTTPException as exc:
            logger.warning("RAG app '%s' deleted; its agent %s could not be revoked: %s",
                           app_id, existing["agent_id"], exc.detail)
    return {"deleted": True, "app_id": app_id, "agent_revoked": revoked}


@app.post("/v1/agent/rag/{app_id}/query")
async def rag_query(
    app_id: str,
    req: RagQueryRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Answer a question from the Knowledge Base for one registered RAG
    application: role-filtered retrieval (Context Cache in front of it),
    then a generation through the same governed, cached path as
    /v1/llm/complete - guardrails, budgets, LLM Cache and tracing included."""
    role, subject = await authenticate(authorization, request)
    rag_app = rag_apps_registry.get(app_id)
    if not rag_app:
        raise HTTPException(status_code=404, detail=f"No RAG application '{app_id}'")
    if not rag_app.get("enabled"):
        raise HTTPException(status_code=403, detail=f"RAG application '{app_id}' is disabled")
    if role not in rag_app["allowed_roles"]:
        raise HTTPException(
            status_code=403,
            detail=f"Role '{role}' may not query RAG application '{app_id}'",
        )
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > 4000:
        raise HTTPException(status_code=400, detail="question is longer than 4000 characters")
    if not store.connected or embeddings is None:
        raise HTTPException(status_code=503, detail="Operations manager not fully initialized yet")

    ctx = trace_context(request)
    inner = _with_trace_headers(request, ctx["trace_id"], ctx.get("agent_id") or f"rag:{app_id}")
    started = time.time()
    effective = dict(rag_app)
    if req.top_k:
        effective["top_k"] = max(1, min(int(req.top_k), int(rag_app["top_k"])))

    # ---- 1. retrieval, behind the Context Cache --------------------------
    namespace = rag_apps.context_namespace(app_id)
    cache_key = rag_apps.retrieval_cache_key(
        app_id, role, question, knowledge_generation, rag_app.get("set_id") or knowledge_sets.DEFAULT_SET_ID
    )
    use_context_cache = not req.bypass_cache and int(rag_app.get("retrieval_ttl_seconds") or 0) > 0
    retrieval_status = "bypass"
    chunks = None
    if use_context_cache:
        cached = await context_get(ContextGetRequest(key=cache_key, namespace=namespace), inner, authorization)
        if cached.get("hit") and isinstance(cached.get("value"), list):
            chunks, retrieval_status = cached["value"], "hit"
        else:
            retrieval_status = "miss"

    retrieval_started = time.time()
    if chunks is None:
        # A document scope narrows the role-filtered results after the
        # search, so ask for more candidates than will be kept.
        single_doc = rag_app["document_ids"][0] if len(rag_app["document_ids"]) == 1 else None
        fetch_k = effective["top_k"] * (4 if rag_app["document_ids"] and not single_doc else 1)
        found = await search_knowledge_route(
            KnowledgeSearchRequest(
                query=question, top_k=min(25, fetch_k), document_id=single_doc,
                set_id=rag_app.get("set_id") or knowledge_sets.DEFAULT_SET_ID,
            ),
            inner, authorization,
        )
        chunks = rag_apps.select_chunks(effective, found.get("results") or [])
        if use_context_cache:
            await context_set(
                ContextSetRequest(
                    key=cache_key, value=chunks, namespace=namespace,
                    ttl_seconds=int(rag_app["retrieval_ttl_seconds"]),
                    source_latency_ms=int((time.time() - retrieval_started) * 1000),
                ),
                inner, authorization,
            )
    retrieval_ms = int((time.time() - retrieval_started) * 1000)

    # ---- 2. generation, through the governed LLM path --------------------
    llm_result = None
    if chunks:
        llm_result = await llm_complete(
            LLMCompleteRequest(
                prompt=rag_apps.build_prompt(rag_app, question, chunks),
                provider=rag_app.get("provider"),
                model=rag_app.get("model"),
                namespace=rag_apps.llm_namespace(app_id, chunks),
                semantic=None if rag_app.get("semantic_cache") else False,
                bypass_cache=req.bypass_cache,
            ),
            inner, authorization,
        )
        answer = llm_result.get("response", "")
    else:
        answer = rag_app["no_answer_text"]

    latency_ms = int((time.time() - started) * 1000)
    llm_cache_status = (llm_result or {}).get("cache", {}).get("status") if llm_result else "skipped"
    await record_span(
        ctx, kind="internal", name=f"rag: {app_id}", started=started, role=role, subject=subject,
        attributes={
            "aom.operation": "rag.query",
            "aom.rag_app": app_id,
            "aom.query": redact_text_for_storage(question[:2000]),
            "aom.retrieval_cache": retrieval_status,
            "aom.result_count": len(chunks),
            "aom.document_ids": sorted({c.get("document_id") for c in chunks if c.get("document_id")}),
            "aom.cache_status": llm_cache_status,
            "aom.cost_usd": (llm_result or {}).get("cost_usd", 0.0),
        },
    )
    return {
        "app_id": app_id,
        "role": role,
        "answer": answer,
        "sources": rag_apps.public_sources(chunks),
        "retrieval": {"cache": retrieval_status, "chunks": len(chunks), "latency_ms": retrieval_ms},
        "llm": None if not llm_result else {
            "cache": llm_result.get("cache"),
            "provider": llm_result.get("provider"),
            "model": llm_result.get("model"),
            "usage": llm_result.get("usage"),
            "cost_usd": llm_result.get("cost_usd"),
            "tokens_saved": llm_result.get("tokens_saved"),
            "cost_saved_usd": llm_result.get("cost_saved_usd"),
            "stub": llm_result.get("stub"),
        },
        "latency_ms": latency_ms,
        "trace": {"trace_id": ctx["trace_id"]},
    }


# ---------------------------------------------------------------------------
# Knowledge sets and selectable embedding models (see app/knowledge_sets.py)
# ---------------------------------------------------------------------------
def _default_knowledge_set() -> dict:
    model = embedding_models.resolve(EMBEDDING_CONFIG["model_name"])
    return knowledge_sets.default_set(
        model["id"] if model else EMBEDDING_CONFIG["model_name"],
        int(EMBEDDING_CONFIG["vector_dim"]),
        COUCHBASE_CONFIG["knowledge_index"],
    )


def all_knowledge_sets() -> dict:
    return {knowledge_sets.DEFAULT_SET_ID: _default_knowledge_set(), **knowledge_sets_registry}


def get_knowledge_set(set_id: str | None) -> dict:
    kset = all_knowledge_sets().get(set_id or knowledge_sets.DEFAULT_SET_ID)
    if not kset:
        raise HTTPException(status_code=404, detail=f"No knowledge set '{set_id}'")
    return kset


def embedder_for(kset: dict) -> embedding_models.SetEmbedder:
    """One embedder per set, created on first use. The default set reuses
    the appliance's already-loaded model; an unknown model ID (the default
    configured as something outside the catalog) falls back to it too."""
    cached = _set_embedders.get(kset["set_id"])
    if cached is not None and cached.model["id"] == (embedding_models.resolve(kset["model_id"]) or {}).get("id"):
        return cached
    if not embedding_models.resolve(kset["model_id"]):
        if kset["set_id"] != knowledge_sets.DEFAULT_SET_ID:
            raise HTTPException(status_code=500, detail=f"Knowledge set '{kset['set_id']}' has an unknown model")

        class _Default:
            model = {"id": kset["model_id"]}

            async def embed_documents(self, texts):
                return await embeddings.embed_many_async(texts)

            async def embed_query(self, text):
                return await embeddings.embed_async(text)

        return _Default()  # type: ignore[return-value]
    embedder = embedding_models.SetEmbedder(
        kset["model_id"], default_embeddings=embeddings, default_model_id=EMBEDDING_CONFIG["model_name"],
    )
    _set_embedders[kset["set_id"]] = embedder
    return embedder


def public_knowledge_sets(documents: list[dict] | None = None) -> list[dict]:
    counts: dict[str, dict] = {}
    for d in documents or []:
        c = counts.setdefault(knowledge_sets.chunk_set_id(d), {"documents": 0, "chunks": 0})
        c["documents"] += 1
        c["chunks"] += int(d.get("chunk_count") or 0)
    out = []
    for s in all_knowledge_sets().values():
        model = embedding_models.resolve(s["model_id"]) or {}
        provider = model.get("provider", "local")
        out.append({
            **s,
            "model_label": model.get("label", s["model_id"]),
            "provider": provider,
            "provider_label": embedding_models.PROVIDERS.get(provider, (provider, ""))[0],
            "available": embedding_models.is_available(model) if model else True,
            "document_count": counts.get(s["set_id"], {}).get("documents", 0),
            "chunk_count": counts.get(s["set_id"], {}).get("chunks", 0),
            "rag_apps": sorted(a for a, app in rag_apps_registry.items()
                               if (app.get("set_id") or knowledge_sets.DEFAULT_SET_ID) == s["set_id"]),
        })
    return sorted(out, key=lambda s: (not s["builtin"], s.get("created_at") or ""))


async def load_knowledge_sets():
    global knowledge_sets_registry
    stored = await store.get_setting(KNOWLEDGE_SETS_SETTINGS_DOC)
    sets = {}
    for set_id, s in ((stored or {}).get("sets") or {}).items():
        if set_id == knowledge_sets.DEFAULT_SET_ID or not embedding_models.resolve(s.get("model_id", "")):
            logger.warning("Skipping invalid stored knowledge set '%s'", set_id)
            continue
        sets[set_id] = s
    knowledge_sets_registry = sets
    # Idempotent: recreates a set's index if the cluster lost it (a restore,
    # a fresh Search node), and is a no-op otherwise.
    for s in sets.values():
        await store.ensure_knowledge_index(s["index_name"], s["vector_field"], s["dims"])
    logger.info("Knowledge sets loaded (%d besides the default)", len(sets))


async def save_knowledge_sets():
    await store.upsert_setting(
        KNOWLEDGE_SETS_SETTINGS_DOC,
        {"doc_type": "settings", "setting_id": "knowledge_sets", "sets": knowledge_sets_registry,
         "updated_at": rag_apps.now_iso()},
    )


@app.get("/v1/knowledge/embedding-models")
async def list_embedding_models(request: Request):
    return {
        "models": embedding_models.public_catalog(default_model=EMBEDDING_CONFIG["model_name"]),
        "providers": [
            {"id": p, "label": label, "requires": env or None}
            for p, (label, env) in embedding_models.PROVIDERS.items()
        ],
        "max_dims": embedding_models.MAX_DIMS,
    }


@app.post("/v1/knowledge/sets")
async def create_knowledge_set(req: KnowledgeSetRequest, request: Request):
    """Create a knowledge set: pick its embedding model, and AOM creates the
    set's own Couchbase vector index sized to that model. A hosted model is
    test-called first so a missing or wrong key fails here, not on the
    first upload."""
    user = require_admin(request)
    model = embedding_models.resolve(req.model_id)
    if not model:
        raise HTTPException(status_code=400, detail=f"Unknown embedding model '{req.model_id}'")
    if model.get("custom") and model.get("status") == "pending":
        raise HTTPException(status_code=409, detail=f"{model['label']} is still downloading - try again once it shows as ready")
    if model.get("custom") and model.get("status") == "error":
        raise HTTPException(status_code=400, detail=f"{model['label']} failed verification: {model.get('error')}")
    if not model.get("dims") or model["dims"] > embedding_models.MAX_DIMS:
        raise HTTPException(status_code=400, detail=f"{model['label']} doesn't fit the {embedding_models.MAX_DIMS}-dimension vector limit")
    if not embedding_models.is_available(model):
        env = embedding_models.PROVIDERS[model["provider"]][1]
        raise HTTPException(status_code=400, detail=f"{model['label']} needs {env} set on the operations manager")
    try:
        kset = knowledge_sets.new_set(
            name=req.name, set_id=req.set_id, model=model, base_index_name=COUCHBASE_CONFIG["knowledge_index"],
            description=req.description, created_by=user.get("username"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if kset["set_id"] in all_knowledge_sets():
        raise HTTPException(status_code=409, detail=f"Knowledge set '{kset['set_id']}' already exists")

    embedder = embedding_models.SetEmbedder(
        kset["model_id"], default_embeddings=embeddings, default_model_id=EMBEDDING_CONFIG["model_name"],
    )
    if model["provider"] not in ("local", "custom_hf"):
        try:
            await embedder.embed_query("connection test")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"{model['label']} test call failed: {exc}") from exc

    if not await store.ensure_knowledge_index(kset["index_name"], kset["vector_field"], kset["dims"]):
        raise HTTPException(status_code=502, detail=f"Could not create vector index '{kset['index_name']}' in Couchbase")

    knowledge_sets_registry[kset["set_id"]] = kset
    _set_embedders[kset["set_id"]] = embedder
    await save_knowledge_sets()
    if model["provider"] in ("local", "custom_hf"):
        # Download and load the weights now, in the background, so the first
        # upload isn't the request that waits for them.
        async def _warm():
            try:
                await embedder.embed_query("warm up")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Warming local embedding model '%s' failed: %s", model["id"], exc)
        spawn(_warm())
    logger.info("Knowledge set '%s' created on %s by %s", kset["set_id"], model["id"], user.get("username"))
    return {"set": next(s for s in public_knowledge_sets() if s["set_id"] == kset["set_id"])}


@app.delete("/v1/knowledge/sets/{set_id}")
async def delete_knowledge_set(set_id: str, request: Request):
    require_admin(request)
    if set_id == knowledge_sets.DEFAULT_SET_ID:
        raise HTTPException(status_code=400, detail="The default knowledge set can't be deleted")
    kset = knowledge_sets_registry.get(set_id)
    if not kset:
        raise HTTPException(status_code=404, detail=f"No knowledge set '{set_id}'")
    docs = [d for d in await store.list_knowledge_documents(limit=1000) if d.get("set_id") == set_id]
    if docs:
        raise HTTPException(status_code=409, detail=f"Delete this set's {len(docs)} document(s) first")
    users = [a for a, app in rag_apps_registry.items() if app.get("set_id") == set_id]
    if users:
        raise HTTPException(status_code=409, detail=f"RAG application(s) {users} use this set - move or delete them first")
    await store.delete_knowledge_index(kset["index_name"])
    knowledge_sets_registry.pop(set_id, None)
    _set_embedders.pop(set_id, None)
    await save_knowledge_sets()
    bump_knowledge_generation()
    return {"deleted": True, "set_id": set_id}


# -- Imported (custom) embedding models ----------------------------------------
CUSTOM_EMBEDDING_MODELS_SETTINGS_DOC = "settings::custom_embedding_models"


class ImportEmbeddingModelRequest(BaseModel):
    # "huggingface": a sentence-transformers model by Hugging Face ID (or an
    # absolute path to one already on the operations manager's disk).
    # "openai_compatible": any endpoint speaking OpenAI's /embeddings API -
    # Ollama, vLLM, TEI, LiteLLM, an internal gateway.
    kind: str
    model_name: str
    label: str = ""
    base_url: str = ""
    api_key: str | None = None
    query_prefix: str = ""
    doc_prefix: str = ""


async def load_custom_embedding_models():
    stored = await store.get_setting(CUSTOM_EMBEDDING_MODELS_SETTINGS_DOC)
    models, secrets = {}, {}
    for model_id, m in ((stored or {}).get("models") or {}).items():
        enc = m.pop("api_key_enc", None)
        if enc:
            try:
                secrets[model_id] = user_auth.decrypt_secret(enc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not decrypt the API key for imported model '%s': %s", model_id, exc)
        models[model_id] = m
    embedding_models.set_custom(models, secrets)
    # An import interrupted by a restart is finished now.
    for m in models.values():
        if m.get("status") == "pending":
            spawn(_verify_imported_model(m["id"]))
    logger.info("Imported embedding models loaded (%d)", len(models))


async def save_custom_embedding_models():
    out = {}
    for model_id, m in embedding_models.CUSTOM.items():
        doc = dict(m)
        if model_id in embedding_models.CUSTOM_SECRETS:
            doc["api_key_enc"] = user_auth.encrypt_secret(embedding_models.CUSTOM_SECRETS[model_id])
        out[model_id] = doc
    await store.upsert_setting(
        CUSTOM_EMBEDDING_MODELS_SETTINGS_DOC,
        {"doc_type": "settings", "setting_id": "custom_embedding_models", "models": out,
         "updated_at": rag_apps.now_iso()},
    )


async def _verify_imported_model(model_id: str):
    """Download (Hugging Face) or call (endpoint) the model once, measure its
    real dimension, and mark it ready - or record why it can't be used."""
    model = embedding_models.CUSTOM.get(model_id)
    if not model:
        return
    try:
        dims = await asyncio.to_thread(embedding_models.probe_dims, model)
        model.update({"dims": dims, "status": "ready", "error": None})
        logger.info("Imported embedding model '%s' ready (%d dims)", model_id, dims)
    except Exception as exc:  # noqa: BLE001
        model.update({"status": "error", "error": str(exc)[:500]})
        embedding_models.unload_local(model.get("source") or model_id)
        logger.warning("Imported embedding model '%s' failed verification: %s", model_id, exc)
    await save_custom_embedding_models()


@app.post("/v1/knowledge/embedding-models")
async def import_embedding_model(req: ImportEmbeddingModelRequest, request: Request):
    user = require_admin(request)
    try:
        model = embedding_models.custom_model(
            kind=req.kind, source=req.model_name, label=req.label, base_url=req.base_url,
            query_prefix=req.query_prefix, doc_prefix=req.doc_prefix, created_by=user.get("username"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if embedding_models.resolve(model["id"]):
        raise HTTPException(status_code=409, detail=f"A model with ID '{model['id']}' already exists - give it a different name")

    embedding_models.CUSTOM[model["id"]] = model
    if req.api_key and req.api_key.strip():
        embedding_models.CUSTOM_SECRETS[model["id"]] = req.api_key.strip()

    if model["provider"] == "custom_openai":
        # An endpoint answers in seconds, so verify inline and refuse a bad
        # URL, key or model name now rather than leaving a broken entry.
        try:
            dims = await asyncio.to_thread(embedding_models.probe_dims, model)
        except Exception as exc:  # noqa: BLE001
            embedding_models.CUSTOM.pop(model["id"], None)
            embedding_models.CUSTOM_SECRETS.pop(model["id"], None)
            raise HTTPException(status_code=400, detail=f"Test call to {model['base_url']} failed: {exc}") from exc
        model.update({"dims": dims, "status": "ready"})
        await save_custom_embedding_models()
    else:
        # A Hugging Face download can take minutes - longer than a dashboard
        # request may run - so it finishes in the background and the page
        # polls the model's status.
        await save_custom_embedding_models()
        spawn(_verify_imported_model(model["id"]))
    logger.info("Embedding model '%s' imported by %s (%s)", model["id"], user.get("username"), model["source"])
    return {"model": next(m for m in embedding_models.public_catalog() if m["id"] == model["id"])}


@app.delete("/v1/knowledge/embedding-models/{model_id:path}")
async def delete_imported_embedding_model(model_id: str, request: Request):
    require_admin(request)
    model = embedding_models.CUSTOM.get(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Only imported models can be removed")
    users = [s["set_id"] for s in knowledge_sets_registry.values() if s.get("model_id") == model_id]
    if users:
        raise HTTPException(status_code=409, detail=f"Knowledge set(s) {users} use this model - delete them first")
    embedding_models.CUSTOM.pop(model_id, None)
    embedding_models.CUSTOM_SECRETS.pop(model_id, None)
    embedding_models.unload_local(model.get("source") or model_id)
    await save_custom_embedding_models()
    return {"deleted": True, "model_id": model_id}
