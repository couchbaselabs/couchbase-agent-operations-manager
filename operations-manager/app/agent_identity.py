"""
Inbound agent identity: who is calling this gateway, and may they still.

The appliance authenticates *humans* with LDAP, lockouts, a password
policy, session expiry and encrypted secrets at rest. It authenticated
*agents* with three strings in a file. Every capability added since has
widened that asymmetry - an agent can now spend a token budget, hold a
high-risk call for human approval, and retrieve role-scoped knowledge
documents, all on a credential that could not be rotated, expired or
revoked without editing `.env` and restarting the service. A leaked agent
key was, in practice, permanent.

This module makes an agent a first-class record instead of a hash in a
table:

  - **A record.** Name, owner, role, when it was issued and by whom, when
    it was last used. An operator can answer "what is this key, and does
    anything still need it?" - which is the question that decides whether a
    suspicious key can be turned off.
  - **Rotation with an overlap.** Issuing a new key does not immediately
    break whatever holds the old one: the previous key keeps working for a
    grace window and then stops on its own, because rotation that causes an
    outage is rotation nobody performs. A key believed to be compromised is
    revoked instead, which takes effect on the next request.
  - **Expiry.** An agent can be issued with an end date, so a credential
    made for a pilot stops working when the pilot does rather than
    outliving everyone who remembers it exists.
  - **Scoping.** An agent may be restricted to a subset of its role's
    tools. Only ever a subset: the role remains the ceiling, and the tool's
    own `allowed_roles` check still runs afterwards, so a scope can narrow
    access and can never widen it.

Storage shape and why
---------------------
Authentication is the hottest path in the appliance - every discover,
invoke, completion, memory call and knowledge lookup starts here - so
resolution must stay a single KV get on a deterministic ID. That is why
the role and scope are denormalized onto the *key* document rather than
being read from the agent record: one get by key hash answers "who is
this, what may they do, and are they still allowed", with no join. The
cost is that revoking or re-scoping an agent has to write through to its
keys, which happens on an operator action rather than on a request.

Legacy documents (the `{role, label}` shape seeded before this module
existed) are still accepted and resolve to an unscoped, non-expiring
identity. An upgrade that silently invalidated every existing agent key
would be a worse failure than the gap this closes.
"""
import hashlib
import re
import secrets
import time

KEY_PREFIX = "aom_"
KEY_BYTES = 24

AGENT_STATUSES = ("active", "revoked")
KEY_STATUSES = ("active", "rotated", "revoked")

MAX_SCOPE_TOOLS = 200
DEFAULT_ROTATION_GRACE_SECONDS = 3600

# How long a revoked key's document is kept after it stops working.
# Deleting it outright would be marginally tidier and materially worse:
# a request with a deleted key reads as 'unrecognized', which during an
# incident is indistinguishable from someone probing with a guess. The
# tombstone lets the gateway say 'this was revoked' - the answer that
# tells an operator their revocation actually took hold - and expires on
# its own afterwards.
REVOCATION_TOMBSTONE_SECONDS = 30 * 24 * 3600


def hash_key(api_key: str) -> str:
    """Never store a raw key. Same function the store has always used, so
    documents written before this module resolve unchanged."""
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()


def generate_key() -> str:
    """A new agent key. Prefixed so it is recognizable in a log or a
    secret manager, and long enough that the hash is the only realistic way
    to attack it."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_BYTES)}"


def key_prefix(api_key: str) -> str:
    """The identifying fragment shown in the UI and used as the audit
    subject. Enough to tell two keys apart, useless for authenticating."""
    return f"{(api_key or '')[:8]}...{(api_key or '')[-4:]}"


def new_agent_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "agent"
    return f"{slug}-{secrets.token_hex(4)}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


def is_expired(expires_at: str | None, now: float | None = None) -> bool:
    if not expires_at:
        return False
    return (now if now is not None else time.time()) > _parse(expires_at)


def normalize_scope(allowed_tools, role_tools: set | None = None) -> list:
    """A scope is a list of tool IDs, capped and de-duplicated. An empty
    list means "everything the role allows" rather than "nothing" - the
    alternative reading would make an agent created without a scope
    useless, which is not what anyone means by leaving a field blank."""
    if not allowed_tools:
        return []
    if not isinstance(allowed_tools, (list, tuple)):
        return []
    tools = sorted({str(t)[:200] for t in allowed_tools if str(t).strip()})[:MAX_SCOPE_TOOLS]
    if role_tools is not None:
        # Belt and braces: the invoke path re-checks the tool's own
        # allowed_roles anyway, so a scope naming something outside the role
        # could not grant it - but storing such an entry would leave an
        # operator reading a scope that does not mean what it says.
        tools = [t for t in tools if t in role_tools]
    return tools


def build_agent(
    *,
    name: str,
    role: str,
    owner: str = "",
    description: str = "",
    allowed_tools: list | None = None,
    expires_at: str | None = None,
    created_by: str | None = None,
    seeded: bool = False,
) -> dict:
    return {
        "doc_type": "agent_identity",
        "agent_id": new_agent_id(name),
        "name": (name or "Unnamed agent")[:120],
        "owner": (owner or "")[:120],
        "description": (description or "")[:400],
        "role": role,
        "allowed_tools": normalize_scope(allowed_tools),
        "status": "active",
        "expires_at": expires_at,
        "created_at": now_iso(),
        "created_by": created_by,
        "last_used_at": None,
        "use_count": 0,
        # Marks the three demo identities the appliance seeds so it works on
        # first boot. Surfaced in the UI so nobody mistakes them for
        # credentials somebody deliberately issued.
        "seeded": bool(seeded),
        "keys": [],
    }


def build_key_doc(
    *,
    agent: dict,
    api_key: str,
    status: str = "active",
    expires_at: str | None = None,
    label: str = "",
) -> dict:
    """The document authentication actually reads.

    Role, scope and both statuses are denormalized here so resolution is
    one KV get. Everything on it is derived from the agent record, and
    `sync_keys` rewrites it whenever that record changes.
    """
    return {
        "doc_type": "agent_key",
        "agent_id": agent["agent_id"],
        "agent_name": agent.get("name"),
        "key_prefix": key_prefix(api_key),
        "role": agent["role"],
        "allowed_tools": agent.get("allowed_tools") or [],
        "status": status,
        "agent_status": agent.get("status", "active"),
        # The key's own expiry: an agent-level end date, or the end of a
        # rotation grace window, whichever applies.
        "expires_at": expires_at or agent.get("expires_at"),
        "label": (label or "")[:120],
        "created_at": now_iso(),
    }


def resolve(key_doc: dict | None, now: float | None = None) -> tuple[dict | None, str | None]:
    """Decide whether a key document authenticates its caller.

    Returns (identity, refusal_reason). Every refusal is a distinct string
    on purpose: "revoked", "expired" and "the agent was turned off" want
    different responses from whoever is reading the audit log, and
    collapsing them into "invalid key" is how a revocation gets mistaken
    for a typo.
    """
    if not key_doc:
        return None, "invalid or unrecognized API key"

    # Documents written before agent identities existed: a bare
    # {role, label}. Accepted, unscoped, and never expiring.
    if key_doc.get("doc_type") != "agent_key":
        role = key_doc.get("role")
        if not role:
            return None, "invalid or unrecognized API key"
        return {
            "role": role,
            "agent_id": None,
            "agent_name": key_doc.get("label") or "legacy key",
            "allowed_tools": [],
            "key_prefix": key_doc.get("label") or "legacy",
            "legacy": True,
        }, None

    if key_doc.get("status") == "revoked":
        return None, "this API key has been revoked"
    if key_doc.get("agent_status") == "revoked":
        return None, f"the agent '{key_doc.get('agent_name')}' has been revoked"
    if is_expired(key_doc.get("expires_at"), now):
        if key_doc.get("status") == "rotated":
            return None, "this API key was rotated and its grace period has passed"
        return None, "this API key has expired"

    return {
        "role": key_doc.get("role"),
        "agent_id": key_doc.get("agent_id"),
        "agent_name": key_doc.get("agent_name"),
        "allowed_tools": key_doc.get("allowed_tools") or [],
        "key_prefix": key_doc.get("key_prefix"),
        "rotated": key_doc.get("status") == "rotated",
        "legacy": False,
    }, None


def in_scope(identity: dict, tool_id: str) -> bool:
    """Whether this agent's scope permits a tool. An empty scope means the
    whole role, which is the common case."""
    scope = (identity or {}).get("allowed_tools") or []
    return True if not scope else tool_id in scope


def filter_to_scope(identity: dict, tools: list[dict]) -> list[dict]:
    """Narrow discovery results to the agent's scope. Applied *after* the
    RBAC + vector pre-filter rather than instead of it, so the scope can
    only ever remove candidates the role already allowed."""
    scope = (identity or {}).get("allowed_tools") or []
    if not scope:
        return tools
    return [t for t in tools if t.get("tool_id") in scope]


def rotation_expiry(grace_seconds: int, now: float | None = None) -> str:
    now = now if now is not None else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(0, int(grace_seconds))))


def public_agent(agent: dict) -> dict:
    """The agent record as the dashboard sees it. Key hashes never leave
    the service - a hash is not a credential, but publishing every hash an
    installation holds is a gift to anyone who later gets the audit log."""
    keys = []
    for key in agent.get("keys") or []:
        keys.append({
            "key_prefix": key.get("key_prefix"),
            "status": key.get("status"),
            "created_at": key.get("created_at"),
            "expires_at": key.get("expires_at"),
            "label": key.get("label"),
        })
    return {
        "agent_id": agent.get("agent_id"),
        "name": agent.get("name"),
        "owner": agent.get("owner"),
        "description": agent.get("description"),
        "role": agent.get("role"),
        "allowed_tools": agent.get("allowed_tools") or [],
        "status": agent.get("status"),
        "expires_at": agent.get("expires_at"),
        "created_at": agent.get("created_at"),
        "created_by": agent.get("created_by"),
        "last_used_at": agent.get("last_used_at"),
        "use_count": agent.get("use_count", 0),
        "seeded": bool(agent.get("seeded")),
        "expired": is_expired(agent.get("expires_at")),
        "keys": keys,
    }
