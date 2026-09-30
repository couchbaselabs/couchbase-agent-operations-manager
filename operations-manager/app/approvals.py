"""
The human approval tier: a third verdict between allow and deny.

Until now an invoke had two outcomes. Either the caller's role was
authorized and the call went downstream, or it was not and the call was
refused. That is the right model for almost everything and the wrong model
for the small set of tools where the question is not "may this role ever do
this?" but "should this particular call happen right now?" - dropping a
production table, issuing a refund, deleting a user. RBAC cannot answer the
second question, because the answer depends on the moment rather than the
role, and encoding it as a permanent deny means the work simply never gets
done through the gateway.

So a tool can be marked as requiring approval, and a call to it parks:

    invoke  ->  202 Accepted, {approval_id, status: "pending"}
    (a human approves or denies it on the Approvals page)
    invoke again, with approval_id  ->  the call executes

Why the agent re-invokes rather than the gateway executing on approval
----------------------------------------------------------------------
An approval that fires the call itself would have to hold the caller's
request open until a human acted - minutes, sometimes - which is exactly
what the request timeout exists to forbid, and it would execute into a
connection that has very likely already gone. Handing the agent a pending ID
to poll keeps every request short, survives a gateway restart, and matches
the shape MCP's own tasks primitive settled on. The cost is one extra call
from the agent, which the SDK hides.

What an approval is bound to
----------------------------
Not just a tool. An approval records a hash of the exact arguments it was
granted for, and consuming it requires those same arguments. Without that
binding, an operator approving `delete_user(id=42)` would be handing out a
token good for `delete_user(id=1)` - the approval would be for the tool,
which is not what anyone reading the queue believes they are agreeing to.

An approval is also single-use and expires. Expiry is a Couchbase document
TTL, so an approval nobody acts on becomes a denial by simply ceasing to
exist - there is no sweeper to run and no state that can leak past its
window.
"""
import hashlib
import json
import time
import uuid

STATUSES = ("pending", "approved", "denied", "consumed", "expired")

# What may put a tool into the approval tier.
TRIGGERS = ("risk_level", "tool_id", "always")

DEFAULT_APPROVAL_CONFIG = {
    "enabled": False,
    # Any tool at or above this risk level needs approval. "off" disables
    # the risk-based rule and leaves only the explicit list below.
    "require_at_risk_level": "critical",
    # Explicit tool IDs, whatever their risk level.
    "require_for_tools": [],
    # Roles that never require approval. Empty by default - an exemption
    # should be a decision someone made.
    "exempt_roles": [],
    # How long a pending approval survives before it expires. Expiry is a
    # denial: the safe answer to "nobody looked at this" is no.
    "ttl_seconds": 3600,
    # How long an approved-but-unused approval stays usable. Short, because
    # the judgement a human made was about the situation at that moment.
    "grace_seconds": 300,
}

RISK_LEVELS = ("critical", "high", "medium", "low", "unclassified")
_RISK_RANK = {level: i for i, level in enumerate(RISK_LEVELS)}
MAX_EXPLICIT_TOOLS = 200


def normalize_config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_APPROVAL_CONFIG)
    for key, value in (cfg or {}).items():
        if key in merged:
            merged[key] = value

    merged["enabled"] = bool(merged["enabled"])

    level = str(merged.get("require_at_risk_level") or "off").strip().lower()
    merged["require_at_risk_level"] = level if level in RISK_LEVELS else "off"

    tools = merged.get("require_for_tools") or []
    if not isinstance(tools, (list, tuple)):
        tools = []
    merged["require_for_tools"] = [str(t)[:200] for t in tools][:MAX_EXPLICIT_TOOLS]

    roles = merged.get("exempt_roles") or []
    if not isinstance(roles, (list, tuple)):
        roles = []
    merged["exempt_roles"] = [str(r)[:64] for r in roles][:20]

    for field, floor, ceiling, default in (
        ("ttl_seconds", 60, 7 * 24 * 3600, DEFAULT_APPROVAL_CONFIG["ttl_seconds"]),
        ("grace_seconds", 30, 24 * 3600, DEFAULT_APPROVAL_CONFIG["grace_seconds"]),
    ):
        try:
            merged[field] = max(floor, min(ceiling, int(merged[field])))
        except (TypeError, ValueError):
            merged[field] = default

    return merged


def arguments_fingerprint(tool_id: str, arguments: dict | None) -> str:
    """A stable hash of the exact call an approval is granted for. Sorted
    keys so argument order cannot produce a different fingerprint for the
    same call."""
    try:
        material = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        material = str(arguments)
    return hashlib.sha256(f"{tool_id}\n{material}".encode("utf-8")).hexdigest()


def requires_approval(tool: dict, role: str | None, cfg: dict) -> tuple[bool, str | None]:
    """Whether this call parks for review, and which rule decided. Returns
    (False, None) when the tier is off, the role is exempt, or no rule
    matches."""
    if not cfg.get("enabled"):
        return False, None
    if role and role in (cfg.get("exempt_roles") or []):
        return False, None

    tool_id = tool.get("tool_id")
    if tool_id and tool_id in (cfg.get("require_for_tools") or []):
        return True, f"'{tool_id}' is on the explicit approval list"

    threshold = cfg.get("require_at_risk_level", "off")
    if threshold in _RISK_RANK:
        risk = str(tool.get("risk_level") or "unclassified").lower()
        if risk in _RISK_RANK and _RISK_RANK[risk] <= _RISK_RANK[threshold]:
            return True, f"risk level '{risk}' is at or above the '{threshold}' approval threshold"

    return False, None


def new_approval(
    *,
    tool_id: str,
    arguments: dict,
    role: str,
    subject: str,
    reason: str,
    trace_id: str | None = None,
    server_id: str | None = None,
    risk_level: str | None = None,
    cfg: dict,
) -> dict:
    now = time.time()
    return {
        "doc_type": "approval",
        "approval_id": f"apr_{uuid.uuid4().hex[:20]}",
        "status": "pending",
        "tool_id": tool_id,
        "server_id": server_id,
        "risk_level": risk_level,
        # The arguments are stored so a reviewer can see what they are
        # agreeing to; the fingerprint is what actually binds the approval.
        "arguments": arguments or {},
        "arguments_fingerprint": arguments_fingerprint(tool_id, arguments),
        "role": role,
        "subject": subject,
        "trace_id": trace_id,
        "requested_reason": reason,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + int(cfg["ttl_seconds"]))),
        "decided_at": None,
        "decided_by": None,
        "decision_note": None,
        "consumed_at": None,
    }


def decide(approval: dict, *, approved: bool, username: str, note: str | None, cfg: dict) -> dict:
    """Record a human's decision. Only a pending approval can be decided -
    re-deciding a consumed one would resurrect a call that already ran."""
    approval = dict(approval)
    approval["status"] = "approved" if approved else "denied"
    approval["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    approval["decided_by"] = username
    approval["decision_note"] = (note or "")[:500] or None
    if approved:
        approval["usable_until"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(cfg["grace_seconds"]))
        )
    return approval


def _parse(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


def redeem(approval: dict | None, *, tool_id: str, arguments: dict, role: str, subject: str) -> tuple[bool, str]:
    """Can this approval be used, right now, for exactly this call?

    Every failure mode is a distinct message on purpose: an agent that
    retried too late and an agent presenting someone else's approval are
    very different situations, and collapsing them into "invalid" makes the
    first one undiagnosable.
    """
    if not approval:
        return False, "no such approval - it may have expired"
    status = approval.get("status")
    if status == "pending":
        return False, "this call is still awaiting a human decision"
    if status == "denied":
        note = approval.get("decision_note")
        return False, "a reviewer denied this call" + (f": {note}" if note else "")
    if status == "consumed":
        return False, "this approval has already been used - approvals are single-use"
    if status == "expired":
        return False, "this approval expired before it was used"
    if status != "approved":
        return False, f"approval is in an unusable state ('{status}')"

    usable_until = _parse(approval.get("usable_until"))
    if usable_until and time.time() > usable_until:
        return False, "this approval was granted but its usable window has passed"

    if approval.get("subject") != subject or approval.get("role") != role:
        return False, "this approval was granted to a different caller"

    if approval.get("arguments_fingerprint") != arguments_fingerprint(tool_id, arguments):
        return False, (
            "the arguments do not match the ones this call was approved for - "
            "an approval is bound to the exact call a reviewer saw"
        )

    return True, "approved"


def consume(approval: dict) -> dict:
    approval = dict(approval)
    approval["status"] = "consumed"
    approval["consumed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return approval


def public_approval(approval: dict) -> dict:
    """What the waiting agent is told. Deliberately not the arguments or the
    reviewer's note - the agent supplied the arguments and does not need the
    internal deliberation echoed back."""
    return {
        "approval_id": approval.get("approval_id"),
        "status": approval.get("status"),
        "tool_id": approval.get("tool_id"),
        "requested_at": approval.get("requested_at"),
        "expires_at": approval.get("expires_at"),
        "decided_at": approval.get("decided_at"),
        "usable_until": approval.get("usable_until"),
    }
