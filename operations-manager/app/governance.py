"""
Rate limits, budgets and timeouts for the gateway.

Everything else in this appliance decides whether a call is *allowed*.
This module decides whether it is *affordable* - a distinction that only
matters once agents are real, at which point it matters a great deal. An
agent stuck in a retry loop is not doing anything unauthorized: every call
passes RBAC, every tool is trusted, every completion is priced correctly.
It is simply doing it four hundred times a minute, against a downstream MCP
server that has no idea it is being hammered, at whatever a provider
charges per million tokens.

Four independent controls, because they fail in different ways:

  - **Request rate** - a per-caller ceiling on calls per minute across the
    whole gateway. Protects this service and, transitively, everything
    behind it.
  - **Tool-call rate and per-run ceiling** - a caller may burn its request
    budget on discovery without touching a downstream server, so tool
    invocation gets its own limit, plus a ceiling on how many tool calls a
    single traced run may make. A run that needs forty tool calls is a
    runaway loop in almost every real agent.
  - **Token and spend budgets** - a rolling hourly token budget and a daily
    spend ceiling per caller. The appliance already prices every completion
    to the cent for the savings dashboard; this is that same number used as
    a control rather than a report.
  - **Timeouts** - an upper bound on how long a downstream MCP call or a
    provider call may take. Without one, a hung downstream holds a
    connection here open indefinitely.

Three scopes, all checked on every call, because "who is over the line"
has three different answers:

  - **Per caller** - the ceilings one API key / agent identity may reach.
    The defaults apply to everyone; an override for a role or for one agent
    replaces only the fields it sets (an agent override wins over a role
    override, which wins over the defaults).
  - **Per role, shared** - one budget that every caller in a role draws
    from together: "the support agents may spend $50 a day between them".
  - **Global** - one ceiling for the whole gateway, all callers combined.
    The backstop that holds even when every individual caller is inside
    its own limit.

A call is refused when any scope that applies to it is exhausted.

Counting is done with Couchbase KV atomic counters (see
`CouchbaseStore.incr_counter`) rather than an in-process dictionary: the
counter document carries the window as its expiry, so the window rolls
itself with no sweeper, and the limit stays correct across a restart or a
second replica of this service. That is a sub-millisecond distributed token
bucket with nothing extra in the deployment.

`enforce` is a first-class setting. Turning limits on in front of live
agents without knowing what they currently consume is how a governance
feature gets switched back off permanently, so the honest default is to
measure first: with `enforce` false every limit is evaluated and recorded
exactly as it would be, and nothing is blocked.
"""
import hashlib
import time

# Window lengths, in seconds, for each counter family.
WINDOW_SECONDS = {
    "requests": 60,
    "tool_calls": 60,
    "tokens": 3600,
    "spend": 86400,
}

# KV counters hold integers, and a spend budget is dollars. Counting in
# millionths of a dollar keeps a per-call cost of $0.000004 from rounding
# away to nothing across a few hundred thousand calls, which is exactly
# the traffic shape a budget exists to bound.
SPEND_SCALE = 1_000_000


def to_spend_units(usd: float) -> int:
    try:
        return max(0, int(round(float(usd) * SPEND_SCALE)))
    except (TypeError, ValueError):
        return 0


def from_spend_units(units: float) -> float:
    return round(float(units or 0) / SPEND_SCALE, 6)


LIMIT_LABELS = {
    "requests": "request rate",
    "tool_calls": "tool-call rate",
    "tool_calls_per_run": "tool calls in one run",
    "tokens": "token budget",
    "spend": "spend budget",
}

# Which config field holds the ceiling for each counter family.
FAMILY_FIELDS = {
    "requests": "requests_per_minute",
    "tool_calls": "tool_calls_per_minute",
    "tool_calls_per_run": "tool_calls_per_run",
    "tokens": "tokens_per_hour",
    "spend": "spend_per_day_usd",
}
# Everything a per-caller override may set, and everything a shared scope
# (a role's total, the global total) may set. A run belongs to one caller,
# so the per-run ceiling has no shared form.
CALLER_LIMIT_FIELDS = tuple(FAMILY_FIELDS.values())
SHARED_LIMIT_FIELDS = tuple(f for f in CALLER_LIMIT_FIELDS if f != "tool_calls_per_run")
OVERRIDE_MATCH_TYPES = ("role", "agent")
MAX_OVERRIDES = 200

# Counter subjects for the shared scopes. A caller subject is a key prefix
# or an "anon:" address, so the "@" keeps these from ever colliding with one.
GLOBAL_SUBJECT = "@global"


def role_subject(role: str) -> str:
    return f"@role:{role}"


DEFAULT_GOVERNANCE_CONFIG = {
    "enabled": True,
    # False = evaluate and record, never block. See the module docstring.
    "enforce": False,
    # Per-caller ceilings. 0 disables that individual limit.
    "requests_per_minute": 240,
    "tool_calls_per_minute": 120,
    "tool_calls_per_run": 25,
    "tokens_per_hour": 500_000,
    "spend_per_day_usd": 25.0,
    # Global ceilings: all callers combined. 0 disables each one, and all
    # are off by default - a gateway-wide ceiling set without knowing the
    # fleet's total traffic refuses everyone at once.
    "global_limits": {field: 0 for field in SHARED_LIMIT_FIELDS},
    # Per-role and per-agent limits - see normalize_override.
    "overrides": [],
    # Roles that bypass every limit. Kept deliberately empty by default -
    # an exemption should be a decision someone made, not a default they
    # inherited.
    "exempt_roles": [],
    # Upper bound on one downstream MCP round-trip and one provider call.
    "downstream_timeout_seconds": 30,
    "llm_timeout_seconds": 60,
    # Upper bound on one whole request through this service, whatever it is
    # doing. The two timeouts above bound the calls this service makes; this
    # one bounds the call somebody makes to it, so a request that stalls
    # somewhere neither of those covers still ends.
    "request_timeout_seconds": 120,
}

_INT_FIELDS = (
    "requests_per_minute",
    "tool_calls_per_minute",
    "tool_calls_per_run",
    "tokens_per_hour",
    "downstream_timeout_seconds",
    "llm_timeout_seconds",
    "request_timeout_seconds",
)

# Timeouts are the one family where zero is not "unlimited" but "instantly
# fails", so they get a floor rather than an off switch.
_MIN_VALUES = {"downstream_timeout_seconds": 1, "llm_timeout_seconds": 1, "request_timeout_seconds": 5}
_MAX_VALUES = {"downstream_timeout_seconds": 600, "llm_timeout_seconds": 900, "request_timeout_seconds": 1800}


def _limit_value(field: str, value):
    """One ceiling coerced to its type, or None when it is not a number."""
    try:
        if field == "spend_per_day_usd":
            return max(0.0, round(float(value), 4))
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def normalize_limits(raw, fields: tuple, *, sparse: bool, keep_zero: bool = True) -> dict:
    """A set of ceilings limited to `fields`. `sparse` keeps only the ones
    supplied (an override); otherwise every field is present and a missing
    one is 0. `keep_zero` False drops zeros, for scopes where 0 just means
    "no ceiling here"."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for field in fields:
        value = _limit_value(field, raw.get(field)) if raw.get(field) not in (None, "") else None
        if value is None:
            if not sparse:
                out[field] = 0
            continue
        if value or keep_zero:
            out[field] = value
    return out


def _override_id(match_type: str, match_value: str) -> str:
    material = f"{match_type}:{match_value}:{time.time_ns()}"
    return "limit-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def normalize_override(raw: dict | None) -> dict | None:
    """One per-role or per-agent entry in its stored shape, or None when it
    names nobody or sets nothing.

    `limits` are per-caller ceilings and carry only the fields the entry
    sets - everything else keeps following the defaults, so a later change
    to the default token budget still reaches an agent whose override only
    raised its request rate. A 0 here is deliberate: "no ceiling on this
    for this caller". `shared_limits` exist for a role only: one total that
    every caller in the role counts against together."""
    raw = raw or {}
    match = raw.get("match") or {}
    match_type = str(match.get("type") or "").strip()
    match_value = str(match.get("value") or "").strip()[:200]
    if match_type not in OVERRIDE_MATCH_TYPES or not match_value:
        return None
    limits = normalize_limits(raw.get("limits"), CALLER_LIMIT_FIELDS, sparse=True)
    shared = {}
    if match_type == "role":
        shared = normalize_limits(raw.get("shared_limits"), SHARED_LIMIT_FIELDS, sparse=True, keep_zero=False)
    if not limits and not shared:
        return None
    return {
        "override_id": str(raw.get("override_id") or "").strip()[:64] or _override_id(match_type, match_value),
        "name": str(raw.get("name") or "").strip()[:120] or f"{match_type} {match_value}",
        "match": {"type": match_type, "value": match_value},
        "enabled": bool(raw.get("enabled", True)),
        "limits": limits,
        "shared_limits": shared,
    }


def normalize_overrides(raw) -> list[dict]:
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        entry = normalize_override(item if isinstance(item, dict) else None)
        if entry and entry["override_id"] not in seen:
            seen.add(entry["override_id"])
            out.append(entry)
    return out[:MAX_OVERRIDES]


def resolve(cfg: dict, role: str | None, agent_id: str | None) -> dict:
    """The ceilings that apply to one caller.

    Returns `caller` (every per-caller ceiling, resolved field by field:
    the agent's override, else the role's, else the default), `source`
    (which of those supplied each field), and `role_total` (the role's
    shared ceilings, empty when it has none). Within one match the first
    enabled entry in stored order applies."""
    def first(match_type, value):
        if not value:
            return None
        for entry in cfg.get("overrides") or []:
            if entry.get("enabled", True) and entry["match"]["type"] == match_type \
                    and entry["match"]["value"] == value:
                return entry
        return None

    role_entry, agent_entry = first("role", role), first("agent", agent_id)
    caller, source = {}, {}
    for field in CALLER_LIMIT_FIELDS:
        if agent_entry and field in agent_entry["limits"]:
            caller[field], source[field] = agent_entry["limits"][field], "agent"
        elif role_entry and field in role_entry["limits"]:
            caller[field], source[field] = role_entry["limits"][field], "role"
        else:
            caller[field], source[field] = cfg.get(field, 0), "default"
    return {
        "caller": caller,
        "source": source,
        "role_total": dict(role_entry["shared_limits"]) if role_entry else {},
    }


def normalize_config(cfg: dict | None) -> dict:
    """Re-validate every value server-side. The settings form is a
    convenience; this is the boundary - same convention as
    llm_cache.normalize_config."""
    merged = dict(DEFAULT_GOVERNANCE_CONFIG)
    for key, value in (cfg or {}).items():
        if key in merged:
            merged[key] = value

    merged["enabled"] = bool(merged["enabled"])
    merged["enforce"] = bool(merged["enforce"])

    for field in _INT_FIELDS:
        try:
            merged[field] = max(0, int(merged[field]))
        except (TypeError, ValueError):
            merged[field] = DEFAULT_GOVERNANCE_CONFIG[field]
        if field in _MIN_VALUES:
            merged[field] = max(_MIN_VALUES[field], merged[field])
        if field in _MAX_VALUES:
            merged[field] = min(_MAX_VALUES[field], merged[field])

    try:
        merged["spend_per_day_usd"] = max(0.0, round(float(merged["spend_per_day_usd"]), 4))
    except (TypeError, ValueError):
        merged["spend_per_day_usd"] = DEFAULT_GOVERNANCE_CONFIG["spend_per_day_usd"]

    roles = merged.get("exempt_roles") or []
    if not isinstance(roles, (list, tuple)):
        roles = []
    merged["exempt_roles"] = [str(r)[:64] for r in roles][:20]

    merged["global_limits"] = normalize_limits(merged.get("global_limits"), SHARED_LIMIT_FIELDS, sparse=False)
    merged["overrides"] = normalize_overrides(merged.get("overrides"))

    return merged


def public_config(cfg: dict) -> dict:
    """Nothing here is secret, so this is the config as stored - the
    function exists so the API surface stays symmetrical with the other
    settings modules, and so a secret added later has one obvious place to
    be stripped."""
    return dict(cfg)


# ---------------------------------------------------------------------------
# Window keys
# ---------------------------------------------------------------------------
# A counter key must identify the caller *and* the window instance, so that
# an expired window is a different document rather than a value someone has
# to reset. Deriving the window index from the clock means two replicas of
# this service agree on which window they are in without coordinating.

def window_index(family: str, now: float | None = None) -> int:
    seconds = WINDOW_SECONDS.get(family, 60)
    return int((now if now is not None else time.time()) // seconds)


def counter_key(family: str, subject: str, now: float | None = None) -> str:
    return f"rl::{family}::{subject or 'unknown'}::{window_index(family, now)}"


def anonymous_subject(client_host: str | None) -> str:
    """The bucket an unauthenticated caller counts against.

    The SDK and skill downloads carry no API key by design - an agent has to
    be able to fetch the client before it has been issued one - so there is
    no subject to key a limit on. Falling back to the client address means
    those routes are still bounded; it is a weaker identifier than an API
    key (a proxy collapses many callers into one bucket, and an attacker
    with addresses to spare can spread across several), which is why it is
    used only where no better one exists."""
    return f"anon:{(client_host or 'unknown')[:64]}"


def run_counter_key(family: str, trace_id: str) -> str:
    """Per-run counters are keyed by the run itself rather than a clock
    window - a run is the window."""
    return f"rl::{family}::run::{trace_id}"


def window_expiry_seconds(family: str) -> int:
    """Let a counter document outlive its window slightly, so a request
    landing microseconds before a boundary cannot read a document that has
    already been reclaimed."""
    return WINDOW_SECONDS.get(family, 60) + 30


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

def is_exempt(cfg: dict, role: str | None) -> bool:
    return bool(role) and role in (cfg.get("exempt_roles") or [])


def limit_for(cfg: dict, family: str) -> float:
    """The default per-caller ceiling for a family."""
    return cfg.get(FAMILY_FIELDS.get(family, ""), 0) or 0


def evaluate(
    cfg: dict, family: str, used: float, role: str | None, *,
    limit: float | None = None, scope: str = "caller", scope_name: str | None = None,
    source: str = "default",
) -> dict:
    """Decide what to do about a counter that has just been advanced to
    `used`. Always returns a full verdict, even when nothing is wrong, so
    callers can attach it to a trace span unconditionally.

    `exceeded` is the factual answer; `blocked` is the operational one -
    they differ exactly when limits are being measured rather than
    enforced, which is the whole point of `enforce`.
    """
    # `limit` is the resolved ceiling for the scope being judged (see
    # resolve); left out, the default per-caller ceiling applies. `scope` is
    # whose counter `used` came from - "caller", "role" (the role's shared
    # total) or "global" - and `source` says where a caller's ceiling came
    # from: the defaults, a role override or an agent override.
    if limit is None:
        limit = limit_for(cfg, family)
    who = {
        "role": f"shared by every caller in role '{scope_name}'",
        "global": "across all callers (global)",
    }.get(scope, "for this caller")
    verdict = {
        "scope": scope,
        "scope_name": scope_name,
        "source": source,
        "family": family,
        "label": LIMIT_LABELS.get(family, family),
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used) if limit else None,
        "exceeded": False,
        "blocked": False,
        "reason": None,
        "retry_after_seconds": None,
    }

    if not cfg.get("enabled") or not limit or is_exempt(cfg, role):
        return verdict

    if used <= limit:
        return verdict

    window = WINDOW_SECONDS.get(family)
    verdict["exceeded"] = True
    verdict["blocked"] = bool(cfg.get("enforce"))
    verdict["retry_after_seconds"] = (
        int(window - (time.time() % window)) if window else None
    )
    if family == "spend":
        verdict["reason"] = (
            f"daily spend budget exhausted {who}: ${used:.4f} used of ${limit:.2f} allowed"
        )
    elif family == "tokens":
        verdict["reason"] = (
            f"hourly token budget exhausted {who}: {int(used)} used of {int(limit)} allowed"
        )
    elif family == "tool_calls_per_run":
        verdict["reason"] = (
            f"this run has made {int(used)} tool call(s), over the per-run ceiling of {int(limit)} - "
            f"the shape of an agent stuck in a loop"
        )
    else:
        verdict["reason"] = (
            f"{verdict['label']} limit exceeded {who}: {int(used)} in the last "
            f"{window}s, over the limit of {int(limit)}"
        )
    return verdict


def blocking_verdict(verdicts: list[dict]) -> dict | None:
    """The first verdict that should actually stop the call, if any."""
    for verdict in verdicts:
        if verdict.get("blocked"):
            return verdict
    return None


def exceeded_verdicts(verdicts: list[dict]) -> list[dict]:
    return [v for v in verdicts if v.get("exceeded")]
