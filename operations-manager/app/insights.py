"""
Insights engine: turns the tool catalog, server registry, and recent audit
log into a list of findings the Dashboard and Insights page can render as
severity-tagged cards - the same "highest severity findings" pattern used
elsewhere in Couchbase's agent tooling, applied here to operations-manager
hygiene and security signal instead of query/index tuning.

Most findings here are *derived*, not stored - recomputed on each request
from whatever is currently in Couchbase, nothing to approve or apply. The
two hijack-detection finding types are the exception: a quarantined-tool
finding reflects a real, stored trust_status change (see
app/hijack_detection.py and the Threat Detection page's release/quarantine
actions), and a cross-tool hijack chain finding is a correlation over the
audit log, not something this module writes anywhere itself.
"""
from collections import Counter, defaultdict

from app import hijack_detection
from config import HIJACK_CHAIN_WINDOW_SECONDS

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Thresholds for pattern detection over the lookback window.
REPEATED_DENY_THRESHOLD = 3
AUTH_FAILURE_THRESHOLD = 5
UNTRUSTED_ATTEMPT_THRESHOLD = 1


def compute_insights(
    servers: list[dict],
    tools: list[dict],
    log_entries: list[dict],
    eval_runs: list[dict] | None = None,
) -> list[dict]:
    findings: list[dict] = []

    tools_by_server: dict[str, list[dict]] = defaultdict(list)
    for t in tools:
        tools_by_server[t.get("server_id")].append(t)

    # -- Unclassified tools (deny-by-default, never explicitly reviewed) ----
    # Quarantined tools are excluded: they're blocked for every role whatever
    # their RBAC policy, and already reported (critical) by the hijack/drift
    # findings below -- counting them here too just double-reported the
    # deliberately poisoned docs-search fixture.
    unclassified = [
        t for t in tools
        if t.get("risk_level") == "unclassified" and t.get("trust_status") != "quarantined"
    ]
    if unclassified:
        names = ", ".join(f"`{t['tool_id']}`" for t in unclassified[:6])
        more = f" and {len(unclassified) - 6} more" if len(unclassified) > 6 else ""
        findings.append({
            "id": "unclassified-tools",
            "severity": "medium",
            "title": "Unclassified tools are defaulting to admin-only",
            "summary": (
                f"{len(unclassified)} ingested tool(s) have no reviewed RBAC policy and fell back to the "
                f"deny-by-default rule (admin-only, risk_level=unclassified): {names}{more}. Assign explicit "
                f"allowed_roles in `rbac_policy.py` (bundled servers), or register the server with a reviewed "
                f"`tool_policies` map (registered servers) so the right roles can discover them."
            ),
            "tags": ["RBAC", "REVIEW NEEDED"],
        })

    # -- Trusted servers with an empty catalog -------------------------------
    empty_trusted = [
        s for s in servers
        if s.get("trust_status") == "trusted" and len(tools_by_server.get(s.get("server_id"), [])) == 0
    ]
    for s in empty_trusted:
        findings.append({
            "id": f"empty-catalog-{s.get('server_id')}",
            "severity": "medium",
            "title": f"Trusted server `{s.get('server_id')}` has no ingested tools",
            "summary": (
                f"'{s.get('label', s.get('server_id'))}' is registered and trusted, but its tool catalog is "
                f"empty - either it has no tools, or ingestion failed (unreachable MCP endpoint, bad "
                f"mcp_url). Use \"Re-ingest\" on the Servers page and check the operations-manager logs."
            ),
            "tags": ["CATALOG", "STALE"],
        })

    # -- Quarantined tools (suspected MCP Tool Hijacking / metadata poisoning)
    quarantined = [t for t in tools if t.get("trust_status") == "quarantined"]
    for t in quarantined:
        auto = t.get("hijack_manual_override") != "quarantined"
        categories = sorted({s.get("category") for s in (t.get("hijack_signals") or [])})
        findings.append({
            "id": f"quarantined-tool-{t.get('tool_id')}",
            "severity": "critical",
            "title": f"`{t.get('tool_id')}` is quarantined for suspected MCP Tool Hijacking",
            "summary": (
                (
                    f"Auto-quarantined at ingest/scan time - its description matched metadata-poisoning "
                    f"pattern(s): {', '.join(categories) or 'unspecified'}. "
                    if auto else "Manually quarantined by an admin. "
                )
                + "It is excluded from discovery and invoke for every role until released from the Threat "
                "Detection page. Review the matched signals there before releasing it."
            ),
            "tags": ["SECURITY", "TOOL HIJACKING", "QUARANTINED"],
        })

    if not log_entries:
        return _sorted(findings)

    # -- Possible cross-tool hijack chains (response poisoning -> escalation)
    tools_by_id = {t["tool_id"]: t for t in tools if t.get("tool_id")}
    findings.extend(hijack_detection.detect_hijack_chains(log_entries, tools_by_id, window_seconds=HIJACK_CHAIN_WINDOW_SECONDS))

    # -- Repeated invalid API key attempts (credential probing) -------------
    auth_failures = [e for e in log_entries if e.get("action") == "authenticate" and e.get("decision") == "DENY"]
    if len(auth_failures) >= AUTH_FAILURE_THRESHOLD:
        findings.append({
            "id": "repeated-auth-failures",
            "severity": "critical",
            "title": "Repeated invalid API key attempts",
            "summary": (
                f"{len(auth_failures)} authentication failure(s) in the last {len(log_entries)} audit-log "
                f"entries examined - consistent with a client probing for a valid API key rather than an "
                f"occasional typo. Consider rotating keys and checking source IPs at the ingress in front "
                f"of this appliance (this log doesn't capture network-level origin)."
            ),
            "tags": ["SECURITY", "CREDENTIAL PROBE"],
        })

    # -- Attempted invoke of an unregistered/untrusted tool ------------------
    untrusted_attempts = [
        e for e in log_entries
        if e.get("action") == "invoke" and e.get("decision") == "DENY"
        and e.get("reason") in ("tool not found in the vetted Couchbase catalog", "owning server is not registered")
    ]
    if len(untrusted_attempts) >= UNTRUSTED_ATTEMPT_THRESHOLD:
        by_tool = Counter(e.get("tool_id") or "unknown" for e in untrusted_attempts)
        top = ", ".join(f"`{tid}` ({n}x)" for tid, n in by_tool.most_common(5))
        findings.append({
            "id": "untrusted-invoke-attempts",
            "severity": "high",
            "title": "Invoke attempted against an unregistered or untrusted tool",
            "summary": (
                f"{len(untrusted_attempts)} invoke request(s) targeted a tool_id outside the vetted catalog: "
                f"{top}. This is exactly the failure mode the operations manager exists to close - a caller (or "
                f"a compromised/misled agent) reaching for a tool that was never registered as trusted. No "
                f"downstream call was made in any of these cases."
            ),
            "tags": ["SECURITY", "UNTRUSTED SERVER"],
        })

    # -- Repeated RBAC denials per (role, tool) ------------------------------
    rbac_denies = [
        e for e in log_entries
        if e.get("action") == "invoke" and e.get("decision") == "DENY"
        and e.get("reason", "").startswith("role")
    ]
    by_role_tool: dict[tuple, int] = Counter((e.get("role"), e.get("tool_id")) for e in rbac_denies)
    for (role, tool_id), count in by_role_tool.items():
        if count >= REPEATED_DENY_THRESHOLD:
            findings.append({
                "id": f"repeated-rbac-deny-{role}-{tool_id}",
                "severity": "high",
                "title": f"Repeated authorization denials for role `{role}` on `{tool_id}`",
                "summary": (
                    f"Role '{role}' was denied invoke access to `{tool_id}` {count} time(s) recently. This "
                    f"is either a caller/agent repeatedly reaching outside its permitted tool set (worth "
                    f"investigating why), or a role that genuinely needs this tool and should be added to "
                    f"its allowed_roles."
                ),
                "tags": ["RBAC", "SUGGESTED REVIEW"],
            })

    # -- Critical-risk tools successfully invoked (informational) -----------
    critical_tool_ids = {t["tool_id"] for t in tools if t.get("risk_level") == "critical"}
    critical_invokes = [
        e for e in log_entries
        if e.get("action") == "invoke" and e.get("decision") == "ALLOW" and e.get("tool_id") in critical_tool_ids
    ]
    if critical_invokes:
        by_tool = Counter(e.get("tool_id") for e in critical_invokes)
        top = ", ".join(f"`{tid}` ({n}x)" for tid, n in by_tool.most_common(5))
        findings.append({
            "id": "critical-tool-invocations",
            "severity": "low",
            "title": "Critical-risk tools were invoked",
            "summary": (
                f"{len(critical_invokes)} successful invocation(s) of critical-risk tool(s): {top}. Not "
                f"necessarily wrong - these are logged here purely so critical-risk activity is visible at "
                f"a glance rather than buried in the audit log."
            ),
            "tags": ["AUDIT", "HIGH-RISK TOOL"],
        })

    # -- Tool definitions that changed after they were approved -------------
    # The rug pull: a tool that passed review and whose upstream server now
    # returns something different. Distinct from a hijack finding on
    # purpose - nothing about the new text has to look malicious for the
    # change itself to invalidate the approval it was given.
    drifted = [t for t in tools if t.get("drift_status") == "drifted"]
    if drifted:
        for tool in drifted[:8]:
            changes = tool.get("drift_changes") or []
            kinds = ", ".join(sorted({c.get("kind", "definition") for c in changes})) or "definition"
            detail = "; ".join(c.get("detail", "") for c in changes[:3])
            findings.append({
                "id": f"tool-drift-{tool.get('tool_id')}",
                "severity": _drift_severity(changes),
                "title": f"`{tool.get('tool_id')}` changed after it was approved",
                "summary": (
                    f"This tool's {kinds} no longer matches the definition an admin approved"
                    + (f" ({tool.get('approved_at')})" if tool.get("approved_at") else "")
                    + f". {detail}. It has been quarantined and is excluded from discovery and invoke for "
                    f"every role until the new definition is reviewed and released from the Threat "
                    f"Detection page, where the previous text is shown alongside it."
                ),
                "tags": ["SECURITY", "DEFINITION DRIFT", "QUARANTINED"],
            })

    # -- Calls stopped by a rate limit or budget -----------------------------
    limited = [e for e in log_entries if e.get("decision") == "DENY" and str(e.get("reason", "")).startswith("limit:")]
    if limited:
        by_subject = Counter(e.get("subject") for e in limited)
        top = ", ".join(f"{subj} ({n}x)" for subj, n in by_subject.most_common(4))
        findings.append({
            "id": "governance-limited-calls",
            "severity": "medium",
            "title": "Calls are being stopped by a rate limit or budget",
            "summary": (
                f"{len(limited)} call(s) in the lookback window were refused by the limits policy: {top}. "
                f"Either an agent is consuming more than its share - the loop this control exists to "
                f"catch - or the ceiling is set below what normal traffic needs. Both are visible under "
                f"Settings -> Limits & Budgets, which also shows current usage against each limit."
            ),
            "tags": ["GOVERNANCE", "RATE LIMIT"],
        })

    # -- Eval regressions ----------------------------------------------------
    for run in (eval_runs or []):
        comparison = run.get("comparison") or {}
        if comparison.get("trend") != "regressed":
            continue
        newly_failing = comparison.get("newly_failing") or []
        findings.append({
            "id": f"eval-regression-{run.get('run_id')}",
            "severity": "high" if newly_failing else "medium",
            "title": f"Evaluation regressed: {run.get('dataset_name') or run.get('dataset_id')}",
            "summary": (
                f"The last run of this dataset scored {run.get('score')} "
                f"({comparison.get('delta'):+.3f} against the previous run), triggered by "
                f"{run.get('trigger')}."
                + (f" Newly failing: {', '.join(f'`{c}`' for c in newly_failing[:6])}." if newly_failing else "")
                + " Open the Evaluations page to see which assertion moved and what the gateway "
                "actually returned."
            ),
            "tags": ["EVALUATION", "REGRESSION"],
        })

    return _sorted(findings)


def _drift_severity(changes: list[dict]) -> str:
    """A schema change outranks a description change: it alters what a
    caller may actually send, not just what the model believes."""
    kinds = {c.get("kind") for c in changes or []}
    return "critical" if "schema" in kinds else "high"


def _sorted(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: SEVERITY_RANK.get(f["severity"], 9))


def decision_breakdown(log_entries: list[dict]) -> dict:
    counts = Counter(e.get("decision", "UNKNOWN") for e in log_entries)
    return {"ALLOW": counts.get("ALLOW", 0), "DENY": counts.get("DENY", 0), "ERROR": counts.get("ERROR", 0)}


def action_breakdown(log_entries: list[dict]) -> dict:
    counts = Counter(e.get("action", "unknown") for e in log_entries)
    return {"discover": counts.get("discover", 0), "invoke": counts.get("invoke", 0), "authenticate": counts.get("authenticate", 0)}


def hourly_volume(log_entries: list[dict], buckets: int = 12) -> list[dict]:
    """Bucket log entries by their ISO-8601 UTC hour (`...THH:MM:SSZ`),
    returning the most recent `buckets` hours oldest-first, zero-filled."""
    import datetime as _dt

    now = _dt.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    hour_keys = [(now - _dt.timedelta(hours=i)) for i in range(buckets - 1, -1, -1)]
    counts = {h.strftime("%Y-%m-%dT%H:00:00Z"): 0 for h in hour_keys}

    for e in log_entries:
        ts = e.get("timestamp") or ""
        if len(ts) >= 13:
            hour_key = ts[:13] + ":00:00Z"
            if hour_key in counts:
                counts[hour_key] += 1

    return [
        {"hour": h.strftime("%H:00"), "timestamp": h.strftime("%Y-%m-%dT%H:00:00Z"), "count": counts[h.strftime("%Y-%m-%dT%H:00:00Z")]}
        for h in hour_keys
    ]
