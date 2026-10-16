"""Warehouse syncs: a user-defined query against Snowflake, Databricks or
BigQuery, run on a schedule, loaded into the Knowledge Base.

A sync is a connection (vendor + credentials, encrypted at rest with the
same Fernet key as MCP server secrets and never returned by the API), a
SQL query, a target (knowledge set, roles, table name), a schedule and a
refresh mode. Every run:

  1. executes the query through the vendor's own HTTP API - no drivers in
     the image, and results arrive in pages/partitions/chunks so a 50 M
     row result is never held in memory:
       Snowflake   SQL REST API, /api/v2/statements - result partitions
       Databricks  Statement Execution API, /api/2.0/sql/statements with
                   EXTERNAL_LINKS - presigned result chunks
       BigQuery    jobs.query + jobs.getQueryResults - pageToken paging
  2. streams the rows into the ingestion spool as newline-delimited JSON
     and hands the file to the ingestion job engine (app/ingest_jobs.py,
     kind "sync") - the same streaming parse, batched embedding, progress,
     cancel and cleanup every other large load gets;
  3. when the new document is fully indexed, applies the refresh mode:
       replace   delete the previous run's document (the swap is atomic
                 from retrieval's side: a document is only visible once
                 complete, so agents see the old snapshot until the new
                 one is whole, never both and never neither)
       snapshot  keep the previous document and give it an expiry, so
                 "what did this table look like last month" is answerable.

Scheduling is a cron expression or an interval, persisted in the sync
document (`next_run_at`), checked every SCHEDULER_TICK_SECONDS by one
background loop. A missed window (process down) runs once at startup, not
once per missed tick; a sync whose previous run is still going is skipped
and the skip recorded. Only one run per sync at a time.

Authentication, per vendor (what the vendors recommend for services):
  Snowflake   key-pair JWT (RSA private key, the account's recommended
              path for programmatic access), or a programmatic access
              token / OAuth access token as a bearer.
  Databricks  service-principal OAuth M2M (client ID + secret, token
              exchange at /oidc/v1/token - the same client-credentials
              code app/server_auth.py uses for MCP servers), or a PAT.
  BigQuery    a service-account JSON key (google-auth, BigQuery read-only
              scope).

Everything here is synchronous `requests` code run in a worker thread by
the scheduler/route; nothing blocks the event loop.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Callable, Iterator

import requests

from app import user_auth
from app.log_forwarding import MASK, _f

logger = logging.getLogger("operations-manager.warehouse_sync")

SCHEDULER_TICK_SECONDS = 30
HTTP_TIMEOUT = (15, 120)
PREVIEW_ROWS = 10
MAX_RUN_HISTORY = 20
QUERY_MAX_CHARS = 20000
# How long one query may run (vendor-side) before the run is failed.
QUERY_TIMEOUT_SECONDS = 3600


class SyncError(RuntimeError):
    """A readable reason, meant for the operator."""


# ---------------------------------------------------------------------------
# Vendor catalog - the console renders the connection form from this
# ---------------------------------------------------------------------------
VENDORS: list[dict] = [
    {
        "id": "snowflake", "label": "Snowflake", "product": "Snowflake SQL REST API", "openSource": False,
        "transport": "POST /api/v2/statements, result partitions streamed",
        "description": "Runs the query on a Snowflake warehouse and streams the result partitions.",
        "fields": [
            _f("account", "Account identifier", "text", "", True, "abc12345.us-east-1",
               "The part before .snowflakecomputing.com (org-account or account locator with region)."),
            _f("user", "User", "text", "", True, "AOM_SVC", "The user the key pair or token belongs to."),
            _f("auth_method", "Authentication", "select", "keypair", True, "", None, ["keypair", "token"]),
            _f("private_key", "Private key (PEM)", "secret", "", False, "",
               "Key-pair auth: the RSA private key registered on the user (ALTER USER ... SET RSA_PUBLIC_KEY). PKCS#8 PEM; "
               "encrypted keys need the passphrase below."),
            _f("private_key_passphrase", "Private key passphrase", "secret", "", False, "", "Only for an encrypted private key."),
            _f("token", "Access token", "secret", "", False, "",
               "Token auth: a programmatic access token (PAT) or an OAuth access token, sent as a bearer."),
            _f("warehouse", "Warehouse", "text", "", True, "COMPUTE_WH", None),
            _f("role", "Role", "text", "", False, "ANALYST", "Optional."),
            _f("database", "Database", "text", "", False, "SALES", "Optional default for unqualified names in the query."),
            _f("schema", "Schema", "text", "", False, "PUBLIC", "Optional."),
        ],
    },
    {
        "id": "databricks", "label": "Databricks", "product": "Databricks SQL Statement Execution API", "openSource": False,
        "transport": "POST /api/2.0/sql/statements (EXTERNAL_LINKS), result chunks streamed",
        "description": "Runs the query on a SQL warehouse and streams the presigned result chunks.",
        "fields": [
            _f("host", "Workspace URL", "text", "", True, "https://adb-1234567890.12.azuredatabricks.net", None),
            _f("warehouse_id", "SQL warehouse ID", "text", "", True, "1234567890abcdef",
               "SQL Warehouses > the warehouse > Connection details."),
            _f("auth_method", "Authentication", "select", "oauth", True, "", None, ["oauth", "token"]),
            _f("client_id", "Service principal client ID", "text", "", False, "", "OAuth M2M."),
            _f("client_secret", "Service principal client secret", "secret", "", False, "", "OAuth M2M."),
            _f("token", "Personal access token", "secret", "", False, "", "Token auth."),
            _f("catalog", "Catalog", "text", "", False, "main", "Optional default for unqualified names."),
            _f("schema", "Schema", "text", "", False, "default", "Optional."),
        ],
    },
    {
        "id": "bigquery", "label": "BigQuery", "product": "BigQuery REST API (jobs.query)", "openSource": False,
        "transport": "jobs.query + getQueryResults, pages streamed",
        "description": "Runs the query as a BigQuery job and streams the result pages.",
        "fields": [
            _f("project_id", "Project ID", "text", "", True, "my-gcp-project", "Billing project for the query job."),
            _f("service_account_json", "Service-account JSON key", "secret", "", True, "",
               "Needs roles/bigquery.jobUser on the project and read access to the queried datasets."),
            _f("location", "Location", "text", "", False, "US", "Optional; the datasets' location (US, EU, us-central1...)."),
        ],
    },
]
VENDOR_BY_ID = {v["id"]: v for v in VENDORS}
MODES = ("replace", "snapshot")


def _secret_names(vendor_id: str) -> set[str]:
    return {f["name"] for f in VENDOR_BY_ID[vendor_id]["fields"] if f["input"] == "secret"}


# ---------------------------------------------------------------------------
# Sync documents
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_sync_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "sync"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def normalize_connection(vendor: str, incoming: dict | None, previous: dict | None = None) -> dict:
    """Encrypt secrets, keep stored ones when the form sends MASK/empty,
    validate selects. Returns the stored shape: plain fields by name,
    secrets under "<name>_encrypted"."""
    if vendor not in VENDOR_BY_ID:
        raise SyncError(f"unknown vendor '{vendor}'")
    incoming = incoming or {}
    previous = previous or {}
    out: dict = {}
    for f in VENDOR_BY_ID[vendor]["fields"]:
        name = f["name"]
        val = incoming.get(name)
        if f["input"] == "secret":
            enc = f"{name}_encrypted"
            if val is None or val == "" or val == MASK:
                if previous.get(enc):
                    out[enc] = previous[enc]
            else:
                out[enc] = user_auth.encrypt_secret(str(val))
            continue
        if val is None or val == "":
            val = previous.get(name, f["default"])
        if f["input"] == "select" and f["options"] and str(val) not in f["options"]:
            val = f["default"]
        out[name] = str(val).strip()[:2000]
    return out


def public_connection(vendor: str, stored: dict) -> dict:
    out = {}
    for f in VENDOR_BY_ID[vendor]["fields"]:
        if f["input"] == "secret":
            out[f["name"]] = MASK if stored.get(f"{f['name']}_encrypted") else ""
        else:
            out[f["name"]] = stored.get(f["name"], f["default"])
    return out


def _get(vendor: str, stored: dict, name: str) -> str:
    if name in _secret_names(vendor):
        enc = stored.get(f"{name}_encrypted")
        if not enc:
            return ""
        try:
            return user_auth.decrypt_secret(enc)
        except Exception:  # noqa: BLE001
            return ""
    return str(stored.get(name) or "")


def missing_connection_fields(vendor: str, stored: dict) -> list[str]:
    out = []
    for f in VENDOR_BY_ID[vendor]["fields"]:
        if f["required"] and not _get(vendor, stored, f["name"]).strip() and not f["default"]:
            out.append(f["label"])
    method = stored.get("auth_method")
    if vendor == "snowflake":
        if method == "keypair" and not _get(vendor, stored, "private_key"):
            out.append("Private key (PEM)")
        if method == "token" and not _get(vendor, stored, "token"):
            out.append("Access token")
    if vendor == "databricks":
        if method == "oauth" and not (_get(vendor, stored, "client_id") and _get(vendor, stored, "client_secret")):
            out.append("Service principal client ID and secret")
        if method == "token" and not _get(vendor, stored, "token"):
            out.append("Personal access token")
    return out


def normalize_schedule(raw: dict | None) -> dict:
    raw = raw or {}
    kind = str(raw.get("kind") or "interval").lower()
    if kind == "cron":
        expr = " ".join(str(raw.get("cron") or "").split())
        parse_cron(expr)  # raises on a bad expression
        return {"kind": "cron", "cron": expr}
    if kind == "manual":
        return {"kind": "manual"}
    try:
        minutes = int(raw.get("every_minutes") or 1440)
    except (TypeError, ValueError):
        minutes = 1440
    return {"kind": "interval", "every_minutes": max(5, min(minutes, 60 * 24 * 90))}


def normalize_sync(raw: dict, *, roles: set[str], existing: dict | None = None) -> dict:
    name = str(raw.get("name") or "").strip()[:120]
    if not name:
        raise SyncError("a sync needs a name")
    vendor = str(raw.get("vendor") or (existing or {}).get("vendor") or "").lower()
    if vendor not in VENDOR_BY_ID:
        raise SyncError("vendor must be snowflake, databricks or bigquery")
    if existing and existing.get("vendor") != vendor:
        raise SyncError("a sync's vendor cannot change - create a new sync")
    query = str(raw.get("query") or "").strip()
    if not query:
        raise SyncError("a sync needs a SQL query")
    if len(query) > QUERY_MAX_CHARS:
        raise SyncError(f"the query is longer than {QUERY_MAX_CHARS} characters")
    allowed = [r for r in dict.fromkeys(str(x).strip() for x in (raw.get("allowed_roles") or [])) if r]
    unknown = [r for r in allowed if r not in roles]
    if unknown:
        raise SyncError(f"unknown role(s): {unknown}")
    if not allowed:
        raise SyncError("pick at least one role that may retrieve the synced table")
    mode = str(raw.get("mode") or "replace").lower()
    if mode not in MODES:
        raise SyncError("mode must be 'replace' or 'snapshot'")
    try:
        snapshot_ttl = int(raw.get("snapshot_ttl_seconds") or 30 * 86400)
    except (TypeError, ValueError):
        snapshot_ttl = 30 * 86400
    conn = normalize_connection(vendor, raw.get("connection"), (existing or {}).get("connection"))
    missing = missing_connection_fields(vendor, conn)
    if missing:
        raise SyncError("connection is missing: " + ", ".join(missing))
    schedule = normalize_schedule(raw.get("schedule"))
    doc = {
        "doc_type": "warehouse_sync",
        "sync_id": (existing or {}).get("sync_id") or new_sync_id(name),
        "name": name,
        "vendor": vendor,
        "connection": conn,
        "query": query,
        "set_id": str(raw.get("set_id") or (existing or {}).get("set_id") or "default"),
        "allowed_roles": allowed,
        "table_name": str(raw.get("table_name") or name).strip()[:200],
        "description": str(raw.get("description") or "")[:500],
        "schedule": schedule,
        "mode": mode,
        "snapshot_ttl_seconds": max(3600, min(snapshot_ttl, 365 * 86400)),
        "enabled": bool(raw.get("enabled", (existing or {}).get("enabled", True))),
        "created_at": (existing or {}).get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "current_document_id": (existing or {}).get("current_document_id"),
        "active_job_id": (existing or {}).get("active_job_id"),
        "last_run": (existing or {}).get("last_run"),
        "runs": (existing or {}).get("runs") or [],
        "next_run_at": None,
    }
    doc["next_run_at"] = compute_next_run(doc, after=time.time())
    return doc


def public_sync(doc: dict) -> dict:
    out = dict(doc)
    out["connection"] = public_connection(doc["vendor"], doc.get("connection") or {})
    return out


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------
def _cron_field(expr: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("step must be positive")
        if part == "*":
            rng = range(lo, hi + 1)
        elif "-" in part:
            a, b = part.split("-", 1)
            rng = range(int(a), int(b) + 1)
        else:
            v = int(part)
            rng = range(v, hi + 1) if step > 1 else range(v, v + 1)
        for v in rng:
            if lo <= v <= hi and (v - rng.start) % step == 0:
                out.add(v)
    if not out:
        raise ValueError("empty field")
    return out


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise SyncError("a cron schedule has five fields: minute hour day-of-month month day-of-week (UTC)")
    try:
        minute = _cron_field(parts[0], 0, 59)
        hour = _cron_field(parts[1], 0, 23)
        dom = _cron_field(parts[2], 1, 31)
        month = _cron_field(parts[3], 1, 12)
        dow = {0 if v == 7 else v for v in _cron_field(parts[4], 0, 7)}
    except ValueError as exc:
        raise SyncError(f"invalid cron expression '{expr}': {exc}") from exc
    return minute, hour, dom, month, dow


def next_cron_time(expr: str, after: float) -> float:
    """The next UTC time strictly after `after` matching the expression,
    found by stepping minutes (bounded at 400 days)."""
    minute, hour, dom, month, dow = parse_cron(expr)
    t = _dt.datetime.fromtimestamp(after, tz=_dt.timezone.utc).replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    end = t + _dt.timedelta(days=400)
    while t < end:
        if t.month not in month:
            t = (t.replace(day=1) + _dt.timedelta(days=32)).replace(day=1, hour=0, minute=0)
            continue
        if t.day not in dom or (t.weekday() + 1) % 7 not in dow:
            t = (t + _dt.timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in hour:
            t = (t + _dt.timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in minute:
            t += _dt.timedelta(minutes=1)
            continue
        return t.timestamp()
    raise SyncError(f"cron expression '{expr}' never matches within 400 days")


def compute_next_run(sync: dict, *, after: float) -> str | None:
    if not sync.get("enabled"):
        return None
    sched = sync.get("schedule") or {}
    if sched.get("kind") == "cron":
        ts = next_cron_time(sched["cron"], after)
    elif sched.get("kind") == "interval":
        ts = after + int(sched.get("every_minutes") or 1440) * 60
    else:
        return None
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Query executors - each yields row dicts, streaming
# ---------------------------------------------------------------------------
def _http_error(vendor: str, resp: requests.Response, what: str) -> SyncError:
    detail = ""
    try:
        body = resp.json()
        detail = body.get("message") or body.get("error_description") or (body.get("error") or {}).get("message") if isinstance(body, dict) else ""
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail)[:200]
    except ValueError:
        detail = (resp.text or "")[:300]
    if resp.status_code in (401, 403):
        return SyncError(f"{vendor} refused the credential while {what} (HTTP {resp.status_code}){': ' + detail if detail else ''}")
    if resp.status_code == 404:
        return SyncError(f"{vendor} answered 404 while {what} - check the account/workspace URL{': ' + detail if detail else ''}")
    return SyncError(f"{vendor} answered HTTP {resp.status_code} while {what}{': ' + detail if detail else ''}")


# -- Snowflake ----------------------------------------------------------------
def _snowflake_jwt(account: str, user: str, private_key_pem: str, passphrase: str | None) -> str:
    try:
        import jwt
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover
        raise SyncError("Snowflake key-pair auth needs PyJWT and cryptography") from exc
    try:
        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=(passphrase.encode("utf-8") if passphrase else None)
        )
    except (ValueError, TypeError) as exc:
        raise SyncError(f"the Snowflake private key could not be read ({exc}) - paste the PKCS#8 PEM, and the passphrase if it is encrypted") from exc
    pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(pub).digest()).decode()
    # Snowflake wants the account identifier without region/cloud and upper-cased.
    acct = account.split(".")[0].upper()
    if "-" in acct and acct.count("-") == 1:
        pass  # org-account form (ORG-ACCOUNT) is used as-is
    usr = user.upper()
    now = int(time.time())
    payload = {"iss": f"{acct}.{usr}.{fingerprint}", "sub": f"{acct}.{usr}", "iat": now, "exp": now + 3540}
    return jwt.encode(payload, key, algorithm="RS256")


def _snowflake_headers(conn: dict) -> dict:
    method = conn.get("auth_method") or "keypair"
    if method == "keypair":
        token = _snowflake_jwt(conn["account"], conn["user"], _get("snowflake", conn, "private_key"), _get("snowflake", conn, "private_key_passphrase") or None)
        return {"Authorization": f"Bearer {token}", "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
                "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "couchbase-aom/1.0"}
    token = _get("snowflake", conn, "token")
    kind = "PROGRAMMATIC_ACCESS_TOKEN" if token and not token.count(".") >= 2 else "OAUTH"
    return {"Authorization": f"Bearer {token}", "X-Snowflake-Authorization-Token-Type": kind,
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "couchbase-aom/1.0"}


def run_snowflake(conn: dict, query: str, *, cancelled: Callable[[], bool], on_total=None) -> Iterator[dict]:
    base = f"https://{conn['account']}.snowflakecomputing.com/api/v2/statements"
    headers = _snowflake_headers(conn)
    body = {"statement": query, "timeout": QUERY_TIMEOUT_SECONDS, "warehouse": conn.get("warehouse") or None,
            "role": conn.get("role") or None, "database": conn.get("database") or None, "schema": conn.get("schema") or None,
            "parameters": {"MULTI_STATEMENT_COUNT": 1}}
    body = {k: v for k, v in body.items() if v is not None}
    try:
        resp = requests.post(base, headers=headers, json=body, params={"async": "true"}, timeout=HTTP_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        raise SyncError(f"could not reach Snowflake at {conn['account']}.snowflakecomputing.com: {exc.__class__.__name__}: {exc}") from exc
    if resp.status_code >= 400 and resp.status_code != 202:
        raise _http_error("Snowflake", resp, "submitting the statement")
    data = resp.json()
    handle = data.get("statementHandle")
    # 202 = still running: poll.
    started = time.time()
    while resp.status_code == 202 or (data.get("statementStatusUrl") and "resultSetMetaData" not in data):
        if cancelled():
            try:
                requests.post(f"{base}/{handle}/cancel", headers=headers, timeout=HTTP_TIMEOUT)
            finally:
                raise SyncError("cancelled")
        if time.time() - started > QUERY_TIMEOUT_SECONDS:
            raise SyncError(f"the Snowflake statement did not finish within {QUERY_TIMEOUT_SECONDS}s")
        time.sleep(2)
        resp = requests.get(f"{base}/{handle}", headers=headers, timeout=HTTP_TIMEOUT)
        if resp.status_code >= 400 and resp.status_code != 202:
            raise _http_error("Snowflake", resp, "waiting for the statement")
        data = resp.json()
    meta = data.get("resultSetMetaData") or {}
    columns = [c["name"] for c in meta.get("rowType") or []]
    partitions = meta.get("partitionInfo") or [{}]
    total = sum(int(p.get("rowCount") or 0) for p in partitions) or int(meta.get("numRows") or 0)
    if on_total and total:
        on_total(total)
    rows = data.get("data") or []
    for r in rows:
        yield dict(zip(columns, r))
    for n in range(1, len(partitions)):
        if cancelled():
            raise SyncError("cancelled")
        pr = requests.get(f"{base}/{handle}", headers=headers, params={"partition": n}, timeout=HTTP_TIMEOUT)
        if pr.status_code >= 400:
            raise _http_error("Snowflake", pr, f"fetching result partition {n}")
        for r in pr.json().get("data") or []:
            yield dict(zip(columns, r))


# -- Databricks ---------------------------------------------------------------
def _databricks_headers(conn: dict) -> dict:
    host = conn["host"].rstrip("/")
    if (conn.get("auth_method") or "oauth") == "oauth":
        from app import server_auth
        auth = {"token_url": f"{host}/oidc/v1/token", "username": conn.get("client_id") or "", "scope": "all-apis", "audience": ""}
        try:
            tok = server_auth.fetch_oauth_token(auth, _get("databricks", conn, "client_secret"))
        except server_auth.OAuthError as exc:
            raise SyncError(f"Databricks OAuth token exchange failed: {exc}") from exc
        return {"Authorization": f"Bearer {tok['access_token']}", "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {_get('databricks', conn, 'token')}", "Content-Type": "application/json"}


def run_databricks(conn: dict, query: str, *, cancelled: Callable[[], bool], on_total=None) -> Iterator[dict]:
    host = conn["host"].rstrip("/")
    if not host.startswith("http"):
        host = "https://" + host
    headers = _databricks_headers(conn)
    body = {"warehouse_id": conn["warehouse_id"], "statement": query, "disposition": "EXTERNAL_LINKS", "format": "JSON_ARRAY",
            "wait_timeout": "30s", "on_wait_timeout": "CONTINUE"}
    if conn.get("catalog"):
        body["catalog"] = conn["catalog"]
    if conn.get("schema"):
        body["schema"] = conn["schema"]
    try:
        resp = requests.post(f"{host}/api/2.0/sql/statements", headers=headers, json=body, timeout=HTTP_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        raise SyncError(f"could not reach Databricks at {host}: {exc.__class__.__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise _http_error("Databricks", resp, "submitting the statement")
    data = resp.json()
    sid = data.get("statement_id")
    started = time.time()
    while (data.get("status") or {}).get("state") in ("PENDING", "RUNNING"):
        if cancelled():
            requests.post(f"{host}/api/2.0/sql/statements/{sid}/cancel", headers=headers, timeout=HTTP_TIMEOUT)
            raise SyncError("cancelled")
        if time.time() - started > QUERY_TIMEOUT_SECONDS:
            raise SyncError(f"the Databricks statement did not finish within {QUERY_TIMEOUT_SECONDS}s")
        time.sleep(3)
        resp = requests.get(f"{host}/api/2.0/sql/statements/{sid}", headers=headers, timeout=HTTP_TIMEOUT)
        if resp.status_code >= 400:
            raise _http_error("Databricks", resp, "waiting for the statement")
        data = resp.json()
    state = (data.get("status") or {}).get("state")
    if state != "SUCCEEDED":
        err = (data.get("status") or {}).get("error") or {}
        raise SyncError(f"Databricks statement {state}: {err.get('message') or 'no detail'}")
    manifest = data.get("manifest") or {}
    columns = [c["name"] for c in (manifest.get("schema") or {}).get("columns") or []]
    total = int(manifest.get("total_row_count") or 0)
    if on_total and total:
        on_total(total)
    result = data.get("result") or {}
    chunk_index = 0
    while True:
        for link in result.get("external_links") or []:
            if cancelled():
                raise SyncError("cancelled")
            url = link.get("external_link")
            if not url:
                continue
            cr = requests.get(url, timeout=HTTP_TIMEOUT, stream=True)
            if cr.status_code >= 400:
                raise SyncError(f"Databricks result chunk {link.get('chunk_index')} could not be fetched (HTTP {cr.status_code}) - the presigned link may have expired")
            for r in cr.json():
                yield dict(zip(columns, r))
            nxt = link.get("next_chunk_internal_link")
            if nxt:
                chunk_index = link.get("chunk_index", chunk_index) + 1
        nxt_link = None
        for link in result.get("external_links") or []:
            nxt_link = link.get("next_chunk_internal_link") or nxt_link
        if not nxt_link:
            break
        nr = requests.get(f"{host}{nxt_link}", headers=headers, timeout=HTTP_TIMEOUT)
        if nr.status_code >= 400:
            raise _http_error("Databricks", nr, "fetching the next result chunk")
        result = nr.json()


# -- BigQuery -----------------------------------------------------------------
def _bigquery_token(conn: dict) -> str:
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise SyncError("BigQuery syncs need the 'google-auth' package in the operations-manager image") from exc
    raw = _get("bigquery", conn, "service_account_json")
    try:
        info = json.loads(raw)
    except ValueError as exc:
        raise SyncError("the service-account key is not valid JSON") from exc
    try:
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/bigquery.readonly"]
        )
        creds.refresh(Request())
    except Exception as exc:  # noqa: BLE001
        raise SyncError(f"Google rejected the service-account key: {str(exc)[:300]}") from exc
    return creds.token


def _bq_value(field: dict, cell: dict):
    v = cell.get("v")
    if v is None:
        return None
    if field.get("mode") == "REPEATED":
        return [_bq_value(dict(field, mode="NULLABLE"), x) for x in v]
    t = field.get("type")
    if t in ("RECORD", "STRUCT"):
        sub = field.get("fields") or []
        return {f["name"]: _bq_value(f, c) for f, c in zip(sub, v.get("f") or [])}
    if t in ("INTEGER", "INT64"):
        return int(v)
    if t in ("FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"):
        return float(v)
    if t in ("BOOLEAN", "BOOL"):
        return v in (True, "true", "TRUE")
    if t == "TIMESTAMP":
        try:
            return _dt.datetime.fromtimestamp(float(v), tz=_dt.timezone.utc).isoformat()
        except (TypeError, ValueError):
            return v
    return v


def run_bigquery(conn: dict, query: str, *, cancelled: Callable[[], bool], on_total=None) -> Iterator[dict]:
    token = _bigquery_token(conn)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    project = conn["project_id"]
    body = {"query": query, "useLegacySql": False, "maxResults": 10000, "timeoutMs": 30000}
    if conn.get("location"):
        body["location"] = conn["location"]
    try:
        resp = requests.post(f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries", headers=headers, json=body, timeout=HTTP_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        raise SyncError(f"could not reach BigQuery: {exc.__class__.__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise _http_error("BigQuery", resp, "submitting the query")
    data = resp.json()
    job = data.get("jobReference") or {}
    job_id, location = job.get("jobId"), job.get("location")
    started = time.time()
    while not data.get("jobComplete", True):
        if cancelled():
            requests.post(f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/jobs/{job_id}/cancel", headers=headers, params={"location": location}, timeout=HTTP_TIMEOUT)
            raise SyncError("cancelled")
        if time.time() - started > QUERY_TIMEOUT_SECONDS:
            raise SyncError(f"the BigQuery job did not finish within {QUERY_TIMEOUT_SECONDS}s")
        resp = requests.get(f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries/{job_id}", headers=headers,
                            params={"location": location, "maxResults": 10000, "timeoutMs": 30000}, timeout=HTTP_TIMEOUT)
        if resp.status_code >= 400:
            raise _http_error("BigQuery", resp, "waiting for the query")
        data = resp.json()
    fields = (data.get("schema") or {}).get("fields") or []
    total = int(data.get("totalRows") or 0)
    if on_total and total:
        on_total(total)
    while True:
        for row in data.get("rows") or []:
            yield {f["name"]: _bq_value(f, c) for f, c in zip(fields, row.get("f") or [])}
        token_page = data.get("pageToken")
        if not token_page:
            break
        if cancelled():
            raise SyncError("cancelled")
        resp = requests.get(f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries/{job_id}", headers=headers,
                            params={"location": location, "maxResults": 10000, "pageToken": token_page}, timeout=HTTP_TIMEOUT)
        if resp.status_code >= 400:
            raise _http_error("BigQuery", resp, "fetching the next result page")
        data = resp.json()


RUNNERS = {"snowflake": run_snowflake, "databricks": run_databricks, "bigquery": run_bigquery}


def preview_query(vendor: str, query: str) -> str:
    q = query.strip().rstrip(";")
    return f"SELECT * FROM ({q}) AS aom_preview LIMIT {PREVIEW_ROWS}"


def test_connection(vendor: str, conn: dict, query: str | None) -> dict:
    """Authenticate, and if a query is given run a LIMIT-10 preview.
    Returns {ok, detail, columns, rows}."""
    missing = missing_connection_fields(vendor, conn)
    if missing:
        return {"ok": False, "detail": "missing: " + ", ".join(missing), "columns": [], "rows": []}
    started = time.time()
    q = preview_query(vendor, query) if query and query.strip() else {
        "snowflake": "SELECT CURRENT_VERSION() AS version", "databricks": "SELECT current_version().dbr_version AS version",
        "bigquery": "SELECT 1 AS ok"}[vendor]
    rows: list[dict] = []
    try:
        for r in RUNNERS[vendor](conn, q, cancelled=lambda: False):
            rows.append(r)
            if len(rows) >= PREVIEW_ROWS:
                break
    except SyncError as exc:
        return {"ok": False, "detail": str(exc), "columns": [], "rows": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{exc.__class__.__name__}: {str(exc)[:300]}", "columns": [], "rows": []}
    columns = list(rows[0].keys()) if rows else []
    ms = int((time.time() - started) * 1000)
    detail = (f"connected; preview returned {len(rows)} row(s) across {len(columns)} column(s) in {ms} ms"
              if query and query.strip() else f"connected in {ms} ms")
    return {"ok": True, "detail": detail, "columns": columns, "rows": [{k: _jsonable(v) for k, v in r.items()} for r in rows]}


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def write_rows_ndjson(vendor: str, conn: dict, query: str, path: str, *, cancelled: Callable[[], bool],
                      on_progress: Callable[[int], None], on_total: Callable[[int], None]) -> int:
    """Run the query and stream the rows to `path` as newline-delimited
    JSON (what the ingestion job parses). Returns the row count."""
    n = 0
    last = time.time()
    with open(path, "w", encoding="utf-8") as out:
        for row in RUNNERS[vendor](conn, query, cancelled=cancelled, on_total=on_total):
            out.write(json.dumps(row, default=str, separators=(",", ":")))
            out.write("\n")
            n += 1
            if time.time() - last > 2:
                last = time.time()
                on_progress(n)
    on_progress(n)
    return n
