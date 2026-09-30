"""
Memory consolidation: dedup, importance, and rollup.

An agent that writes memory and never revisits it accumulates three
problems, and they are different problems with different fixes.

  1. **Duplication.** The same fact gets asserted again and again -
     "prefers metric units" written on every session that mentions units.
     Recall then spends its top_k on five phrasings of one fact instead of
     five different facts, which makes the context objectively worse the
     more the agent remembers.
  2. **Flatness.** Every entry is equally weighty. A throwaway aside from
     one session outranks a durable preference simply by being a closer
     lexical match, because nothing records that one of them has mattered
     forty times and the other once.
  3. **Sprawl.** Two hundred conversational fragments from a finished
     session are individually true and collectively unreadable. What the
     agent needs from a closed session is what it concluded, not a
     transcript.

Dedup, importance scoring and rollup answer those in order. Only the third
needs a model.

Superseding, not deleting
-------------------------
Consolidation never destroys its own inputs. A merged or rolled-up entry
records the IDs it came from, and those sources are marked `superseded`:
excluded from recall, retained and readable, each pointing at the entry
that replaced it. A summary is a lossy artifact produced by a model that
can be wrong, and the only way to find out it was wrong is to still have
what it was built from. Superseded entries carry their own retention
window and age out on a Couchbase TTL afterwards.

Where the model call goes
-------------------------
Rollup summarization runs through this appliance's own
`/v1/llm/complete` path - the same cache, the same token and spend
budgets, the same audit entry and the same trace span as any agent's
completion. A memory layer that quietly called a provider on its own
would be spending money outside the component sold as the control point,
which is precisely the objection this appliance exists to answer. It also
means consolidation is nearly free on the second run over similar
material, and that an operator can see what it cost.

This module is the policy and the arithmetic. The orchestration - reading
memories, calling the gateway, writing results back - lives in main.py,
which passes the completion in as a callable for the same reason evals.py
does: so consolidation exercises the real governed path rather than a
reimplementation of it.
"""
import math
import re
import time

STATUSES = ("active", "superseded")

# Importance is a weighted blend of four signals, all of which the
# appliance already knows without asking a model. The weights are the
# argument, so they are stated once, here, rather than buried in the
# formula: what an agent recalls often matters more than what it wrote
# recently, and a durable profile fact matters more than either.
IMPORTANCE_WEIGHTS = {
    "recall": 0.40,
    "reinforcement": 0.25,
    "type": 0.20,
    "recency": 0.15,
}

# A profile fact is durable by definition; a conversational fragment is
# scoped to one session and expected to age out.
TYPE_WEIGHT = {"profile": 1.0, "semantic": 0.75, "conversational": 0.35}

DEFAULT_MEMORY_CONFIG = {
    "enabled": True,
    # Near-duplicate merging.
    "dedup_enabled": True,
    # Deliberately high. A false merge destroys a distinct fact, and unlike
    # a missed merge that is not self-correcting - the next pass cannot
    # un-merge it. 0.97 catches rephrasings, not neighbours.
    "dedup_similarity_threshold": 0.97,
    # Rollup of finished sessions.
    "rollup_enabled": True,
    "rollup_memory_types": ["conversational"],
    # A session is only rolled up once it has stopped growing and has
    # enough in it to be worth summarizing.
    "rollup_min_entries": 8,
    "rollup_min_idle_hours": 24,
    # Importance scoring, and whether a recall bumps its entry's counter.
    "importance_enabled": True,
    "track_recall": True,
    # The background pass.
    "interval_minutes": 60,
    "max_users_per_pass": 25,
    # How long a superseded entry is kept before Couchbase expires it.
    "retain_superseded_hours": 720,
}

_INT_FIELDS = (
    "rollup_min_entries", "rollup_min_idle_hours", "interval_minutes",
    "max_users_per_pass", "retain_superseded_hours",
)
_MIN = {"rollup_min_entries": 2, "rollup_min_idle_hours": 1, "interval_minutes": 5,
        "max_users_per_pass": 1, "retain_superseded_hours": 1}
_MAX = {"rollup_min_entries": 500, "rollup_min_idle_hours": 24 * 90, "interval_minutes": 24 * 60,
        "max_users_per_pass": 500, "retain_superseded_hours": 24 * 365}

MEMORY_TYPES = ("conversational", "profile", "semantic")


def normalize_config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_MEMORY_CONFIG)
    for key, value in (cfg or {}).items():
        if key in merged:
            merged[key] = value

    for flag in ("enabled", "dedup_enabled", "rollup_enabled", "importance_enabled", "track_recall"):
        merged[flag] = bool(merged[flag])

    for field in _INT_FIELDS:
        try:
            merged[field] = int(merged[field])
        except (TypeError, ValueError):
            merged[field] = DEFAULT_MEMORY_CONFIG[field]
        merged[field] = max(_MIN[field], min(_MAX[field], merged[field]))

    try:
        threshold = float(merged["dedup_similarity_threshold"])
    except (TypeError, ValueError):
        threshold = DEFAULT_MEMORY_CONFIG["dedup_similarity_threshold"]
    # Floored well above "related". Anything under 0.90 is a different fact
    # about the same subject, and merging those loses information.
    merged["dedup_similarity_threshold"] = max(0.90, min(0.999, threshold))

    types = merged.get("rollup_memory_types") or []
    if not isinstance(types, (list, tuple)):
        types = []
    merged["rollup_memory_types"] = [t for t in MEMORY_TYPES if t in types]

    return merged


# ---------------------------------------------------------------------------
# Importance
# ---------------------------------------------------------------------------

def _parse(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


def _saturate(count: int, midpoint: float) -> float:
    """Diminishing returns rather than a linear count: the difference
    between one recall and five matters; between fifty and fifty-four it
    does not."""
    count = max(0, int(count or 0))
    return count / (count + midpoint) if (count + midpoint) else 0.0


def score_importance(entry: dict, now: float | None = None) -> float:
    """A 0-1 blend of how often this memory is actually used, how often it
    has been re-asserted, what kind of memory it is, and how recent it is.

    Recency is last, and weighted least, on purpose. Ranking memory by
    recency is what an agent's context window already does for free; the
    reason to score importance at all is to surface the durable thing that
    was written months ago and has mattered ever since.
    """
    now = now if now is not None else time.time()

    recall = _saturate(entry.get("recall_count"), 5.0)
    reinforcement = _saturate(entry.get("reinforcement_count"), 3.0)
    type_weight = TYPE_WEIGHT.get(entry.get("memory_type"), 0.5)

    age_days = max(0.0, (now - _parse(entry.get("created_at"))) / 86400.0)
    # Half-life of 60 days, so a year-old entry still scores ~0.02 on
    # recency rather than zero - old is not the same as worthless.
    recency = math.exp(-age_days / 60.0)

    score = (
        IMPORTANCE_WEIGHTS["recall"] * recall
        + IMPORTANCE_WEIGHTS["reinforcement"] * reinforcement
        + IMPORTANCE_WEIGHTS["type"] * type_weight
        + IMPORTANCE_WEIGHTS["recency"] * recency
    )
    return round(min(1.0, max(0.0, score)), 4)


def importance_band(score: float) -> str:
    if score >= 0.66:
        return "high"
    if score >= 0.36:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def _normalize_for_compare(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())


def find_duplicate_groups(
    entries: list[dict],
    threshold: float,
    exclude_types: set | None = None,
) -> list[list[dict]]:
    """Group near-identical memories.

    Single-link clustering: an entry joins the first group it is close
    enough to. Deliberately simple, and deliberately anchored - every
    member is compared against the group's *first* member rather than any
    member, so a chain of small differences cannot walk a group away from
    what it started as. That drift is the failure mode that turns
    "deduplicate" into "quietly merge two unrelated facts".

    Only entries of the same memory_type are ever grouped: a conversational
    mention and a durable profile fact can be worded identically and mean
    different things.

    `exclude_types` holds the types rollup is going to summarize, and they
    are skipped here. Dedup exists for a *fact* that has been re-asserted;
    two similar turns of one conversation are not that - they are two
    moments, and merging them destroys the sequence the summary is supposed
    to be built from. Without this, dedup runs first, collapses a chattier
    session below the rollup threshold, and that session silently never
    gets summarized at all.
    """
    exclude_types = exclude_types or set()
    groups: list[list[dict]] = []
    for entry in entries:
        if entry.get("status") == "superseded":
            continue
        if entry.get("memory_type") in exclude_types:
            continue
        vector = entry.get("embedding")
        placed = False
        for group in groups:
            anchor = group[0]
            if anchor.get("memory_type") != entry.get("memory_type"):
                continue
            if anchor.get("user_id") != entry.get("user_id"):
                continue
            similarity = cosine(vector, anchor.get("embedding")) if vector else 0.0
            if similarity == 0.0 and not vector:
                # No embedding to compare (an entry written before
                # embeddings, or one that failed to embed): fall back to
                # exact normalized text rather than guessing.
                similarity = 1.0 if _normalize_for_compare(anchor.get("content")) == _normalize_for_compare(entry.get("content")) else 0.0
            if similarity >= threshold:
                group.append(entry)
                placed = True
                break
        if not placed:
            groups.append([entry])
    return [g for g in groups if len(g) > 1]


def merge_group(group: list[dict], now: float | None = None) -> dict:
    """Collapse a duplicate group into the survivor.

    The newest entry wins on content - it is the most recent phrasing of
    the fact - while the counters accumulate across the whole group, so a
    fact asserted six times ends up scoring as one that has been
    reinforced six times rather than as six unremarkable singletons. That
    accumulation is the entire point: dedup that merely deleted copies
    would throw away the evidence that this fact matters.
    """
    now = now if now is not None else time.time()
    ordered = sorted(group, key=lambda e: _parse(e.get("created_at")))
    survivor = dict(ordered[-1])
    sources = [e for e in ordered if e is not ordered[-1]]

    survivor["reinforcement_count"] = (
        sum(int(e.get("reinforcement_count") or 0) for e in group) + len(sources)
    )
    survivor["recall_count"] = sum(int(e.get("recall_count") or 0) for e in group)
    # The oldest assertion is when this fact was actually first known.
    survivor["created_at"] = ordered[0].get("created_at") or survivor.get("created_at")
    survivor["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    survivor["consolidated_from"] = sorted(
        {*(survivor.get("consolidated_from") or []), *[e.get("memory_id") for e in sources if e.get("memory_id")]}
    )
    survivor["consolidation_kind"] = "dedup"
    survivor["status"] = "active"

    merged_metadata = {}
    for entry in ordered:
        merged_metadata.update(entry.get("metadata") or {})
    survivor["metadata"] = merged_metadata

    survivor["importance"] = score_importance(survivor, now)
    return survivor


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------

def find_rollup_sessions(entries: list[dict], cfg: dict, now: float | None = None) -> list[tuple[str, list[dict]]]:
    """Sessions finished long enough ago, and busy enough, to be worth
    summarizing. Returns [(session_id, entries)].

    A session still being written to is never rolled up: summarizing a
    conversation mid-flight produces a summary that is immediately wrong,
    and would have to be redone on the next pass anyway.
    """
    now = now if now is not None else time.time()
    wanted_types = set(cfg.get("rollup_memory_types") or [])
    idle_cutoff = now - int(cfg["rollup_min_idle_hours"]) * 3600

    by_session: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("status") == "superseded":
            continue
        if entry.get("memory_type") not in wanted_types:
            continue
        session_id = entry.get("session_id") or ""
        if not session_id:
            # No session means nothing to close; these are rolled up by
            # dedup or not at all.
            continue
        by_session.setdefault(session_id, []).append(entry)

    ready = []
    for session_id, group in by_session.items():
        if len(group) < int(cfg["rollup_min_entries"]):
            continue
        newest = max(_parse(e.get("updated_at") or e.get("created_at")) for e in group)
        if newest > idle_cutoff:
            continue
        ready.append((session_id, sorted(group, key=lambda e: _parse(e.get("created_at")))))
    return ready


SUMMARY_INSTRUCTION = (
    "You are consolidating an AI agent's memory of one finished session with a user. "
    "Below are the individual things the agent recorded, oldest first.\n\n"
    "Write a single compact summary of what is worth remembering about this user from this "
    "session. Keep concrete facts, decisions, preferences and commitments. Drop pleasantries, "
    "restatements and anything that was only true during the conversation itself. If two entries "
    "contradict each other, keep the later one and say that it superseded an earlier statement. "
    "Do not invent anything that is not in the entries. Write plain prose, no preamble, no "
    "bullet points, and no more than 120 words.\n\n"
)


def build_rollup_prompt(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        stamp = (entry.get("created_at") or "")[:19].replace("T", " ")
        lines.append(f"- [{stamp}] {(entry.get('content') or '').strip()}")
    return SUMMARY_INSTRUCTION + "\n".join(lines)


def build_rollup_doc(
    *,
    entries: list[dict],
    summary: str,
    embedding: list,
    session_id: str,
    now: float | None = None,
) -> dict:
    """The consolidated entry that replaces a session's fragments. Stored
    as `semantic` rather than `conversational`: the point of rolling a
    session up is that what survives it is knowledge about the user, not
    dialogue from one conversation."""
    now = now if now is not None else time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    first = entries[0]
    return {
        "doc_type": "agent_memory",
        "user_id": first.get("user_id"),
        "session_id": session_id,
        "memory_type": "semantic",
        "content": (summary or "").strip()[:8000],
        "metadata": {
            "consolidated": "true",
            "source_session": session_id,
            "source_count": str(len(entries)),
        },
        "embedding": embedding,
        "role": first.get("role"),
        "subject": first.get("subject"),
        "status": "active",
        "consolidation_kind": "rollup",
        "consolidated_from": [e.get("memory_id") for e in entries if e.get("memory_id")],
        "recall_count": sum(int(e.get("recall_count") or 0) for e in entries),
        "reinforcement_count": 0,
        "created_at": first.get("created_at") or stamp,
        "updated_at": stamp,
        "consolidated_at": stamp,
    }


def mark_superseded(entry: dict, superseded_by: str, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    entry = dict(entry)
    entry["status"] = "superseded"
    entry["superseded_by"] = superseded_by
    entry["superseded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    return entry


def empty_report() -> dict:
    return {
        "users_examined": 0,
        "duplicates_merged": 0,
        "groups_merged": 0,
        "sessions_rolled_up": 0,
        "entries_superseded": 0,
        "importance_updated": 0,
        "llm_calls": 0,
        "cache_hits": 0,
        "errors": [],
    }
