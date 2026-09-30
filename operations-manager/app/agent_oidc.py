"""
Federated agent identity: accepting a bearer JWT from the customer's own
identity provider.

API keys, however well managed, are a credential this appliance issues and
therefore a credential the customer has to manage separately from every
other machine identity they own. An enterprise that already runs Entra,
Okta or Keycloak wants an agent to authenticate the way its other
workloads do - a short-lived token from the IdP, obtained through the
client-credentials flow, validated here against the issuer's published
keys. That is what the MCP 2025-11-25 authorization extensions settled on
for headless clients, and it is what "federate agent identity" means in
practice.

Deliberately not an authorization server
----------------------------------------
This module validates tokens; it never issues them. Becoming an OAuth
authorization server would mean owning client registration, consent,
refresh, revocation endpoints and the standards obligations that come with
all of it - a large surface, for a capability the customer's IdP already
provides better. Validation is the part that has to happen here, and it is
the whole of what is here.

How a token becomes a role
--------------------------
The signature, issuer, audience and expiry are checked first, against keys
fetched from the issuer's JWKS endpoint and cached. Only then is a claim
read and mapped to one of this appliance's RBAC roles. The mapping is
explicit and configured - never "trust whatever string the token calls a
role" - because a claim value is chosen by the IdP's administrators and an
implicit mapping would let a group name created for something else quietly
become platform-admin here.

An unmapped claim is refused rather than defaulted, unless a default role
is explicitly configured. Silently falling back to a role is how a token
that should not have been accepted at all ends up with whatever the
default happens to grant.
"""
import logging
import threading
import time

logger = logging.getLogger("operations-manager.agent_oidc")

DEFAULT_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]

# Symmetric algorithms are refused outright: HS256 with a JWKS is the
# classic confusion attack, where an attacker signs a token using the
# public key as an HMAC secret. There is no legitimate reason for an IdP
# federation to use one.
FORBIDDEN_ALGORITHMS = {"HS256", "HS384", "HS512", "none"}

DEFAULT_OIDC_CONFIG = {
    "enabled": False,
    # The `iss` a token must carry, and where its signing keys live. Most
    # IdPs publish the JWKS URI in their discovery document; it is asked
    # for explicitly here rather than discovered, so nothing this service
    # trusts is fetched from a URL derived at runtime.
    "issuer": "",
    "jwks_uri": "",
    # Required. A token minted for another audience is a token minted for
    # somebody else's service.
    "audience": "",
    "algorithms": list(DEFAULT_ALGORITHMS),
    # Which claim carries the role, and how its values map onto RBAC roles.
    "role_claim": "roles",
    "role_map": {},
    # Used when the role claim is present but unmapped. Empty means refuse,
    # which is the safe reading.
    "default_role": "",
    # Which claim identifies the caller in the audit log.
    "subject_claim": "sub",
    # Tolerance for clock skew between this appliance and the IdP.
    "leeway_seconds": 60,
    "jwks_cache_seconds": 300,
}


def normalize_config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_OIDC_CONFIG)
    for key, value in (cfg or {}).items():
        if key in merged:
            merged[key] = value

    merged["enabled"] = bool(merged["enabled"])
    for field in ("issuer", "jwks_uri", "audience", "role_claim", "subject_claim", "default_role"):
        merged[field] = str(merged.get(field) or "").strip()[:500]
    merged["role_claim"] = merged["role_claim"] or "roles"
    merged["subject_claim"] = merged["subject_claim"] or "sub"

    algorithms = merged.get("algorithms") or []
    if not isinstance(algorithms, (list, tuple)):
        algorithms = []
    algorithms = [str(a).upper() for a in algorithms if str(a).upper() not in FORBIDDEN_ALGORITHMS]
    merged["algorithms"] = algorithms or list(DEFAULT_ALGORITHMS)

    role_map = merged.get("role_map") or {}
    if not isinstance(role_map, dict):
        role_map = {}
    merged["role_map"] = {str(k)[:200]: str(v)[:64] for k, v in list(role_map.items())[:100]}

    for field, floor, ceiling in (("leeway_seconds", 0, 600), ("jwks_cache_seconds", 30, 86400)):
        try:
            merged[field] = max(floor, min(ceiling, int(merged[field])))
        except (TypeError, ValueError):
            merged[field] = DEFAULT_OIDC_CONFIG[field]

    return merged


def config_problems(cfg: dict) -> list[str]:
    """What is missing before this configuration could work. Surfaced in
    the UI rather than discovered as a 401 at three in the morning."""
    problems = []
    if not cfg.get("enabled"):
        return problems
    if not cfg.get("issuer"):
        problems.append("issuer is required")
    if not cfg.get("jwks_uri"):
        problems.append("JWKS URI is required")
    if not cfg.get("audience"):
        problems.append(
            "audience is required - without it, a token minted for any other service by the same "
            "issuer would be accepted here"
        )
    if not cfg.get("role_map") and not cfg.get("default_role"):
        problems.append(
            "no role mapping and no default role - every valid token would be refused for having "
            "no role this appliance recognizes"
        )
    return problems


# ---------------------------------------------------------------------------
# JWKS client cache
# ---------------------------------------------------------------------------
# One client per JWKS URI, holding PyJWT's own key cache. Rebuilt when the
# URI changes so a config edit takes effect without a restart.
_clients: dict = {}
_clients_lock = threading.Lock()


def _client_for(cfg: dict):
    from jwt import PyJWKClient

    uri = cfg["jwks_uri"]
    with _clients_lock:
        entry = _clients.get(uri)
        if entry is None:
            entry = PyJWKClient(
                uri,
                cache_keys=True,
                lifespan=int(cfg.get("jwks_cache_seconds", 300)),
                timeout=10,
            )
            _clients[uri] = entry
        return entry


def reset_clients():
    """Drop cached JWKS clients - called when the configuration is saved,
    so a corrected URI is used immediately rather than after the old
    client's lifespan expires."""
    with _clients_lock:
        _clients.clear()


def looks_like_jwt(token: str) -> bool:
    """Cheap discriminator so an API key is never sent through JWT
    validation and a JWT is never hashed and looked up as a key. Three
    dot-separated segments, and the first decodes as a JOSE header."""
    if not token or token.count(".") != 2:
        return False
    return not token.startswith("aom_")


def map_role(claims: dict, cfg: dict) -> tuple[str | None, str | None]:
    """Turn a validated token's claims into an RBAC role.

    Returns (role, refusal_reason). Handles a claim that is a single string
    and one that is a list, because IdPs disagree about that and both are
    normal.
    """
    raw = claims.get(cfg["role_claim"])
    values = []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple)):
        values = [str(v) for v in raw]
    elif raw is not None:
        values = [str(raw)]

    role_map = cfg.get("role_map") or {}
    for value in values:
        if value in role_map:
            return role_map[value], None

    if cfg.get("default_role"):
        return cfg["default_role"], None

    if not values:
        return None, f"token carries no '{cfg['role_claim']}' claim and no default role is configured"
    return None, (
        f"none of the token's {cfg['role_claim']} values {values[:5]} map to a role, "
        f"and no default role is configured"
    )


def validate(token: str, cfg: dict, valid_roles: set) -> tuple[dict | None, str | None]:
    """Validate a bearer JWT and resolve it to an identity.

    Returns (identity, refusal_reason). Never raises for a bad token - an
    unparseable or hostile token is an authentication failure to be logged,
    not an exception to surface as a 500.
    """
    import jwt as pyjwt
    from jwt.exceptions import InvalidTokenError

    if not cfg.get("enabled"):
        return None, "JWT authentication is not enabled"
    problems = config_problems(cfg)
    if problems:
        return None, f"JWT authentication is misconfigured: {'; '.join(problems)}"

    try:
        signing_key = _client_for(cfg).get_signing_key_from_jwt(token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not resolve a signing key from the JWKS: %s", exc)
        return None, "could not resolve the token's signing key from the issuer's JWKS"

    try:
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=cfg["algorithms"],
            audience=cfg["audience"],
            issuer=cfg["issuer"],
            leeway=int(cfg.get("leeway_seconds", 60)),
            options={"require": ["exp", "iss", "aud"]},
        )
    except InvalidTokenError as exc:
        return None, f"token rejected: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unexpected error validating a JWT: %s", exc)
        return None, "token could not be validated"

    role, reason = map_role(claims, cfg)
    if not role:
        return None, reason
    if role not in valid_roles:
        return None, f"token maps to role '{role}', which this appliance does not define"

    subject = str(claims.get(cfg["subject_claim"]) or claims.get("sub") or "unknown")[:120]
    return {
        "role": role,
        "agent_id": None,
        "agent_name": f"{subject} (via {cfg['issuer']})",
        # A federated token carries no per-agent scope: what it may do is
        # the role it maps to. Narrowing that is the IdP's job, through
        # which claim it issues.
        "allowed_tools": [],
        "key_prefix": subject,
        "federated": True,
        "claims_subject": subject,
        "legacy": False,
    }, None
