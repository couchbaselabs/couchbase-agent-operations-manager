"""
The seed RBAC policy for the Couchbase Agent Operations Manager.

This module is a *bootstrap*, not the runtime source of truth: it seeds
`ROLES`, three sample trusted servers, and their per-tool policy into
Couchbase on first boot (only if those documents don't already exist), so
the appliance is immediately testable. From then on, Couchbase's `servers`
and `tools` collections are authoritative - new servers registered through
the Servers page or `POST /v1/servers`, and roles assigned to them there,
persist independently of this file and survive a redeploy of this image.

ROLES is seeded from BUILTIN_ROLES below, and from then on is managed by
admins through the Roles & RBAC page (POST/PUT/DELETE /v1/roles). Custom
roles are stored in Couchbase at settings::roles (config.ROLES_SETTINGS_DOC)
and merged *into this same dict in place* at startup (see apply_stored_roles)
so every module that imported ROLES - rag_apps, agent_oidc, the governance
policy, normalize_tool_policies below - sees additions without a restart.
The built-in roles can have their description edited but cannot be
deleted: the seeded tool policy, demo agents and sample servers reference
them, and `admin` in particular is what deny-by-default falls back to.

SEED_SERVERS is the bundled sample MCP servers used for out-of-the-box
evaluation - jira/zendesk/snowflake (well-behaved), plus two fixtures for
MCP Tool Hijacking detection (see app/hijack_detection.py):
`docs-search`, whose one tool's *description* carries a metadata-poisoning
payload, and `web-search`, whose tool description is clean but whose
*response* carries one instead. Both are registered/trusted like any other
seed server - docs-search::search_docs gets auto-quarantined the moment
it's ingested (metadata poisoning is caught before a tool is ever
discoverable), while web-search::fetch_page ingests and invokes normally,
so its response only gets flagged live, the first time something actually
calls it - use it from the Agent Tool Audit to see a hijack finding appear in
real time. The bundled shadow-diagnostics server is a third, separate
fixture, intentionally left out of this map the same way any unreviewed,
unregistered MCP server would be: it never gets its tools ingested at all,
so it can never be discovered or invoked through this operations manager no
matter how well a query matches it semantically.
"""

import re

BUILTIN_ROLES = {
    "support_agent": "Customer Support Agent - handles tickets and related issue tracking.",
    "finance_analyst": "Finance Analyst - runs read-only analytics against the data warehouse.",
    "admin": "Platform Administrator - full access to every trusted tool, including admin actions.",
}

# The live role registry. Mutated in place only - never rebound - because
# main.py and friends hold a reference to this exact object.
ROLES: dict[str, str] = dict(BUILTIN_ROLES)

ROLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
MAX_ROLE_DESCRIPTION_CHARS = 300


def is_builtin_role(role_id: str) -> bool:
    return role_id in BUILTIN_ROLES


def normalize_role(role_id: str, description: str | None) -> tuple[str, str]:
    """Validate one role as submitted through the Roles & RBAC page. Role
    IDs become label values in Prometheus metrics and keys in every tool's
    allowed_roles list, so the character set is deliberately narrow and the
    ID is immutable once created (editing renames nothing - it only changes
    the description)."""
    rid = str(role_id or "").strip().lower()
    if not ROLE_ID_PATTERN.match(rid):
        raise ValueError(
            "role id must start with a letter and contain only lowercase letters, digits and underscores (2-48 chars)"
        )
    desc = " ".join(str(description or "").split())[:MAX_ROLE_DESCRIPTION_CHARS]
    if not desc:
        raise ValueError("a role needs a one-line description of who holds it and what it is for")
    return rid, desc


def apply_stored_roles(stored: dict | None) -> None:
    """Merge the settings::roles document into ROLES in place. Built-in
    roles always exist (their descriptions may be overridden); custom roles
    come entirely from the document; anything no longer in the document is
    dropped. Called at startup and after every change through the API."""
    custom = (stored or {}).get("roles") or {}
    merged = dict(BUILTIN_ROLES)
    for rid, desc in custom.items():
        try:
            rid, desc = normalize_role(rid, desc)
        except ValueError:
            continue
        merged[rid] = desc
    ROLES.clear()
    ROLES.update(merged)


def roles_document() -> dict:
    """The settings::roles payload: every role, including built-ins so an
    edited built-in description survives a restart."""
    return {"roles": dict(ROLES)}


def public_roles() -> list[dict]:
    return [
        {"id": rid, "description": desc, "builtin": is_builtin_role(rid)}
        for rid, desc in ROLES.items()
    ]

# server_id -> metadata describing a bundled sample MCP server, seeded once
# on first boot. Editable afterwards from the Servers page like any other
# registered server.
SEED_SERVERS = {
    "jira": {
        "label": "Jira (Issue Tracking)",
        "owner": "IT Platform Team",
        "mcp_path": "/jira/mcp",
    },
    "zendesk": {
        "label": "Zendesk (Customer Support)",
        "owner": "Support Engineering",
        "mcp_path": "/zendesk/mcp",
    },
    "snowflake": {
        "label": "Snowflake (Data Warehouse)",
        "owner": "Data Platform Team",
        "mcp_path": "/snowflake/mcp",
    },
    "docs-search": {
        "label": "Internal Docs Search",
        "owner": "Knowledge Platform Team",
        "mcp_path": "/docs-search/mcp",
    },
    "web-search": {
        "label": "Web Search",
        "owner": "Platform Team",
        "mcp_path": "/web-search/mcp",
    },
}

# "{server_id}::{tool_name}" -> {allowed_roles, risk_level}. Reviewable,
# code-owned policy for the bundled sample servers' tools.
TOOL_POLICY = {
    "jira::search_issues": {"allowed_roles": ["support_agent", "admin"], "risk_level": "low"},
    "jira::get_issue": {"allowed_roles": ["support_agent", "admin"], "risk_level": "low"},
    "jira::create_issue": {"allowed_roles": ["support_agent", "admin"], "risk_level": "medium"},
    "jira::add_comment": {"allowed_roles": ["support_agent", "admin"], "risk_level": "low"},
    "zendesk::search_tickets": {"allowed_roles": ["support_agent", "admin"], "risk_level": "low"},
    "zendesk::get_ticket": {"allowed_roles": ["support_agent", "admin"], "risk_level": "low"},
    "zendesk::update_ticket": {"allowed_roles": ["support_agent", "admin"], "risk_level": "medium"},
    "zendesk::escalate_ticket": {"allowed_roles": ["support_agent", "admin"], "risk_level": "medium"},
    "snowflake::query": {"allowed_roles": ["finance_analyst", "admin"], "risk_level": "low"},
    "snowflake::get_metrics": {"allowed_roles": ["finance_analyst", "admin"], "risk_level": "low"},
    "snowflake::list_tables": {"allowed_roles": ["finance_analyst", "admin"], "risk_level": "low"},
    "snowflake::manage_users": {"allowed_roles": ["admin"], "risk_level": "critical"},
    "snowflake::manage_warehouse": {"allowed_roles": ["admin"], "risk_level": "critical"},
    # docs-search::search_docs is intentionally NOT listed here even though
    # its description carries an injected payload - the point of this
    # fixture is that hijack detection quarantines it automatically at
    # ingest, overriding whatever RBAC policy it would otherwise get. If
    # you add a reviewed entry for it here, it still gets quarantined; only
    # releasing it from the Threat Detection page re-enables it.
    "web-search::fetch_page": {"allowed_roles": ["support_agent", "finance_analyst", "admin"], "risk_level": "low"},
}


# Risk levels a reviewed policy may assign ("unclassified" is reserved for
# the deny-by-default fallback below and can't be chosen explicitly).
REVIEWED_RISK_LEVELS = ("critical", "high", "medium", "low")


def normalize_tool_policies(raw: dict | None) -> dict:
    """Validate a registered server's per-tool policy map, as sent to
    POST /v1/servers as `tool_policies`:

        {"<tool_name>": {"allowed_roles": ["finance_analyst", "admin"],
                         "risk_level": "low"}, ...}

    Returns the cleaned map; raises ValueError with a readable reason on any
    unknown role, empty role list or unreviewed risk level, so registration
    fails loudly instead of silently falling back to admin-only."""
    out: dict = {}
    for tool_name, pol in (raw or {}).items():
        if not isinstance(tool_name, str) or not tool_name or not isinstance(pol, dict):
            raise ValueError(f"tool_policies entry {tool_name!r} must map a tool name to an object")
        roles = pol.get("allowed_roles")
        if not isinstance(roles, list) or not roles:
            raise ValueError(f"tool_policies[{tool_name!r}].allowed_roles must be a non-empty list")
        unknown = [r for r in roles if r not in ROLES]
        if unknown:
            raise ValueError(f"tool_policies[{tool_name!r}] has unknown role(s): {unknown}")
        risk = str(pol.get("risk_level", "")).lower()
        if risk not in REVIEWED_RISK_LEVELS:
            raise ValueError(f"tool_policies[{tool_name!r}].risk_level must be one of {list(REVIEWED_RISK_LEVELS)}")
        out[tool_name] = {"allowed_roles": list(dict.fromkeys(roles)), "risk_level": risk}
    return out


def policy_for(
    server_id: str,
    tool_name: str,
    default_allowed_roles: list | None = None,
    tool_policies: dict | None = None,
) -> dict:
    """Return the RBAC policy for one tool.

    Precedence:
      1. Code-reviewed overrides in TOOL_POLICY (bundled sample servers).
      2. The registered server's own reviewed per-tool policy
         (`tool_policies`, declared at registration via POST /v1/servers
         and validated by normalize_tool_policies) -- how a server owner
         classifies tools this appliance doesn't ship code for.
      3. The owning server's `default_allowed_roles` (still "unclassified":
         roles were granted wholesale, not reviewed per tool).
      4. Deny by default: a new tool showing up should never be silently
         open to everyone, so it lands admin-only with risk_level
         "unclassified" until someone reviews it - the Insights page flags
         these.
    """
    explicit = TOOL_POLICY.get(f"{server_id}::{tool_name}")
    if explicit:
        return explicit
    reviewed = (tool_policies or {}).get(tool_name)
    if reviewed:
        return {"allowed_roles": list(reviewed["allowed_roles"]), "risk_level": reviewed["risk_level"]}
    if default_allowed_roles:
        return {"allowed_roles": default_allowed_roles, "risk_level": "unclassified"}
    return {"allowed_roles": ["admin"], "risk_level": "unclassified"}
