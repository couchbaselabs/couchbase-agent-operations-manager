"""Test Connection for MCP servers: turn whatever goes wrong between this
gateway and a downstream MCP endpoint into a sentence an operator can act
on.

A registration that fails ingestion today surfaces as whatever exception
the MCP client raised - `ExceptionGroup`, `httpx.ConnectError`,
`McpError: Session terminated` - none of which tells the person at the
Servers page whether they typed the URL wrong, the server is down, the
credential is bad, or the thing on the other end is not an MCP server at
all. This module runs the connection in stages and names the stage that
failed:

  1. URL           - parses, http(s), has a host
  2. DNS           - the host resolves
  3. TCP/TLS       - a socket opens (and, for https, the handshake completes)
  4. Credential    - for OAuth, the token exchange itself succeeds
  5. HTTP          - the endpoint answers; 401/403/404/405 each get their own
                     explanation
  6. MCP           - initialize + tools/list succeed over Streamable HTTP

Nothing here is stored: it is called from POST /v1/servers/test-connection
with either a saved server's doc or the unsaved form, and from the Servers
page's per-row "Test" button.
"""
from __future__ import annotations

import asyncio
import socket
import ssl
import time
from urllib.parse import urlparse

import requests

from app import mcp_client, server_auth

PROBE_TIMEOUT_SECONDS = 12


def _step(name: str, ok: bool, detail: str, **extra) -> dict:
    return {"step": name, "ok": ok, "detail": detail, **extra}


# Shared with the invoke path so the probe and a failed invoke describe the
# same downstream failure the same way.
_exception_text = mcp_client.exception_text


async def probe(server_doc: dict, *, secret: str | None = None, timeout_seconds: int | None = None) -> dict:
    """Run every stage against `server_doc` (which may be an unsaved form
    assembled by the route). `secret` is a plaintext credential typed into
    the form; when None, the stored encrypted secret is used."""
    timeout = timeout_seconds or PROBE_TIMEOUT_SECONDS
    started = time.time()
    steps: list[dict] = []
    mcp_url = str(server_doc.get("mcp_url") or "").strip()
    platform = server_auth.normalize_platform(server_doc.get("platform"))
    auth = server_auth.normalize_auth(server_doc.get("auth"))

    def done(ok: bool, summary: str, suggestion: str | None = None, tools: list | None = None) -> dict:
        return {
            "ok": ok,
            "summary": summary,
            "suggestion": suggestion,
            "steps": steps,
            "tools": tools or [],
            "latency_ms": int((time.time() - started) * 1000),
            "platform": platform,
            "auth_mode": auth["mode"],
        }

    # 1. URL ----------------------------------------------------------------
    parsed = urlparse(mcp_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        steps.append(_step("url", False, f"'{mcp_url or '(empty)'}' is not an absolute http(s) URL"))
        return done(False, "The MCP URL is not valid.", "Enter the full endpoint, e.g. https://host:443/path/mcp.")
    platform_issue = server_auth.validate_platform_url(platform, mcp_url)
    steps.append(_step("url", True, f"{parsed.scheme}://{parsed.hostname}{':' + str(parsed.port) if parsed.port else ''}{parsed.path or '/'}",
                       warning=platform_issue))

    # 2. DNS ----------------------------------------------------------------
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, port, 0, socket.SOCK_STREAM)
        addrs = sorted({i[4][0] for i in infos})
        steps.append(_step("dns", True, f"{parsed.hostname} resolves to {', '.join(addrs[:3])}"))
    except socket.gaierror as exc:
        steps.append(_step("dns", False, f"{parsed.hostname} does not resolve ({exc.strerror or exc})"))
        return done(
            False, f"The host name '{parsed.hostname}' could not be resolved.",
            "Check for a typo in the host, or that this container's DNS can see it (a Compose service name only "
            "resolves inside the same Compose network; a cluster-internal name needs the full service DNS).",
        )

    # 3. TCP / TLS ----------------------------------------------------------
    def _connect():
        sock = socket.create_connection((parsed.hostname, port), timeout=min(timeout, 8))
        try:
            if parsed.scheme == "https":
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=parsed.hostname) as tls:
                    cert = tls.getpeercert() or {}
                    return {"tls": tls.version(), "subject": dict(x[0] for x in cert.get("subject", ()) if x)}
            return {}
        finally:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    try:
        tls_info = await asyncio.to_thread(_connect)
        steps.append(_step("connect", True, f"TCP connection to port {port} opened"
                           + (f"; TLS {tls_info['tls']}" if tls_info.get("tls") else "")))
    except ssl.SSLCertVerificationError as exc:
        steps.append(_step("connect", False, f"TLS certificate verification failed: {exc.reason or exc}"))
        return done(
            False, "The server's TLS certificate is not trusted by this appliance.",
            "Install the issuing CA into the operations-manager container (see SECURITY_HARDENING.md), or use "
            "the certificate's real host name if it is a name mismatch.",
        )
    except ssl.SSLError as exc:
        steps.append(_step("connect", False, f"TLS handshake failed: {exc}"))
        return done(False, "The TLS handshake failed.",
                    "The port may be plain HTTP (try http://), or the server requires a client certificate.")
    except (ConnectionRefusedError, OSError) as exc:
        steps.append(_step("connect", False, f"could not open a TCP connection to {parsed.hostname}:{port} ({exc})"))
        return done(
            False, f"Nothing is listening on {parsed.hostname}:{port}, or a firewall is blocking it.",
            "Confirm the server is running and the port is right; from Docker Compose, use the service name "
            "and container port rather than localhost.",
        )

    # 4. Credential (OAuth exchange) -----------------------------------------
    headers: dict = {}
    plain_secret = secret if secret is not None and str(secret).strip() else server_auth._decrypt_secret(server_doc)  # noqa: SLF001
    if auth["mode"] == "oauth":
        try:
            token = await asyncio.to_thread(server_auth.fetch_oauth_token, auth, plain_secret)
            headers["Authorization"] = f"Bearer {token['access_token']}"
            ttl = int(token["expires_at"] - time.time())
            steps.append(_step("credential", True, f"OAuth client-credentials exchange succeeded (token valid ~{ttl}s)"))
        except server_auth.OAuthError as exc:
            steps.append(_step("credential", False, str(exc)))
            return done(False, "The OAuth token exchange failed.", str(exc))
    elif auth["mode"] != "none":
        if not plain_secret:
            steps.append(_step("credential", False, f"auth mode is '{auth['mode']}' but no credential is on file"))
            return done(False, "No credential is stored for this server.",
                        "Enter the token/password in the form (or save one) before testing.")
        tmp = dict(server_doc)
        tmp["auth_secret"] = server_auth.user_auth.encrypt_secret(plain_secret)
        headers.update(server_auth.credential_headers(tmp))
        steps.append(_step("credential", True, f"{auth['mode']} credential attached"))
    else:
        steps.append(_step("credential", True, "no credential configured (identity headers only)"))
    headers.update(server_auth.identity_headers(role="admin", subject="test-connection", tool_id=None))

    # 5. HTTP ----------------------------------------------------------------
    def _http():
        return requests.post(
            mcp_url,
            json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
            headers={**headers, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            timeout=min(timeout, 10),
            allow_redirects=False,
        )

    try:
        resp = await asyncio.to_thread(_http)
    except requests.exceptions.RequestException as exc:
        steps.append(_step("http", False, f"HTTP request failed: {exc.__class__.__name__}: {exc}"))
        return done(False, "The endpoint accepted a connection but the HTTP request failed.",
                    "A proxy or load balancer in front of the server may be closing the request.")
    status = resp.status_code
    www = resp.headers.get("WWW-Authenticate", "")
    if status in (401, 407):
        steps.append(_step("http", False, f"HTTP {status}{' - ' + www if www else ''}"))
        if auth["mode"] == "none":
            return done(False, "The server requires authentication and none is configured.",
                        f"Choose an auth mode{' - the WWW-Authenticate header says: ' + www if www else ''}.")
        return done(False, "The server rejected the credential.",
                    "The token/password is wrong or expired, or (for OAuth) the token was issued for a different "
                    "audience/scope than this endpoint checks.")
    if status == 403:
        steps.append(_step("http", False, "HTTP 403 Forbidden"))
        return done(False, "The credential was accepted but is not allowed to use this endpoint.",
                    "Grant the service principal / role access to the MCP server (and, on Databricks/Snowflake, "
                    "to the underlying catalog, schema or functions).")
    if status == 404:
        steps.append(_step("http", False, "HTTP 404 Not Found"))
        return done(False, "Nothing is served at that path.",
                    "The host is right but the path is not - most servers mount MCP at /mcp; check the URL hint "
                    "for this platform.")
    if status in (301, 302, 307, 308):
        loc = resp.headers.get("Location", "")
        steps.append(_step("http", False, f"HTTP {status} redirect to {loc or '(no Location)'}"))
        return done(False, "The endpoint redirects instead of answering.",
                    f"Register the final URL directly{': ' + loc if loc else ''} (a redirect to a login page "
                    "usually means the endpoint is behind a browser SSO proxy, not an API gateway).")
    if status == 421:
        steps.append(_step("http", False, "HTTP 421 Misdirected Request"))
        return done(False, "The server rejected the Host header (DNS-rebinding protection).",
                    "FastMCP-based servers only accept localhost by default - run them with host=0.0.0.0 or disable "
                    "the Host check (see sample-mcp-servers/app/main.py for the setting).")
    if status >= 500:
        steps.append(_step("http", False, f"HTTP {status}"))
        return done(False, f"The server answered with an internal error (HTTP {status}).",
                    "Check the MCP server's own logs; the gateway side is fine up to this point.")
    ctype = resp.headers.get("Content-Type", "")
    if status == 405 or (status < 400 and "json" not in ctype and "event-stream" not in ctype):
        steps.append(_step("http", False, f"HTTP {status}, Content-Type '{ctype or 'none'}'"))
        return done(False, "The endpoint answered, but not like an MCP Streamable HTTP server.",
                    "A 405 or an HTML/plain-text reply usually means this is a web page, an SSE-only legacy MCP "
                    "endpoint, or the wrong path. Streamable HTTP endpoints accept POST and answer JSON or SSE.")
    steps.append(_step("http", True, f"HTTP {status}, Content-Type '{ctype}'"))

    # 6. MCP ----------------------------------------------------------------
    try:
        tools = await asyncio.wait_for(mcp_client.list_tools(mcp_url, headers=headers, timeout_seconds=timeout), timeout + 2)
    except asyncio.TimeoutError:
        steps.append(_step("mcp", False, f"MCP initialize/tools-list did not finish within {timeout}s"))
        return done(False, "The MCP handshake hung.",
                    "The server accepted the request but never completed initialize - often an SSE stream that is "
                    "buffered by a proxy. Raise the downstream timeout in Limits & Budgets if the server is just slow.")
    except Exception as exc:  # noqa: BLE001
        text = _exception_text(exc)
        steps.append(_step("mcp", False, text))
        lowered = text.lower()
        if "session terminated" in lowered or "invalid session" in lowered:
            return done(False, "The MCP session was terminated by the server during initialize.",
                        "The server did not accept this client's protocol version, or a proxy dropped the "
                        "Mcp-Session-Id header between requests.")
        if "unauthorized" in lowered or "401" in lowered:
            return done(False, "The server rejected the credential during the MCP handshake.",
                        "Check the credential; for OAuth, confirm the scope grants MCP access.")
        return done(False, "The MCP handshake failed.", text)
    names = [t["name"] for t in tools]
    steps.append(_step("mcp", True, f"initialize + tools/list succeeded: {len(tools)} tool(s)"))
    return done(
        True,
        f"Connected. The server reports {len(tools)} tool(s)" + (f": {', '.join(names[:8])}" + (" …" if len(names) > 8 else "") if names else "."),
        (platform_issue and f"Note: {platform_issue}") or None,
        tools=[{"name": t["name"], "description": (t.get("description") or "")[:160]} for t in tools],
    )
