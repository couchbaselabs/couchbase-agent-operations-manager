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
     *this gateway*: a bearer token, an API key in a named header, HTTP
     basic, or an OAuth 2.0 access token obtained with the client-
     credentials grant from the server's own token endpoint (the mode
     Snowflake, Databricks and BigQuery MCP endpoints expect - see
     PLATFORM_PRESETS). Stored encrypted at rest with the same Fernet key derived from
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
import logging
import threading
import time
from urllib.parse import urlparse

import requests

from app import user_auth
from config import AUTH_SECRET_KEY

logger = logging.getLogger("operations-manager.server_auth")

AUTH_MODES = ("none", "bearer", "header", "basic", "oauth")

DEFAULT_AUTH = {
    "mode": "none",
    # Header name for mode="header" (e.g. "X-Api-Key"). Ignored otherwise.
    "header_name": "X-Api-Key",
    # Username for mode="basic", or the client_id for mode="oauth". The
    # password/token/client-secret half lives in the encrypted secret,
    # never here.
    "username": "",
    # mode="oauth" only: where to exchange client credentials for an access
    # token, the scopes to request, and an optional audience/resource
    # parameter some issuers (Databricks, Entra ID for Snowflake) want.
    "token_url": "",
    "scope": "",
    "audience": "",
}

# Which managed data platform a registered server fronts, if any. Purely
# advisory metadata plus URL validation and sensible auth defaults - the
# gateway talks plain Streamable HTTP MCP to all of them.
PLATFORM_PRESETS: dict[str, dict] = {
    "generic": {
        "label": "Generic MCP server",
        "url_hint": "https://host:port/path/mcp",
        "hosts": (),
        "auth_mode": "none",
        "notes": "Any MCP server that speaks Streamable HTTP.",
    },
    "snowflake": {
        "label": "Snowflake (Cortex Agents / managed MCP)",
        "url_hint": "https://<account>.snowflakecomputing.com/api/v2/databases/<db>/schemas/<schema>/mcp-servers/<name>",
        "hosts": ("snowflakecomputing.com", "snowflakecomputing.cn"),
        "auth_mode": "oauth",
        "token_url_hint": "https://<account>.snowflakecomputing.com/oauth/token-request",
        "scope_hint": "session:role:<ROLE>",
        "notes": "Snowflake-managed MCP servers accept an OAuth access token (Snowflake OAuth or an external IdP) "
                 "or a programmatic access token as a bearer token.",
    },
    "databricks": {
        "label": "Databricks (managed MCP: Genie, Vector Search, UC functions)",
        "url_hint": "https://<workspace>.cloud.databricks.com/api/2.0/mcp/<type>/<catalog>/<schema>",
        "hosts": ("cloud.databricks.com", "azuredatabricks.net", "gcp.databricks.com"),
        "auth_mode": "oauth",
        "token_url_hint": "https://<workspace>.cloud.databricks.com/oidc/v1/token",
        "scope_hint": "all-apis",
        "notes": "Use a service principal's OAuth M2M client ID/secret (recommended) or a personal access token as a "
                 "bearer token.",
    },
    "bigquery": {
        "label": "Google BigQuery (MCP Toolbox / Cloud Run)",
        "url_hint": "https://<toolbox-service>.run.app/mcp",
        "hosts": ("run.app", "googleapis.com", "cloudfunctions.net"),
        "auth_mode": "oauth",
        "token_url_hint": "https://oauth2.googleapis.com/token",
        "scope_hint": "https://www.googleapis.com/auth/bigquery.readonly",
        "notes": "A BigQuery MCP server (Google's MCP Toolbox for Databases, or the BigQuery remote MCP preview) behind "
                 "Cloud Run / IAP expects a Google OAuth access token; a short-lived ID token as a bearer token also "
                 "works for Cloud Run-authenticated services.",
    },
}

PLATFORMS = tuple(PLATFORM_PRESETS)


def public_platforms() -> list[dict]:
    return [{"id": pid, **{k: v for k, v in p.items() if k != "hosts"}, "hosts": list(p["hosts"])} for pid, p in PLATFORM_PRESETS.items()]


def normalize_platform(platform: str | None) -> str:
    p = str(platform or "generic").strip().lower()
    return p if p in PLATFORM_PRESETS else "generic"


def validate_platform_url(platform: str, mcp_url: str) -> str | None:
    """A readable reason the URL does not look like that platform's MCP
    endpoint, or None. Advisory: a customer fronting Snowflake through
    their own proxy is allowed to override by choosing 'generic'."""
    platform = normalize_platform(platform)
    preset = PLATFORM_PRESETS[platform]
    try:
        parsed = urlparse(mcp_url or "")
    except Exception:  # noqa: BLE001
        return "the MCP URL could not be parsed"
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "the MCP URL must be an absolute http(s) URL"
    if platform == "generic":
        return None
    if parsed.scheme != "https":
        return f"{preset['label']} endpoints are HTTPS only"
    host = (parsed.hostname or "").lower()
    if preset["hosts"] and not any(host == h or host.endswith("." + h) for h in preset["hosts"]):
        return (
            f"'{host}' is not a {preset['label'].split(' (')[0]} host (expected one under "
            f"{', '.join(preset['hosts'])}). Choose the generic platform if this endpoint is behind your own proxy."
        )
    if platform == "snowflake" and "/mcp" not in parsed.path:
        return "a Snowflake managed MCP endpoint path ends in /mcp-servers/<name> (see the URL hint)"
    if platform == "databricks" and not parsed.path.startswith("/api/2.0/mcp/"):
        return "a Databricks managed MCP endpoint path starts with /api/2.0/mcp/ (see the URL hint)"
    return None

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
    token_url = str(merged.get("token_url") or "").strip()[:500]
    merged["token_url"] = token_url if token_url.startswith(("http://", "https://")) else ""
    merged["scope"] = str(merged.get("scope") or "").strip()[:500]
    merged["audience"] = str(merged.get("audience") or "").strip()[:500]
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


# -- OAuth 2.0 client-credentials ----------------------------------------
#
# One access token per registered server, cached in-process until shortly
# before it expires. The exchange is a plain POST to the server's token
# endpoint (RFC 6749 §4.4); the client secret is the encrypted per-server
# secret, the client_id lives in auth.username. Cached by the server_id
# plus a hash of the inputs so changing the credential invalidates the
# token at once rather than at expiry.
_OAUTH_TIMEOUT_SECONDS = 15
_OAUTH_REFRESH_SKEW_SECONDS = 60
_oauth_tokens: dict[str, dict] = {}
_oauth_lock = threading.Lock()


class OAuthError(RuntimeError):
    """Token exchange failed - the message is already human-readable."""


def _oauth_cache_key(server_doc: dict, auth: dict, secret: str) -> str:
    material = "|".join([
        str((server_doc or {}).get("server_id") or ""),
        auth.get("token_url") or "", auth.get("username") or "", auth.get("scope") or "",
        auth.get("audience") or "", hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16],
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def fetch_oauth_token(auth: dict, secret: str) -> dict:
    """Perform the client-credentials exchange. Returns {access_token,
    expires_at}. Raises OAuthError with a message an operator can act on."""
    token_url = auth.get("token_url") or ""
    client_id = auth.get("username") or ""
    if not token_url:
        raise OAuthError("OAuth mode needs a token endpoint URL (the issuer's /oauth/token or /oidc/v1/token)")
    if not client_id:
        raise OAuthError("OAuth mode needs a client ID")
    if not secret:
        raise OAuthError("OAuth mode needs a client secret - none is stored for this server")
    form = {"grant_type": "client_credentials"}
    if auth.get("scope"):
        form["scope"] = auth["scope"]
    if auth.get("audience"):
        # Databricks and some IdPs call it `resource`; Auth0-style issuers
        # call it `audience`. Sending both is harmless.
        form["audience"] = auth["audience"]
        form["resource"] = auth["audience"]
    try:
        resp = requests.post(
            token_url, data=form, auth=(client_id, secret),
            headers={"Accept": "application/json"}, timeout=_OAUTH_TIMEOUT_SECONDS,
        )
    except requests.exceptions.SSLError as exc:
        raise OAuthError(f"TLS handshake with the token endpoint failed ({exc.__class__.__name__}): "
                         "check the certificate chain or the token URL") from exc
    except requests.exceptions.ConnectionError as exc:
        raise OAuthError(f"could not connect to the token endpoint {token_url}: DNS, firewall or the URL "
                         "itself is wrong") from exc
    except requests.exceptions.Timeout as exc:
        raise OAuthError(f"the token endpoint {token_url} did not answer within {_OAUTH_TIMEOUT_SECONDS}s") from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error_description") or body.get("error") or ""
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:200]
        if resp.status_code in (400, 401):
            raise OAuthError(f"the token endpoint rejected the client credentials (HTTP {resp.status_code}"
                             f"{': ' + detail if detail else ''}) - check the client ID, secret and scope")
        raise OAuthError(f"the token endpoint answered HTTP {resp.status_code}{': ' + detail if detail else ''}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthError("the token endpoint did not return JSON - is this really an OAuth token URL?") from exc
    token = body.get("access_token")
    if not token:
        raise OAuthError(f"the token endpoint returned no access_token (got keys: {', '.join(body.keys()) or 'none'})")
    try:
        expires_in = int(body.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return {"access_token": str(token), "expires_at": time.time() + max(30, expires_in)}


def oauth_access_token(server_doc: dict, auth: dict | None = None, secret: str | None = None) -> str:
    auth = auth or normalize_auth((server_doc or {}).get("auth"))
    secret = _decrypt_secret(server_doc) if secret is None else secret
    key = _oauth_cache_key(server_doc, auth, secret)
    with _oauth_lock:
        cached = _oauth_tokens.get(key)
        if cached and cached["expires_at"] - _OAUTH_REFRESH_SKEW_SECONDS > time.time():
            return cached["access_token"]
    fresh = fetch_oauth_token(auth, secret)
    fresh["server_id"] = str((server_doc or {}).get("server_id") or "")
    with _oauth_lock:
        _oauth_tokens[key] = fresh
    return fresh["access_token"]


def forget_oauth_token(server_doc: dict) -> None:
    sid = str((server_doc or {}).get("server_id") or "")
    with _oauth_lock:
        for key in [k for k, v in _oauth_tokens.items() if v.get("server_id") == sid]:
            _oauth_tokens.pop(key, None)


def credential_headers(server_doc: dict) -> dict:
    """The headers that authenticate *this gateway* to one downstream
    server. For OAuth this may perform a (cached) token exchange; a failed
    exchange is logged and the call goes out unauthenticated so the
    downstream server's own 401 is what surfaces - the Test Connection
    button is where the exchange error itself is reported."""
    auth = normalize_auth((server_doc or {}).get("auth"))
    secret = _decrypt_secret(server_doc)
    if auth["mode"] == "none" or not secret:
        return {}

    if auth["mode"] == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if auth["mode"] == "oauth":
        try:
            return {"Authorization": f"Bearer {oauth_access_token(server_doc, auth, secret)}"}
        except OAuthError as exc:
            logger.warning("OAuth token exchange failed for server '%s': %s", server_doc.get("server_id"), exc)
            return {}
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
