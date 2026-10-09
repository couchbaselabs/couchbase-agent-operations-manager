"""
RAG Applications - registered, governed retrieval-augmented generation
served straight from the Knowledge Base.

The Knowledge Base already gives agents governed retrieval (one Couchbase
Search request: vector kNN + the caller's role as a pre-filter), and
/v1/llm/complete already gives them a governed, cached model call. A RAG
application is the registered combination of the two: a named app with its
own retrieval settings, prompt and model, served at

    POST /v1/agent/rag/{app_id}/query   {"question": "..."}

so an application team gets an answer endpoint instead of re-implementing
retrieve -> prompt -> generate (and its caching, guardrails, budgets and
tracing) in their own service.

Two caches sit on the request path, and each caches what it is good at:

  1. Context Cache - the retrieved chunks, keyed by the normalized question,
     the caller's role and the Knowledge Base generation. A repeat question
     skips the embedding call and the vector search entirely. The role is
     in the key because retrieval is role-filtered; the generation is in
     the key so adding or deleting a document makes every older entry
     unreachable at once instead of waiting out its TTL.

  2. LLM Cache - the generated answer, through the same code path as
     /v1/llm/complete. Its namespace carries a fingerprint of exactly which
     chunks were retrieved, so semantic (paraphrase) matching only ever
     compares prompts built from the same context - "what's the refund
     window?" and "how long do I have to return something?" can share an
     answer, but two questions that retrieved different documents never can.

Retrieval never widens access: the caller's role is the filter, and an
app's document scope can only narrow it further. Registration itself is an
admin action (dashboard session), and by default issues the app its own
Agent Identity so its traffic is attributed, scoped and revocable like any
other agent's.

Nothing in here talks to Couchbase; persistence lives in
app/couchbase_client.py (one settings document) and orchestration in
app/main.py.
"""
import hashlib
import re
import time

APP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
MAX_TOP_K = 10
MAX_DOCUMENT_SCOPE = 200

DEFAULT_APP: dict = {
    "app_id": "",
    "name": "",
    "description": "",
    "owner": "",
    "enabled": True,
    # Roles whose API keys may query this app. Retrieval is always filtered
    # by the *caller's* role, so listing a role here never grants it any
    # document it couldn't already read.
    "allowed_roles": [],
    # The knowledge set (embedding model + vector index) retrieval runs
    # against - see app/knowledge_sets.py.
    "set_id": "default",
    # Empty = every document in the set the caller's role can read.
    "document_ids": [],
    "top_k": 5,
    "min_score": 0.0,
    # Context Cache lifetime for retrieved chunks. KB changes invalidate
    # immediately regardless (see module docstring), so this only bounds
    # how long an unchanged KB serves the same chunks for a question.
    "retrieval_ttl_seconds": 1800,
    # None = the LLM Caching policy's provider/model.
    "provider": None,
    "model": None,
    # Paraphrase matching for answers (only ever within identical context).
    "semantic_cache": True,
    "system_prompt": (
        "You are a helpful assistant. Answer using only the numbered context passages. "
        "Cite the passages you used as [1], [2], ... If the context does not contain the "
        "answer, say you don't know rather than guessing."
    ),
    "no_answer_text": "I couldn't find anything in the knowledge base that answers that.",
    "agent_id": None,
    "created_at": None,
    "created_by": None,
    "updated_at": None,
}

EDITABLE_FIELDS = (
    "name", "description", "owner", "enabled", "allowed_roles", "set_id", "document_ids", "top_k",
    "min_score", "retrieval_ttl_seconds", "provider", "model", "semantic_cache",
    "system_prompt", "no_answer_text",
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:40].strip("-") or "rag-app"


def _clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, type(default)(value)))
    except (TypeError, ValueError):
        return default


def normalize_app(raw: dict | None, roles: set, existing: dict | None = None) -> dict:
    """Validate one application definition. Raises ValueError with a message
    fit for the dashboard. `existing` is the stored app on an update - only
    EDITABLE_FIELDS may change, identity fields are carried over."""
    raw = raw or {}
    app = dict(DEFAULT_APP)
    if existing:
        app.update(existing)
        for key in EDITABLE_FIELDS:
            if key in raw:
                app[key] = raw[key]
    else:
        for key in DEFAULT_APP:
            if key in raw:
                app[key] = raw[key]

    app["name"] = str(app.get("name") or "").strip()[:80]
    if not app["name"]:
        raise ValueError("name is required")
    app_id = str(app.get("app_id") or slugify(app["name"])).strip().lower()
    if not APP_ID_PATTERN.match(app_id):
        raise ValueError("app_id must be 2-40 characters: lowercase letters, digits and dashes")
    app["app_id"] = app_id
    app["description"] = str(app.get("description") or "").strip()[:500]
    app["owner"] = str(app.get("owner") or "").strip()[:120]
    app["enabled"] = bool(app.get("enabled", True))

    allowed = [str(r) for r in (app.get("allowed_roles") or [])]
    unknown = [r for r in allowed if r not in roles]
    if unknown:
        raise ValueError(f"Unknown role(s): {unknown}")
    if not allowed:
        raise ValueError("Pick at least one role that may query this application")
    app["allowed_roles"] = sorted(set(allowed))

    app["set_id"] = str(app.get("set_id") or "default").strip().lower()[:32]
    docs = [str(d).strip() for d in (app.get("document_ids") or []) if str(d).strip()]
    app["document_ids"] = sorted(set(docs))[:MAX_DOCUMENT_SCOPE]
    app["top_k"] = _clamp(app.get("top_k"), 1, MAX_TOP_K, 5)
    app["min_score"] = _clamp(app.get("min_score"), 0.0, 1.0, 0.0)
    app["retrieval_ttl_seconds"] = _clamp(app.get("retrieval_ttl_seconds"), 0, 7 * 24 * 3600, 1800)
    app["provider"] = (str(app["provider"]).strip() or None) if app.get("provider") else None
    app["model"] = (str(app["model"]).strip() or None) if app.get("model") else None
    app["semantic_cache"] = bool(app.get("semantic_cache", True))
    app["system_prompt"] = str(app.get("system_prompt") or DEFAULT_APP["system_prompt"]).strip()[:4000]
    app["no_answer_text"] = str(app.get("no_answer_text") or DEFAULT_APP["no_answer_text"]).strip()[:500]
    return app


def normalize_question(question: str) -> str:
    q = re.sub(r"\s+", " ", (question or "").strip().lower())
    return q.rstrip("?.! ")


def retrieval_cache_key(app_id: str, role: str, question: str, kb_generation: int, set_id: str = "default") -> str:
    digest = hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()[:32]
    return f"rag:{app_id}:{set_id}:{role}:g{kb_generation}:{digest}"


def context_namespace(app_id: str) -> str:
    return f"rag:{app_id}"


def context_fingerprint(chunks: list[dict]) -> str:
    ids = sorted(str(c.get("chunk_id")) for c in chunks)
    return hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:10]


def llm_namespace(app_id: str, chunks: list[dict]) -> str:
    # <= 4 + 40 + 1 + 10 = 55 chars, inside the cache's 64-char namespace limit.
    return f"rag:{app_id}:{context_fingerprint(chunks)}"


def build_prompt(app: dict, question: str, chunks: list[dict]) -> str:
    passages = "\n\n".join(
        f"[{i}] {c.get('document_title') or c.get('document_id')} (part {int(c.get('chunk_index') or 0) + 1})\n"
        f"{(c.get('content') or '').strip()}"
        for i, c in enumerate(chunks, start=1)
    )
    # The same canonical question the retrieval cache keys on (minus the
    # lower-casing, which the LLM cache's own prompt normalization already
    # does), so "What's X?" and "what's x" hit the same cached answer too.
    canonical = re.sub(r"\s+", " ", (question or "").strip()).rstrip("?.! ")
    return f"{app['system_prompt']}\n\nContext:\n{passages}\n\nQuestion: {canonical}\nAnswer:"


def select_chunks(app: dict, results: list[dict]) -> list[dict]:
    """Apply the app's document scope and score floor to role-filtered
    search results, keeping only what the cache and the prompt need."""
    scope = set(app.get("document_ids") or [])
    chosen = []
    for r in results:
        if scope and r.get("document_id") not in scope:
            continue
        score = r.get("score")
        if app.get("min_score") and score is not None and float(score) < float(app["min_score"]):
            continue
        chosen.append({
            "chunk_id": r.get("chunk_id"),
            "document_id": r.get("document_id"),
            "document_title": r.get("document_title"),
            "chunk_index": r.get("chunk_index"),
            "content": r.get("content"),
            "score": score,
            "expires_at": r.get("expires_at"),
        })
        if len(chosen) >= int(app.get("top_k") or 5):
            break
    return chosen


def public_sources(chunks: list[dict]) -> list[dict]:
    return [
        {
            "n": i,
            "document_id": c.get("document_id"),
            "document_title": c.get("document_title"),
            "chunk_index": c.get("chunk_index"),
            "score": c.get("score"),
            "preview": (c.get("content") or "").strip()[:240],
        }
        for i, c in enumerate(chunks, start=1)
    ]


def activity_by_app(app_ids: list[str], llm_events: list[dict], context_events: list[dict]) -> dict:
    """Per-app activity from the most recent LLM and Context Cache events
    (both already recorded by the shared cache paths - nothing extra is
    written per query). Keyed by app_id."""
    stats = {
        a: {"queries": 0, "llm_hits": 0, "tokens_saved": 0, "cost_saved_usd": 0.0, "cost_usd": 0.0,
            "retrievals": 0, "retrieval_hits": 0, "last_query_at": None}
        for a in app_ids
    }

    def _app_of(namespace: str | None) -> str | None:
        parts = (namespace or "").split(":")
        return parts[1] if len(parts) >= 2 and parts[0] == "rag" and parts[1] in stats else None

    for e in llm_events:
        a = _app_of(e.get("namespace"))
        if not a or e.get("outcome") == "error":
            continue
        s = stats[a]
        s["queries"] += 1
        if str(e.get("outcome", "")).startswith("hit"):
            s["llm_hits"] += 1
        s["tokens_saved"] += int(e.get("tokens_saved") or 0)
        s["cost_saved_usd"] += float(e.get("cost_saved_usd") or 0.0)
        s["cost_usd"] += float(e.get("cost_usd") or 0.0)
        ts = e.get("timestamp")
        if ts and (not s["last_query_at"] or ts > s["last_query_at"]):
            s["last_query_at"] = ts
    for e in context_events:
        a = _app_of(e.get("namespace"))
        if not a or e.get("outcome") not in ("hit", "miss"):
            continue
        stats[a]["retrievals"] += 1
        if e.get("outcome") == "hit":
            stats[a]["retrieval_hits"] += 1
    for s in stats.values():
        s["cost_saved_usd"] = round(s["cost_saved_usd"], 6)
        s["cost_usd"] = round(s["cost_usd"], 6)
    return stats
