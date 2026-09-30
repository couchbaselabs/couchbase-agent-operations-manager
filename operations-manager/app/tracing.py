"""
Agent run and span tracing.

The audit log answers "was this call allowed?". It cannot answer "what did
this agent do, in what order, and where did it go wrong?" - those are
different questions with different shapes. An audit entry is one flat,
independent decision record; a trace is a *tree* of operations belonging to
one task, and the relationships between them are the whole point.

This module is the span model. Storage lives in
`app/couchbase_client.py` (the `traces` collection) exactly like the tool
catalog and the LLM cache, so the two cannot drift apart on what a span
document is allowed to look like.

Span kinds
----------
Seven kinds, matching the distinction the wider Couchbase AI Data Plane's
tracer draws, so a span written here and a span written by an agent
instrumented directly against Agent Catalog describe the same thing:

  - "user"        - an incoming message/task from an end user.
  - "llm"         - a model call, including intermediate reasoning turns.
  - "tool_call"   - a tool the agent decided to invoke, with its arguments.
  - "tool_result" - what that tool returned (kept separate from the call so
    a poisoned *response* is attributable independently of the request that
    triggered it - see app/hijack_detection.py).
  - "hand_off"    - context passed from one agent to another.
  - "internal"    - control flow the agent did on its own: retrieval,
    memory recall, planning, a routing decision.
  - "assistant"   - the final response handed back.

Attribute naming
----------------
Span attributes use the OpenTelemetry GenAI semantic-convention keys
(`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, and
so on) wherever a convention exists for what is being recorded. Nothing
here depends on an OpenTelemetry SDK being installed - the point is that
the JSON already sitting in Couchbase is shaped the way an exporter, or a
customer's own SQL++, would expect to find it, rather than in a private
vocabulary that would have to be translated later.

Why this is worth doing in Couchbase specifically
-------------------------------------------------
The spans land in the same bucket as the tool catalog, the agent memory
and the LLM cache. That means the question an operator actually has -
"which tool did this agent choose, from which prompt, against which
recalled memory, and did the completion come from cache?" - is one SQL++
join, not four exports correlated by hand afterwards.
"""
import time
import uuid

SPAN_KINDS = (
    "user",
    "llm",
    "tool_call",
    "tool_result",
    "hand_off",
    "internal",
    "assistant",
)

STATUSES = ("ok", "error")

MAX_ATTRIBUTE_VALUE_CHARS = 2000
MAX_ATTRIBUTES = 40
MAX_NAME_CHARS = 200

# Request headers an instrumented caller can set to join its calls into one
# run. All optional: a caller that sets none of them still gets a complete,
# single-request trace - it just won't be correlated with anything else.
TRACE_ID_HEADER = "x-aom-trace-id"
PARENT_SPAN_HEADER = "x-aom-parent-span-id"
AGENT_ID_HEADER = "x-aom-agent-id"
SESSION_ID_HEADER = "x-aom-session-id"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_trace_id() -> str:
    return uuid.uuid4().hex


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


def normalize_kind(value: str | None) -> str:
    value = (value or "").strip().lower().replace("-", "_")
    return value if value in SPAN_KINDS else "internal"


def _safe_id(value: str | None, fallback: str = "") -> str:
    """Trace/span/agent/session identifiers arrive from a caller-set header,
    so they are treated as untrusted input: kept to a conservative charset
    and length before they become part of a document ID."""
    if not value:
        return fallback
    cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in "-_.")[:64]
    return cleaned or fallback


def sanitize_attributes(attributes: dict | None) -> dict:
    """Attributes ride along with a span for querying and display. Capped
    so one caller cannot bloat a document (or the index over this
    collection) with an arbitrarily large blob - the same treatment
    agent_memory.sanitize_metadata gives memory metadata."""
    clean: dict = {}
    for key, value in list((attributes or {}).items())[:MAX_ATTRIBUTES]:
        key = str(key)[:120]
        if isinstance(value, (int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, (list, tuple)):
            clean[key] = [str(v)[:200] for v in list(value)[:20]]
        else:
            clean[key] = str(value)[:MAX_ATTRIBUTE_VALUE_CHARS]
    return clean


def trace_context_from_headers(headers) -> dict:
    """Build a trace context from the incoming request headers, minting a
    new trace ID when the caller did not supply one. `headers` is anything
    with a case-insensitive `.get()` (Starlette's Headers, or a plain
    lower-cased dict)."""
    def _h(name: str) -> str | None:
        try:
            return headers.get(name)
        except Exception:  # noqa: BLE001
            return None

    supplied = _safe_id(_h(TRACE_ID_HEADER))
    return {
        "trace_id": supplied or new_trace_id(),
        "parent_span_id": _safe_id(_h(PARENT_SPAN_HEADER)) or None,
        "agent_id": _safe_id(_h(AGENT_ID_HEADER)) or None,
        "session_id": _safe_id(_h(SESSION_ID_HEADER)) or None,
        # False when the caller supplied a trace ID: several requests are
        # being stitched into one run, so the run summary must accumulate
        # rather than be treated as complete after this request.
        "root": not supplied,
    }


def build_span(
    *,
    trace_id: str,
    kind: str,
    name: str,
    started_at: float,
    ended_at: float | None = None,
    status: str = "ok",
    span_id: str | None = None,
    parent_span_id: str | None = None,
    role: str | None = None,
    subject: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    attributes: dict | None = None,
    error: str | None = None,
) -> dict:
    """One span document. `started_at`/`ended_at` are epoch seconds (what
    `time.time()` returns) - the wall-clock ISO strings and the derived
    latency are both stored, so the timeline can be rendered without
    re-deriving it and a SQL++ ORDER BY still sorts correctly."""
    ended = ended_at if ended_at is not None else time.time()
    return {
        "doc_type": "agent_span",
        "trace_id": trace_id,
        "span_id": span_id or new_span_id(),
        "parent_span_id": parent_span_id,
        "kind": normalize_kind(kind),
        "name": str(name)[:MAX_NAME_CHARS],
        "status": status if status in STATUSES else "ok",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended)),
        "started_epoch_ms": int(started_at * 1000),
        "latency_ms": max(0, int((ended - started_at) * 1000)),
        "role": role,
        "subject": subject,
        "agent_id": agent_id,
        "session_id": session_id,
        "attributes": sanitize_attributes(attributes),
        "error": (str(error)[:1000] if error else None),
    }


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------
# A run is the roll-up of every span sharing a trace ID. It is maintained
# incrementally as spans land rather than computed on read, because the
# Traces list page wants one row per run over potentially millions of
# spans, and an aggregate over the whole span collection to render a list
# is exactly the query that stops being viable in production.

def empty_run(trace_id: str) -> dict:
    return {
        "doc_type": "agent_run",
        "trace_id": trace_id,
        "started_at": None,
        "ended_at": None,
        "latency_ms": 0,
        "span_count": 0,
        "error_count": 0,
        "status": "ok",
        "role": None,
        "subject": None,
        "agent_id": None,
        "session_id": None,
        "kinds": {},
        "tools_called": [],
        "tools_denied": [],
        "models_called": [],
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cache_hits": 0,
        "cache_misses": 0,
        "memory_recalls": 0,
        "hijack_flags": 0,
        "limit_blocks": 0,
        "first_query": None,
    }


def _append_unique(values: list, value, cap: int = 40) -> list:
    if value and value not in values and len(values) < cap:
        values.append(value)
    return values


def fold_span_into_run(run: dict, span: dict) -> dict:
    """Merge one span into its run summary. Deliberately total and
    order-independent: spans for a run can arrive out of order (a long
    completion finishing after a fast tool call), and the summary has to be
    correct either way."""
    run = dict(run or empty_run(span["trace_id"]))
    attrs = span.get("attributes") or {}

    run["span_count"] = int(run.get("span_count") or 0) + 1
    if span.get("status") == "error":
        run["error_count"] = int(run.get("error_count") or 0) + 1
        run["status"] = "error"

    kinds = dict(run.get("kinds") or {})
    kinds[span["kind"]] = int(kinds.get(span["kind"]) or 0) + 1
    run["kinds"] = kinds

    for field in ("role", "subject", "agent_id", "session_id"):
        if not run.get(field) and span.get(field):
            run[field] = span[field]

    started = span.get("started_at")
    ended = span.get("ended_at")
    if started and (not run.get("started_at") or started < run["started_at"]):
        run["started_at"] = started
    if ended and (not run.get("ended_at") or ended > run["ended_at"]):
        run["ended_at"] = ended
    run["latency_ms"] = int(run.get("latency_ms") or 0) + int(span.get("latency_ms") or 0)

    if span["kind"] == "tool_call":
        tool_id = attrs.get("aom.tool_id")
        if attrs.get("aom.decision") == "DENY":
            run["tools_denied"] = _append_unique(list(run.get("tools_denied") or []), tool_id)
        else:
            run["tools_called"] = _append_unique(list(run.get("tools_called") or []), tool_id)
        if attrs.get("aom.limit_blocked"):
            run["limit_blocks"] = int(run.get("limit_blocks") or 0) + 1

    if span["kind"] == "tool_result" and attrs.get("aom.hijack_flagged"):
        run["hijack_flags"] = int(run.get("hijack_flags") or 0) + 1

    if span["kind"] == "llm":
        run["models_called"] = _append_unique(list(run.get("models_called") or []), attrs.get("gen_ai.request.model"))
        run["prompt_tokens"] = int(run.get("prompt_tokens") or 0) + int(attrs.get("gen_ai.usage.input_tokens") or 0)
        run["completion_tokens"] = int(run.get("completion_tokens") or 0) + int(attrs.get("gen_ai.usage.output_tokens") or 0)
        run["total_tokens"] = int(run["prompt_tokens"]) + int(run["completion_tokens"])
        run["cost_usd"] = round(float(run.get("cost_usd") or 0.0) + float(attrs.get("aom.cost_usd") or 0.0), 6)
        cache_status = str(attrs.get("aom.cache_status") or "")
        if cache_status.startswith("hit"):
            run["cache_hits"] = int(run.get("cache_hits") or 0) + 1
        elif cache_status in ("miss", "bypass"):
            run["cache_misses"] = int(run.get("cache_misses") or 0) + 1

    if span["kind"] == "internal" and attrs.get("aom.operation") == "memory.search":
        run["memory_recalls"] = int(run.get("memory_recalls") or 0) + 1

    if not run.get("first_query"):
        run["first_query"] = attrs.get("aom.query") or attrs.get("gen_ai.prompt") or None

    run["updated_at"] = now_iso()
    return run
