"""
Downstream authentication: what a registered MCP server sees when this
gateway calls it on a caller's behalf.

Without this, every downstream MCP server behind the gateway sees exactly
one client - the appliance - and nothing about who actually asked. That
breaks least privilege at the last hop: an operations manager can decide
that only `finance_analyst` may reach `snowflake::run_query`, but Snowflake
itself then has no way to apply its own controls, log the real principal,
or refuse a request this gateway would have allowed. It also means a
downstream server has no credential to check at all, so anything that can
reach its URL can call it directly and bypass every decision made here.

Two mechanisms, deliberately separate:

  1. **A per-server credential.** What the *server* uses to authenticate
     *this gateway*: a bearer token, an API key in a named header, or HTTP
     basic. Stored encrypted at rest with the same Fernet key derived from
     AUTH_SECRET_KEY that protects the LDAP bind password and SIEM secrets
     (see user_auth.encrypt_secret), and never returned by any API - the
     Servers page shows only whether one is configured.

  2. **Caller identity headers.** What the *server* learns about *who* the
     call is for: the role, the masked subject label, and the trace ID, so
     a downstream audit log can be correlated with this one. These are
     signed with the same secret so a downstream server can verify they
     came from this gateway rather than from anyone who happened to guess
     the header names - an unsigned identity header is a suggestion, not an
     assertion.

The signature covers the identity claims and a timestamp, so a captured
header set cannot be replayed indefinitely against a downstream server that
checks it.
"""
import hashlib
import hmac
import time

from app import user_auth
from config import AUTH_SECRET_KEY

AUTH_MODES = ("none", "bearer", "header", "basic")

DEFAULT_AUTH = {
    "mode": "none",
    # Header name for mode="header" (e.g. "X-Api-Key"). Ignored otherwise.
    "header_name": "X-Api-Key",
    # Username for mode="basic". The password/token half lives in the
    # encrypted secret, never here.
    "username": "",
}

# Identity headers this gateway attaches to every downstream call.
IDENTITY_HEADERS = {
    "role": "X-AOM-Caller-Role",
    "subject": "X-AOM-Caller-Subject",
    "trace_id": "X-AOM-Trace-Id",
    "tool_id": "X-AOM-Tool-Id",
    "issued_at": "X-AOM-Issued-At",
    "signature": "X-AOM-Signature",
    "gateway": "X-AOM-Gateway",
}

# How long a signed identity header set stays valid. Long enough to survive
# a slow downstream, short enough that a captured header set is not a
# durable credential.
IDENTITY_TTL_SECONDS = 300


def normalize_auth(cfg: dict | None) -> dict:
    """Re-validate the non-secret half of a server's auth config."""
    merged = dict(DEFAULT_AUTH)
    for key, value in (cfg or {}).items():
        if key in merged:
            merged[key] = value

    mode = str(merged.get("mode") or "none").strip().lower()
    merged["mode"] = mode if mode in AUTH_MODES else "none"

    header_name = str(merged.get("header_name") or "").strip()
    # Header names go straight into an outbound HTTP request, so anything
    # that is not a valid token character is dropped rather than escaped.
    header_name = "".join(ch for ch in header_name if ch.isalnum() or ch in "-_")[:64]
    merged["header_name"] = header_name or DEFAULT_AUTH["header_name"]

    merged["username"] = str(merged.get("username") or "").strip()[:200]
    return merged


def public_auth(server_doc: dict | None) -> dict:
    """The auth config as the dashboard is allowed to see it: the mode and
    its non-secret fields, plus whether a secret is on file. The secret
    itself is never returned by any route."""
    auth = normalize_auth((server_doc or {}).get("auth"))
    return {
        **auth,
        "secret_configured": bool((server_doc or {}).get("auth_secret")),
    }


def store_secret(server_doc: dict, secret: str | None) -> dict:
    """Attach an encrypted downstream secret to a server document. A None
    or empty secret leaves whatever is already stored untouched, so saving
    the Servers form without re-typing the credential does not silently
    erase it - the same convention the LDAP bind password uses. Mutates and
    returns `server_doc`."""
    if secret is None or not str(secret).strip():
        return server_doc
    server_doc["auth_secret"] = user_auth.encrypt_secret(str(secret))
    return server_doc


def clear_secret(server_doc: dict) -> dict:
    server_doc.pop("auth_secret", None)
    return server_doc


def _decrypt_secret(server_doc: dict) -> str:
    encrypted = (server_doc or {}).get("auth_secret")
    if not encrypted:
        return ""
    try:
        return user_auth.decrypt_secret(encrypted)
    except Exception:  # noqa: BLE001
        # A secret that will not decrypt almost always means AUTH_SECRET_KEY
        # was rotated. Failing the call loudly would take down every
        # downstream tool at once; returning empty lets the downstream
        # server reject the call on its own terms, which is both more
        # honest and easier to diagnose from its logs.
        return ""


def credential_headers(server_doc: dict) -> dict:
    """The headers that authenticate *this gateway* to one downstream
    server."""
    auth = normalize_auth((server_doc or {}).get("auth"))
    secret = _decrypt_secret(server_doc)
    if auth["mode"] == "none" or not secret:
        return {}

    if auth["mode"] == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if auth["mode"] == "header":
        return {auth["header_name"]: secret}
    if auth["mode"] == "basic":
        import base64

        raw = f"{auth['username']}:{secret}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    return {}


def _signing_key() -> bytes:
    return hashlib.sha256(f"aom-downstream-identity::{AUTH_SECRET_KEY}".encode("utf-8")).digest()


def sign_identity(claims: dict) -> str:
    """HMAC over the identity claims, in a fixed field order so the
    signature a downstream server recomputes matches byte for byte."""
    material = "|".join(
        f"{field}={claims.get(field) or ''}"
        for field in ("role", "subject", "trace_id", "tool_id", "issued_at")
    )
    return hmac.new(_signing_key(), material.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_identity(headers: dict) -> bool:
    """The verification a downstream MCP server would run. Not used by the
    gateway itself - it lives here so the bundled sample servers, and
    anyone writing a real one, have the exact counterpart to `sign_identity`
    rather than having to reverse-engineer it from the header names."""
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}

    def _get(field: str):
        return lowered.get(IDENTITY_HEADERS[field].lower())

    issued_at = _get("issued_at")
    signature = _get("signature")
    if not issued_at or not signature:
        return False
    try:
        if abs(time.time() - float(issued_at)) > IDENTITY_TTL_SECONDS:
            return False
    except (TypeError, ValueError):
        return False

    expected = sign_identity({
        "role": _get("role"),
        "subject": _get("subject"),
        "trace_id": _get("trace_id"),
        "tool_id": _get("tool_id"),
        "issued_at": issued_at,
    })
    return hmac.compare_digest(expected, str(signature))


def identity_headers(
    *,
    role: str | None,
    subject: str | None,
    trace_id: str | None = None,
    tool_id: str | None = None,
    appliance_name: str = "couchbase-agent-operations-manager",
) -> dict:
    """The signed assertion of who this call is for. Sent on every
    downstream call, including ones with no credential configured - a
    server that wants to apply its own per-principal policy needs the
    principal whether or not it also needs a secret from us."""
    claims = {
        "role": role or "",
        "subject": subject or "",
        "trace_id": trace_id or "",
        "tool_id": tool_id or "",
        "issued_at": str(int(time.time())),
    }
    return {
        IDENTITY_HEADERS["role"]: claims["role"],
        IDENTITY_HEADERS["subject"]: claims["subject"],
        IDENTITY_HEADERS["trace_id"]: claims["trace_id"],
        IDENTITY_HEADERS["tool_id"]: claims["tool_id"],
        IDENTITY_HEADERS["issued_at"]: claims["issued_at"],
        IDENTITY_HEADERS["signature"]: sign_identity(claims),
        IDENTITY_HEADERS["gateway"]: appliance_name,
    }


def outbound_headers(
    server_doc: dict,
    *,
    role: str | None,
    subject: str | None,
    trace_id: str | None = None,
    tool_id: str | None = None,
) -> dict:
    """Everything this gateway attaches to one downstream MCP request:
    the server's own credential, plus the signed caller identity."""
    headers = dict(identity_headers(role=role, subject=subject, trace_id=trace_id, tool_id=tool_id))
    headers.update(credential_headers(server_doc))
    return headers
