"""
Evaluation: does a change to this appliance make the agents better or
worse?

Nothing else here can answer that. The audit log records what happened, the
insights engine derives findings from it, and the threat detector flags
what looks dangerous - but an operator who widens a role's tool access,
edits the RBAC policy, re-ingests a server, or switches the cache's model
has no way to find out what that did to the agents downstream except by
waiting for someone to complain.

The unit of evaluation here is a *trajectory*, not an answer string. What
matters about an agent is which tool it reached for and whether it was
allowed to, and only then what came back - so a case can assert against
discovery (did the right tool surface, and did the wrong one stay hidden?),
invocation (was this allowed or denied, as intended?), or a completion
(does the answer still mean what it used to?). Those three are exactly the
three gateway surfaces this appliance owns, which is what makes evaluating
them possible at all: the gateway sees every one of them.

Case kinds
----------
  - "discover" - run a query as a role, assert that `expect_tools` appear
    in the results and that `forbid_tools` do not. This is the case kind
    that catches an RBAC or catalog change: a tool that quietly became
    discoverable to the wrong role fails here, loudly, before an agent
    finds it.
  - "invoke"   - call a tool as a role, assert the gateway's decision. An
    authorization regression is a decision that flipped.
  - "complete" - send a prompt, assert the answer still contains what it
    should, or still means what a reference answer means (cosine
    similarity over the same embedding model the rest of the appliance
    uses - a paraphrase passes, a different answer does not).

Regression, not just score
--------------------------
A score on its own is a number nobody acts on. What an operator needs is
the comparison: this run against the last one for the same dataset. So
every run is scored, stored, and then judged against its predecessor -
`improved`, `stable`, or `regressed`, with the specific cases that changed
verdict named. `regressed` is the signal worth wiring to anything else.

That wiring already exists. The LLM cache invalidates on catalog change
because an agent's answer can depend on which tools it was allowed to see;
that is the same reason a catalog change can move an eval result, so the
same trigger runs the gate (see `main.refresh_catalog_version`). The
question "you changed a tool's allowed roles - which saved trajectories
does that break?" gets answered at the moment of the change rather than
in production.

This module is the model, the scoring and the runner. Storage lives in
app/couchbase_client.py, and the wiring that gives the runner something to
call lives in main.py - the runner takes the three gateway operations as
injected callables so it exercises the same code paths a real agent hits,
never a reimplementation of them that could drift.
"""
import math
import re
import time
import uuid

CASE_KINDS = ("discover", "invoke", "complete")
RUN_STATUSES = ("passed", "failed", "error")
TREND = ("improved", "stable", "regressed", "baseline")

MAX_CASES_PER_DATASET = 200
MAX_TEXT_CHARS = 4000

# A case scoring at or above this is a pass. Discovery cases produce
# partial credit (three of four expected tools found is 0.75), so the
# threshold is what decides whether partial credit counts.
PASS_THRESHOLD = 0.999

# Default semantic threshold for a "complete" case checked against a
# reference answer. Deliberately looser than the LLM cache's 0.94: the
# cache is deciding whether to reuse an answer verbatim, this is deciding
# whether an answer still means the same thing.
DEFAULT_SIMILARITY_THRESHOLD = 0.82


def new_dataset_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:48] or "dataset"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_case(case: dict, index: int = 0) -> dict:
    """Re-validate one case. Unknown keys are dropped rather than stored:
    a dataset is executed later, possibly by a background trigger nobody is
    watching, so it should never carry fields whose meaning is unclear."""
    kind = str(case.get("kind") or "").strip().lower()
    if kind not in CASE_KINDS:
        kind = "discover"

    normalized = {
        "case_id": str(case.get("case_id") or f"case-{index + 1}")[:64],
        "kind": kind,
        "description": str(case.get("description") or "")[:300],
        "role": str(case.get("role") or "")[:64],
        "weight": 1.0,
    }

    try:
        normalized["weight"] = max(0.1, min(10.0, float(case.get("weight", 1.0))))
    except (TypeError, ValueError):
        normalized["weight"] = 1.0

    if kind == "discover":
        normalized["query"] = str(case.get("query") or "")[:MAX_TEXT_CHARS]
        normalized["top_k"] = max(1, min(25, int(case.get("top_k") or 5)))
        normalized["expect_tools"] = [str(t)[:200] for t in (case.get("expect_tools") or [])][:25]
        normalized["forbid_tools"] = [str(t)[:200] for t in (case.get("forbid_tools") or [])][:25]
    elif kind == "invoke":
        normalized["tool_id"] = str(case.get("tool_id") or "")[:200]
        normalized["arguments"] = case.get("arguments") if isinstance(case.get("arguments"), dict) else {}
        decision = str(case.get("expect_decision") or "ALLOW").strip().upper()
        normalized["expect_decision"] = decision if decision in ("ALLOW", "DENY") else "ALLOW"
    else:
        normalized["prompt"] = str(case.get("prompt") or "")[:MAX_TEXT_CHARS]
        normalized["expect_contains"] = [str(t)[:200] for t in (case.get("expect_contains") or [])][:10]
        normalized["expect_similar_to"] = str(case.get("expect_similar_to") or "")[:MAX_TEXT_CHARS]
        try:
            threshold = float(case.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD))
        except (TypeError, ValueError):
            threshold = DEFAULT_SIMILARITY_THRESHOLD
        normalized["similarity_threshold"] = max(0.0, min(1.0, threshold))

    return normalized


def normalize_dataset(dataset: dict) -> dict:
    name = str(dataset.get("name") or "Untitled dataset")[:120]
    cases = [normalize_case(c, i) for i, c in enumerate(dataset.get("cases") or [])][:MAX_CASES_PER_DATASET]
    return {
        "doc_type": "eval_dataset",
        "dataset_id": str(dataset.get("dataset_id") or new_dataset_id(name))[:80],
        "name": name,
        "description": str(dataset.get("description") or "")[:500],
        "enabled": bool(dataset.get("enabled", True)),
        # Whether the catalog-change trigger runs this dataset. On by
        # default: a dataset nobody runs is a dataset nobody trusts.
        "run_on_catalog_change": bool(dataset.get("run_on_catalog_change", True)),
        "cases": cases,
        "created_at": dataset.get("created_at") or now_iso(),
        "updated_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def cosine_similarity(a: list, b: list) -> float:
    """Both vectors come from ToolEmbeddings, which L2-normalizes on the
    way out, so this is a dot product in practice - the norms are kept for
    correctness if a caller ever supplies an un-normalized vector."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def score_discover(case: dict, tool_ids: list[str]) -> dict:
    """Recall over the expected tools, and a hard failure on any forbidden
    tool. Forbidden tools are absolute, not weighted: a tool the wrong role
    can now see is a security regression, and averaging it away against
    three passing expectations would hide exactly the thing worth catching.
    """
    found = [t for t in (case.get("expect_tools") or []) if t in tool_ids]
    missing = [t for t in (case.get("expect_tools") or []) if t not in tool_ids]
    leaked = [t for t in (case.get("forbid_tools") or []) if t in tool_ids]

    expected = case.get("expect_tools") or []
    score = (len(found) / len(expected)) if expected else 1.0
    if leaked:
        score = 0.0

    detail_parts = []
    if missing:
        detail_parts.append(f"expected but not discovered: {', '.join(missing)}")
    if leaked:
        detail_parts.append(f"discovered but must not be: {', '.join(leaked)}")
    if not detail_parts:
        detail_parts.append(f"{len(found)}/{len(expected) or 0} expected tool(s) discovered, none forbidden")

    return {
        "score": round(score, 4),
        "passed": score >= PASS_THRESHOLD,
        "detail": "; ".join(detail_parts),
        "observed": {"tool_ids": tool_ids[:25], "missing": missing, "leaked": leaked},
    }


def score_invoke(case: dict, decision: str, reason: str | None = None) -> dict:
    expected = case.get("expect_decision", "ALLOW")
    passed = decision == expected
    return {
        "score": 1.0 if passed else 0.0,
        "passed": passed,
        "detail": (
            f"decision {decision} as expected"
            if passed
            else f"expected {expected} but the gateway returned {decision}"
            + (f" ({reason})" if reason else "")
        ),
        "observed": {"decision": decision, "reason": (reason or "")[:300]},
    }


def score_complete(case: dict, response: str, similarity: float | None) -> dict:
    """Substring expectations and semantic similarity are checked together
    when both are set - a case can require both that an answer mentions a
    specific figure and that it still reads like the reference."""
    checks = []
    scores = []

    expect_contains = case.get("expect_contains") or []
    if expect_contains:
        lowered = (response or "").lower()
        missing = [t for t in expect_contains if t.lower() not in lowered]
        scores.append(0.0 if missing else 1.0)
        checks.append(
            f"missing expected text: {', '.join(missing)}" if missing
            else f"all {len(expect_contains)} expected phrase(s) present"
        )

    if case.get("expect_similar_to"):
        threshold = float(case.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD))
        sim = similarity if similarity is not None else 0.0
        scores.append(1.0 if sim >= threshold else round(max(0.0, sim / threshold), 4))
        checks.append(f"similarity {sim:.3f} against reference (threshold {threshold:.2f})")

    if not scores:
        # A case with no assertion at all still exercises the path; it
        # passes as long as something came back, which is a real smoke test
        # and an honest one to report as such.
        passed = bool((response or "").strip())
        return {
            "score": 1.0 if passed else 0.0,
            "passed": passed,
            "detail": "no assertion set - checked only that a non-empty answer came back",
            "observed": {"response_preview": (response or "")[:240]},
        }

    score = min(scores)
    return {
        "score": round(score, 4),
        "passed": score >= PASS_THRESHOLD,
        "detail": "; ".join(checks),
        "observed": {"response_preview": (response or "")[:240], "similarity": similarity},
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_dataset(
    dataset: dict,
    *,
    discover_fn,
    invoke_fn,
    complete_fn,
    embed_fn=None,
    trigger: str = "manual",
) -> dict:
    """Execute every case in `dataset` and return a run document.

    The three gateway operations arrive as callables rather than being
    imported, so a run exercises the same authorization, the same vector
    pre-filter and the same cache the live path uses. An eval that tested a
    reimplementation of those would pass happily while production broke.

    Each callable is expected to return, respectively:
      discover_fn(role, query, top_k) -> list[tool_id]
      invoke_fn(role, tool_id, arguments) -> (decision, reason)
      complete_fn(role, prompt)         -> response text
    and to raise rather than return a sentinel on failure - a case that
    errored is recorded as an error, never silently as a failure, because
    the two need different responses from whoever reads the run.
    """
    started = time.time()
    results = []

    for case in dataset.get("cases") or []:
        case_started = time.time()
        try:
            if case["kind"] == "discover":
                tool_ids = await discover_fn(case.get("role"), case.get("query"), case.get("top_k", 5))
                outcome = score_discover(case, list(tool_ids or []))
            elif case["kind"] == "invoke":
                decision, reason = await invoke_fn(case.get("role"), case.get("tool_id"), case.get("arguments") or {})
                outcome = score_invoke(case, decision, reason)
            else:
                response = await complete_fn(case.get("role"), case.get("prompt"))
                similarity = None
                if case.get("expect_similar_to") and embed_fn is not None:
                    try:
                        similarity = cosine_similarity(
                            embed_fn(response or ""), embed_fn(case["expect_similar_to"])
                        )
                    except Exception:  # noqa: BLE001
                        similarity = None
                outcome = score_complete(case, response or "", similarity)
            status = "passed" if outcome["passed"] else "failed"
            error = None
        except Exception as exc:  # noqa: BLE001
            outcome = {"score": 0.0, "passed": False, "detail": f"case errored: {exc}", "observed": {}}
            status = "error"
            error = str(exc)[:500]

        results.append({
            "case_id": case["case_id"],
            "kind": case["kind"],
            "description": case.get("description") or "",
            "role": case.get("role"),
            "weight": case.get("weight", 1.0),
            "status": status,
            "score": outcome["score"],
            "detail": outcome["detail"],
            "observed": outcome.get("observed") or {},
            "error": error,
            "latency_ms": int((time.time() - case_started) * 1000),
        })

    return build_run(dataset, results, started, trigger)


def build_run(dataset: dict, results: list[dict], started: float, trigger: str) -> dict:
    total_weight = sum(float(r.get("weight") or 1.0) for r in results) or 1.0
    weighted = sum(float(r.get("score") or 0.0) * float(r.get("weight") or 1.0) for r in results)
    score = round(weighted / total_weight, 4)

    passed = sum(1 for r in results if r["status"] == "passed")
    failed = sum(1 for r in results if r["status"] == "failed")
    errored = sum(1 for r in results if r["status"] == "error")

    return {
        "doc_type": "eval_run",
        "run_id": f"{dataset.get('dataset_id')}::{int(time.time() * 1000)}",
        "dataset_id": dataset.get("dataset_id"),
        "dataset_name": dataset.get("name"),
        "trigger": trigger,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "finished_at": now_iso(),
        "duration_ms": int((time.time() - started) * 1000),
        "score": score,
        "case_count": len(results),
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "status": "passed" if (failed == 0 and errored == 0 and results) else ("error" if errored else "failed"),
        "results": results,
    }


def compare_runs(current: dict, previous: dict | None) -> dict:
    """The part an operator actually acts on: what moved since last time.

    A dropped score alone is not the useful output - which cases flipped
    is. `newly_failing` is the list worth blocking a change on; a run with
    no predecessor is the baseline and is never a regression.
    """
    if not previous:
        return {
            "trend": "baseline",
            "delta": 0.0,
            "previous_run_id": None,
            "previous_score": None,
            "newly_failing": [],
            "newly_passing": [],
            "summary": "First run for this dataset - recorded as the baseline to compare future runs against.",
        }

    prev_by_case = {r["case_id"]: r for r in (previous.get("results") or [])}
    newly_failing = [
        r["case_id"] for r in (current.get("results") or [])
        if r["status"] != "passed" and prev_by_case.get(r["case_id"], {}).get("status") == "passed"
    ]
    newly_passing = [
        r["case_id"] for r in (current.get("results") or [])
        if r["status"] == "passed" and prev_by_case.get(r["case_id"], {}).get("status") not in (None, "passed")
    ]

    delta = round(float(current.get("score") or 0.0) - float(previous.get("score") or 0.0), 4)
    if newly_failing:
        trend = "regressed"
    elif delta < -0.0001:
        trend = "regressed"
    elif newly_passing or delta > 0.0001:
        trend = "improved"
    else:
        trend = "stable"

    if trend == "regressed":
        summary = (
            f"Score moved {delta:+.3f} against the previous run"
            + (f"; newly failing: {', '.join(newly_failing)}" if newly_failing else "")
        )
    elif trend == "improved":
        summary = (
            f"Score moved {delta:+.3f} against the previous run"
            + (f"; newly passing: {', '.join(newly_passing)}" if newly_passing else "")
        )
    else:
        summary = "No change against the previous run."

    return {
        "trend": trend,
        "delta": delta,
        "previous_run_id": previous.get("run_id"),
        "previous_score": previous.get("score"),
        "newly_failing": newly_failing,
        "newly_passing": newly_passing,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Starter dataset
# ---------------------------------------------------------------------------
# Seeded on first boot against the bundled sample servers, for the same
# reason the samples and the offline LLM stub exist: the feature has to be
# demonstrable before anyone has written anything of their own. Every case
# below asserts something this appliance genuinely guarantees, so a failing
# run means something actually broke.

STARTER_DATASET = {
    "dataset_id": "gateway-baseline",
    "name": "Gateway baseline",
    "description": (
        "Trajectory checks against the bundled sample servers: that RBAC still hides what it should, "
        "that an unregistered server is still unreachable, and that a quarantined tool stays out of discovery."
    ),
    "run_on_catalog_change": True,
    "cases": [
        {
            "case_id": "support-finds-tickets",
            "kind": "discover",
            "description": "A support agent asking about customer tickets should find the Zendesk tools.",
            "role": "support_agent",
            "query": "look up a customer's open support tickets",
            "top_k": 5,
            "expect_tools": ["zendesk::search_tickets"],
            "forbid_tools": ["snowflake::manage_users"],
        },
        {
            "case_id": "support-cannot-see-admin",
            "kind": "discover",
            "description": "Snowflake admin tooling must never surface for a support agent, however the query is phrased.",
            "role": "support_agent",
            "query": "manage database users and warehouse settings",
            "top_k": 10,
            "expect_tools": [],
            "forbid_tools": ["snowflake::manage_users", "snowflake::manage_warehouse"],
        },
        {
            "case_id": "finance-analytics-only",
            "kind": "discover",
            "description": "A finance analyst gets Snowflake analytics, not Snowflake administration.",
            "role": "finance_analyst",
            "query": "run a revenue query against the warehouse",
            "top_k": 10,
            "forbid_tools": ["snowflake::manage_users", "snowflake::manage_warehouse"],
        },
        {
            "case_id": "unregistered-server-denied",
            "kind": "invoke",
            "description": "The shadow-diagnostics server is never registered, so its tools are unreachable for anyone.",
            "role": "admin",
            "tool_id": "shadow-diagnostics::run_diagnostic",
            "arguments": {},
            "expect_decision": "DENY",
        },
        {
            "case_id": "quarantined-tool-denied",
            "kind": "invoke",
            "description": "A tool quarantined for metadata poisoning stays uninvokable until an admin releases it.",
            "role": "admin",
            "tool_id": "docs-search::search_docs",
            "arguments": {"query": "refund policy"},
            "expect_decision": "DENY",
        },
        {
            "case_id": "support-denied-admin-tool",
            "kind": "invoke",
            "description": "Authorization is re-checked on invoke, so skipping discovery does not help a support agent.",
            "role": "support_agent",
            "tool_id": "snowflake::manage_users",
            "arguments": {},
            "expect_decision": "DENY",
        },
    ],
}
