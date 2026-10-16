"""Operator client for the Couchbase Agent Operations Manager.

`AOMClient` is what an *agent* uses: a bearer key, discover/invoke,
completions, memory, retrieval. This module is the other side - what an
operator, a data pipeline or CI uses to feed and configure the appliance:

  - load the Knowledge Base at scale: stream a multi-gigabyte file, queue a
    cloud import (s3://, gs://, az://, https://), follow the ingestion job;
  - manage warehouse syncs: a scheduled SQL query against Snowflake,
    Databricks or BigQuery that keeps a table document fresh;
  - manage RBAC roles, and test an MCP server's connection.

These routes sit behind the dashboard login (a local account or LDAP), not
an agent key - adding documents is an operator decision, since who may
*read* a document is an RBAC decision made at upload time. So this client
signs in with a username and password and carries the session cookie.

    from aom_sdk import AOMAdminClient

    admin = AOMAdminClient("https://localhost:3111", username="admin",
                           password=os.environ["AOM_PASSWORD"], verify=False)
    job = admin.upload_knowledge("orders_0_0_0.csv.gz", allowed_roles=["finance_analyst"],
                                 dump={"source": "snowflake", "table_name": "SALES.PUBLIC.ORDERS"})
    job = admin.wait_for_job(job["job_id"])      # streams progress to the callback if given

Use the dashboard's origin (port 3111 on the bundled stack) rather than
the API port: that is where the session cookie is set, and the dashboard's
nginx is configured for unbuffered 5 GB uploads.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import urlencode

import requests

from .exceptions import (
    AOMAuthenticationError,
    AOMAuthorizationError,
    AOMConnectionError,
    AOMError,
    AOMNotFoundError,
    AOMServerError,
)

ACTIVE_JOB_STATUSES = ("uploading", "queued", "downloading", "running")


class _FileProgress:
    """Wraps a file so `requests` streams it in 1 MB pieces and we can
    report how far along the upload is."""

    def __init__(self, fh, total: int, on_progress: Optional[Callable[[int, int], None]]):
        self.fh, self.total, self.on_progress, self.sent = fh, total, on_progress, 0

    def __iter__(self) -> Iterator[bytes]:
        while True:
            block = self.fh.read(1 << 20)
            if not block:
                return
            self.sent += len(block)
            if self.on_progress:
                self.on_progress(self.sent, self.total)
            yield block


class AOMAdminClient:
    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        verify: bool | str = True,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.verify = verify
        self.timeout = timeout
        self._session = requests.Session()
        self._session.verify = verify
        self._login(password)

    def __repr__(self) -> str:
        return f"AOMAdminClient(base_url={self.base_url!r}, username={self.username!r})"

    # -- plumbing --------------------------------------------------------------
    def _login(self, password: str) -> None:
        self._request("POST", "/v1/auth/login", json_body={"username": self.username, "password": password})

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        data: Any = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method, url, json=json_body, params=params, data=data, headers=headers,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise AOMConnectionError(f"Could not reach the operations manager at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text or None
            message = detail or f"{response.status_code} {response.reason}"
            if response.status_code == 401:
                raise AOMAuthenticationError(message, response.status_code, detail)
            if response.status_code == 403:
                raise AOMAuthorizationError(message, response.status_code, detail)
            if response.status_code == 404:
                raise AOMNotFoundError(message, response.status_code, detail)
            if response.status_code >= 500:
                raise AOMServerError(message, response.status_code, detail)
            raise AOMError(message, response.status_code, detail)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def me(self) -> dict:
        """GET /v1/auth/me - the signed-in account."""
        return self._request("GET", "/v1/auth/me")["user"]

    # -- Knowledge Base: documents -------------------------------------------
    def knowledge(self) -> dict:
        """GET /v1/knowledge - documents, knowledge sets, limits, spool state
        and the recent ingestion jobs."""
        return self._request("GET", "/v1/knowledge")

    def delete_knowledge_document(self, document_id: str) -> dict:
        return self._request("DELETE", f"/v1/knowledge/{document_id}")

    def ingest_text(self, title: str, content: str, *, allowed_roles: List[str], set_id: Optional[str] = None,
                    ttl_seconds: Optional[int] = None, expires_at: Optional[str] = None) -> dict:
        """POST /v1/knowledge - the small inline path (pasted text, up to the
        appliance's inline limit, 20 MB by default). Anything larger goes
        through upload_knowledge()."""
        body: Dict[str, Any] = {"title": title, "content": content, "encoding": "text", "allowed_roles": allowed_roles}
        if set_id:
            body["set_id"] = set_id
        if ttl_seconds:
            body["ttl_seconds"] = ttl_seconds
        if expires_at:
            body["expires_at"] = expires_at
        return self._request("POST", "/v1/knowledge", json_body=body)["document"]

    def upload_knowledge(
        self,
        path: str,
        *,
        allowed_roles: List[str],
        title: Optional[str] = None,
        set_id: Optional[str] = None,
        dump: Optional[dict] = None,
        ttl_seconds: Optional[int] = None,
        expires_at: Optional[str] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        """POST /v1/knowledge/uploads - stream a local file (up to the
        appliance's KNOWLEDGE_MAX_UPLOAD_MB, 5 GB by default) and return the
        ingestion job at once. Nothing is read into memory on either side.

        `dump` marks a warehouse table export and is parsed by row:
            {"source": "snowflake"|"databricks"|"bigquery"|"other",
             "table_name": "SALES.PUBLIC.ORDERS",
             "header": True|False|None (auto),      # CSV/TSV only
             "column_names": ["id", ...]}           # when header is False
        Without it the file is treated as a document (text, Markdown, PDF...).

        `on_progress(sent_bytes, total_bytes)` is called as the body goes out.
        Follow the job with wait_for_job()."""
        size = os.path.getsize(path)
        q: Dict[str, Any] = {"filename": os.path.basename(path), "allowed_roles": ",".join(allowed_roles)}
        if title:
            q["title"] = title
        if set_id:
            q["set_id"] = set_id
        if ttl_seconds:
            q["ttl_seconds"] = ttl_seconds
        if expires_at:
            q["expires_at"] = expires_at
        if dump:
            q["dump_source"] = dump.get("source") or "other"
            if dump.get("table_name"):
                q["table_name"] = dump["table_name"]
            if dump.get("header") is not None:
                q["header"] = str(bool(dump["header"])).lower()
            if dump.get("column_names"):
                q["column_names"] = ",".join(dump["column_names"])
        with open(path, "rb") as fh:
            res = self._request(
                "POST", f"/v1/knowledge/uploads?{urlencode(q)}",
                data=_FileProgress(fh, size, on_progress),
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                timeout=max(self.timeout, 6 * 3600),
            )
        return res["job"]

    def import_knowledge(
        self,
        uri: str,
        *,
        allowed_roles: List[str],
        auth: Optional[dict] = None,
        title: Optional[str] = None,
        filename: Optional[str] = None,
        set_id: Optional[str] = None,
        dump: Optional[dict] = None,
        ttl_seconds: Optional[int] = None,
        expires_at: Optional[str] = None,
    ) -> dict:
        """POST /v1/knowledge/imports - queue an import from cloud storage or
        a URL; the appliance streams the object itself. Returns the job.

        `uri` is s3://, gs://, az:// / abfss://, or https://. `auth` is used
        for the download only and never stored:
            https: {"mode": "bearer", "secret": token}  or {"mode": "header", "header_name": ..., "secret": ...}
            s3:    {"access_key_id": ..., "secret": ..., "region": ...}   (omit for the pod's IAM role)
            gs:    {"service_account_json": "<json text>"}                (omit for ADC)
            az:    {"connection_string": ...} or {"account": ..., "secret": "<key or SAS>"}"""
        body: Dict[str, Any] = {"uri": uri, "allowed_roles": allowed_roles}
        for k, v in (("auth", auth), ("title", title), ("filename", filename), ("set_id", set_id), ("dump", dump),
                     ("ttl_seconds", ttl_seconds), ("expires_at", expires_at)):
            if v is not None:
                body[k] = v
        return self._request("POST", "/v1/knowledge/imports", json_body=body)["job"]

    # -- Knowledge Base: ingestion jobs --------------------------------------
    def knowledge_jobs(self, limit: int = 50) -> List[dict]:
        return self._request("GET", "/v1/knowledge/jobs", params={"limit": limit})["jobs"]

    def knowledge_job(self, job_id: str) -> dict:
        return self._request("GET", f"/v1/knowledge/jobs/{job_id}")["job"]

    def cancel_job(self, job_id: str) -> dict:
        """Stop a job; chunks it already wrote are removed."""
        return self._request("POST", f"/v1/knowledge/jobs/{job_id}/cancel")["job"]

    def retry_job(self, job_id: str) -> dict:
        return self._request("POST", f"/v1/knowledge/jobs/{job_id}/retry")["job"]

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll_seconds: float = 2.0,
        timeout: Optional[float] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """Poll until the job is done, failed or cancelled. Raises AOMError
        for anything but `done`, with the job's own error text."""
        started = time.time()
        while True:
            job = self.knowledge_job(job_id)
            if on_progress:
                on_progress(job)
            if job["status"] not in ACTIVE_JOB_STATUSES:
                if job["status"] != "done":
                    raise AOMError(f"job {job_id} {job['status']}: {job.get('error')}", None, job.get("error"))
                return job
            if timeout and time.time() - started > timeout:
                raise AOMError(f"job {job_id} still {job['status']} after {timeout:.0f}s", None, None)
            time.sleep(poll_seconds)

    # -- Warehouse syncs -----------------------------------------------------
    def warehouse_syncs(self) -> dict:
        """GET /v1/warehouse-syncs - {syncs, vendors (connection field
        catalog per vendor), modes}."""
        return self._request("GET", "/v1/warehouse-syncs")

    def create_warehouse_sync(self, sync: dict) -> dict:
        """POST /v1/warehouse-syncs. `sync` is:
            {"name", "vendor": "snowflake"|"databricks"|"bigquery",
             "connection": {...fields from warehouse_syncs()["vendors"]...},
             "query": "SELECT ...", "allowed_roles": [...], "set_id", "table_name",
             "schedule": {"kind": "interval", "every_minutes": 1440}
                       | {"kind": "cron", "cron": "0 2 * * *"} | {"kind": "manual"},
             "mode": "replace"|"snapshot", "snapshot_ttl_seconds", "enabled"}"""
        return self._request("POST", "/v1/warehouse-syncs", json_body={"sync": sync})["sync"]

    def update_warehouse_sync(self, sync_id: str, sync: dict) -> dict:
        """PUT /v1/warehouse-syncs/{id}. Secret connection fields sent as
        "********" (as the API returns them) keep their stored value."""
        return self._request("PUT", f"/v1/warehouse-syncs/{sync_id}", json_body={"sync": sync})["sync"]

    def delete_warehouse_sync(self, sync_id: str, *, delete_document: bool = False) -> dict:
        return self._request("DELETE", f"/v1/warehouse-syncs/{sync_id}", params={"delete_document": str(delete_document).lower()})

    def run_warehouse_sync(self, sync_id: str) -> dict:
        """POST /v1/warehouse-syncs/{id}/run - start a run now; returns the
        ingestion job (follow it with wait_for_job())."""
        return self._request("POST", f"/v1/warehouse-syncs/{sync_id}/run")["job"]

    def set_warehouse_sync_enabled(self, sync_id: str, enabled: bool) -> dict:
        return self._request("POST", f"/v1/warehouse-syncs/{sync_id}/enabled", params={"enabled": str(enabled).lower()})["sync"]

    def test_warehouse_sync(self, vendor: str, connection: dict, query: Optional[str] = None, *, sync_id: Optional[str] = None) -> dict:
        """POST /v1/warehouse-syncs/test - authenticate and, with a query,
        preview 10 rows: {ok, detail, columns, rows}."""
        body: Dict[str, Any] = {"vendor": vendor, "connection": connection}
        if query:
            body["query"] = query
        if sync_id:
            body["sync_id"] = sync_id
        return self._request("POST", "/v1/warehouse-syncs/test", json_body=body)

    # -- Roles ------------------------------------------------------------------
    def roles(self) -> List[dict]:
        """GET /v1/roles - every role, built-in and custom."""
        return self._request("GET", "/v1/roles")["roles"]

    def create_role(self, role_id: str, description: str) -> dict:
        return self._request("POST", "/v1/roles", json_body={"id": role_id, "description": description})["role"]

    def update_role(self, role_id: str, description: str) -> dict:
        return self._request("PUT", f"/v1/roles/{role_id}", json_body={"description": description})["role"]

    def delete_role(self, role_id: str, *, force: bool = False) -> dict:
        """DELETE /v1/roles/{id}. Refused while tools, servers or RAG apps
        still grant the role unless `force`, which strips those grants."""
        return self._request("DELETE", f"/v1/roles/{role_id}", params={"force": str(force).lower()})

    # -- MCP servers ------------------------------------------------------------
    def test_server_connection(self, *, server_id: Optional[str] = None, mcp_url: Optional[str] = None,
                               platform: Optional[str] = None, auth: Optional[dict] = None,
                               auth_secret: Optional[str] = None) -> dict:
        """POST /v1/servers/test-connection - probe a registered server or an
        unsaved form stage by stage (URL, DNS, TCP/TLS, credential, HTTP, MCP)
        and get {ok, summary, suggestion, steps, tools}."""
        body = {k: v for k, v in (("server_id", server_id), ("mcp_url", mcp_url), ("platform", platform),
                                  ("auth", auth), ("auth_secret", auth_secret)) if v is not None}
        return self._request("POST", "/v1/servers/test-connection", json_body=body)
