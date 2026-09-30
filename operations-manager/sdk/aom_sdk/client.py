"""
Client SDK for the Couchbase Agent Operations Manager.

An agent that talks to the operations manager directly has to attach a
bearer API key to every call, know the exact JSON shape for discover /
invoke / complete, and turn HTTP status codes back into something it can
branch on. `AOMClient` does all three, so integrating code looks like:

    from aom_sdk import AOMClient

    client = AOMClient("https://localhost:8090", api_key="demo-support-agent-9f21", verify=False)  # self-signed by default
    discovered = client.discover("look up a customer's open tickets")
    result = client.invoke(discovered["tools"][0]["tool_id"], arguments={})

See the bundled examples/ directory and the appliance's Tools -> Developer
SDK page for the full quickstart, including cached LLM completions.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

import requests

from .exceptions import (
    AOMApprovalError,
    AOMAuthenticationError,
    AOMAuthorizationError,
    AOMConnectionError,
    AOMError,
    AOMGuardrailError,
    AOMNotFoundError,
    AOMRateLimitError,
    AOMScopeError,
    AOMServerError,
    AOMTimeoutError,
)
from .mcp_tools import to_mcp_tool

DEFAULT_TIMEOUT_SECONDS = 30


class AOMClient:
    """A thin, typed wrapper around the operations manager's REST gateway.

    Parameters
    ----------
    base_url:
        Where the operations manager is reachable, e.g. ``http://localhost:8090``
        for the bundled Docker Compose stack, or your appliance's real
        origin in production.
    api_key:
        The bearer credential this agent authenticates with. Either an AOM
        agent key (issued from **Settings -> Agent Identities**, prefixed
        ``aom_``) or, when the appliance is configured to federate identity,
        an OIDC access token from your own provider - both are sent as
        ``Authorization: Bearer``, and the appliance works out which it is.
        Only required for calls that authenticate - discover, invoke,
        complete, memory and knowledge - not for ``health()`` or
        ``roles()``.

        Keys are stored by the appliance only as a hash, so a lost key is
        rotated rather than looked up. If yours stops working, the refusal
        says why: revoked, expired, or rotated past its grace window.
    timeout:
        Per-request timeout in seconds, passed straight to ``requests``.
    session:
        Reuse an existing ``requests.Session`` (e.g. for connection pooling
        across many agent instances) instead of letting the client create
        its own.
    verify:
        TLS verification, passed straight to ``requests`` on every call:
        ``True`` (default) verifies the server's certificate normally,
        ``False`` disables verification entirely, and a string path
        verifies against that specific CA bundle/certificate file instead
        of the system trust store. The bundled Docker Compose stack serves
        HTTPS with a self-signed certificate out of the box (see its
        README), which fails verification under the default ``True`` -
        pass ``False`` for that, or point ``verify`` at the exported
        certificate, until you've installed a real one.
    agent_id:
        An optional name for this agent, attached to every call and shown
        on the Traces page. Purely descriptive - it is not an identity and
        grants nothing; the API key is what authenticates.
    session_id:
        An optional conversation/session identifier, attached to every call
        so runs belonging to one session can be found together.

    Tracing
    -------
    Each call carries a trace ID. By default the client mints one per call,
    which is enough for a single-request trace. Wrap the work an agent does
    for one task in :meth:`run` and every call inside it shares a trace ID
    instead, so the appliance's Traces page shows one run with the
    discovery, the tool calls and the completions in order rather than a
    scatter of unrelated single-span traces:

        with client.run(name="resolve ticket 4821") as run:
            tools = client.discover("look up a customer's open tickets")
            result = client.invoke(tools["tools"][0]["tool_id"])
            answer = client.complete("Summarise that ticket in two sentences.")
            print(run.trace_id)
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        session: Optional[requests.Session] = None,
        verify: "bool | str" = True,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required, e.g. AOMClient('https://localhost:8090')")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.verify = verify
        self.agent_id = agent_id
        self.session_id = session_id
        self._trace_id: Optional[str] = None
        self._session = session or requests.Session()

    def __repr__(self) -> str:
        return f"AOMClient(base_url={self.base_url!r}, authenticated={bool(self.api_key)})"

    # -- internals ----------------------------------------------------------
    def _headers(self, auth: bool) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if auth:
            if not self.api_key:
                raise AOMAuthenticationError(
                    "This call requires an API key - pass api_key=... to AOMClient(...)"
                )
            headers["Authorization"] = f"Bearer {self.api_key}"
        # Inside a run() block every call shares that run's trace ID; outside
        # one, each call is its own single-span trace. Either way the call is
        # traced - an agent should not have to opt in to being observable.
        headers["X-AOM-Trace-Id"] = self._trace_id or uuid.uuid4().hex
        if self.agent_id:
            headers["X-AOM-Agent-Id"] = self.agent_id
        if self.session_id:
            headers["X-AOM-Session-Id"] = self.session_id
        return headers

    # -- runs ----------------------------------------------------------------
    def run(self, name: Optional[str] = None, trace_id: Optional[str] = None) -> "AgentRun":
        """Group every call made inside the block into one traced run.

        Nesting is not supported and is not silently tolerated: two
        overlapping runs would produce a trace that claims a structure the
        agent does not have, which is worse than no trace at all.
        """
        return AgentRun(self, name=name, trace_id=trace_id)

    @property
    def trace_id(self) -> Optional[str]:
        """The current run's trace ID, or None outside a run() block."""
        return self._trace_id

    def trace(self, trace_id: str) -> dict:
        """GET /v1/traces/{trace_id} - one run, its spans in order, and the
        catalog state of every tool it touched. Requires a dashboard
        session, not an agent API key: this is the operator's view."""
        return self._request("GET", f"/v1/traces/{trace_id}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = False,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method,
                url,
                headers=self._headers(auth),
                json=json_body,
                params=params,
                timeout=self.timeout,
                verify=self.verify,
            )
        except requests.RequestException as exc:
            raise AOMConnectionError(
                f"Could not reach the operations manager at {self.base_url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            detail: Optional[str]
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text or None
            message = detail or f"{response.status_code} {response.reason}"

            # The appliance names why it refused in X-AOM-Refusal, so a
            # caller branches on a type rather than pattern-matching English
            # out of the detail string - which would break the first time
            # somebody reworded a message.
            refusal = response.headers.get("X-AOM-Refusal")

            if response.status_code == 401:
                raise AOMAuthenticationError(message, response.status_code, detail)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                raise AOMRateLimitError(
                    message, response.status_code, detail,
                    retry_after=int(retry_after) if (retry_after or "").isdigit() else None,
                    limit=response.headers.get("X-AOM-Limit"),
                )
            if response.status_code == 400 and refusal == "guardrail":
                raise AOMGuardrailError(message, response.status_code, detail)
            if response.status_code == 403:
                if refusal == "scope":
                    raise AOMScopeError(message, response.status_code, detail)
                if refusal == "approval":
                    raise AOMApprovalError(message, response.status_code, detail)
                raise AOMAuthorizationError(message, response.status_code, detail)
            if response.status_code == 404:
                raise AOMNotFoundError(message, response.status_code, detail)
            if response.status_code == 504:
                raise AOMTimeoutError(message, response.status_code, detail)
            if response.status_code >= 500:
                raise AOMServerError(message, response.status_code, detail)
            raise AOMError(message, response.status_code, detail)

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- health / metadata ----------------------------------------------------
    def health(self) -> dict:
        """GET /api/health - no API key required. Useful as a startup
        readiness check before an agent starts sending real traffic."""
        return self._request("GET", "/api/health")

    def roles(self) -> List[dict]:
        """GET /v1/roles - the RBAC roles configured on this appliance."""
        return self._request("GET", "/v1/roles")["roles"]

    # -- tool discovery / invocation -------------------------------------------
    def discover(self, query: str, top_k: int = 5) -> dict:
        """POST /v1/tools/discover - RBAC + vector-search pre-filtered tool
        search for your role. Never returns a tool your API key's role
        can't also invoke."""
        return self._request(
            "POST", "/v1/tools/discover", auth=True,
            json_body={"query": query, "top_k": top_k},
        )

    def invoke(self, tool_id: str, arguments: Optional[dict] = None, approval_id: Optional[str] = None) -> dict:
        """POST /v1/tools/invoke - re-checks authorization independently of
        discover, then proxies to the tool's real MCP server. The response
        includes ``hijack_warning`` (non-null when the live payload was
        flagged by the MCP Tool Hijacking detector) - always check it
        rather than trusting ``result`` blindly.

        A tool in the approval tier returns a dict with
        ``status == "pending_approval"`` and an ``approval`` block instead of
        a result. Poll :meth:`approval` and call this again with
        ``approval_id``, or use :meth:`invoke_with_approval` to have that
        handled for you."""
        body = {"tool_id": tool_id, "arguments": arguments or {}}
        if approval_id:
            body["approval_id"] = approval_id
        return self._request("POST", "/v1/tools/invoke", auth=True, json_body=body)

    def discover_and_invoke(
        self, query: str, arguments: Optional[dict] = None, top_k: int = 1
    ) -> dict:
        """Convenience wrapper: discover the single best-matching tool for
        ``query`` and invoke it immediately. Raises ``AOMNotFoundError`` if
        nothing matched - handy for a quick script, less so for an agent
        that should reason about *which* discovered tool to pick."""
        discovered = self.discover(query, top_k=max(1, top_k))
        tools = discovered.get("tools") or []
        if not tools:
            raise AOMNotFoundError(f"No tool matched query: {query!r}")
        return self.invoke(tools[0]["tool_id"], arguments=arguments)

    # -- cached LLM completions ----------------------------------------------
    def complete(
        self,
        prompt: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        namespace: Optional[str] = None,
        bypass_cache: bool = False,
        params: Optional[dict] = None,
    ) -> dict:
        """POST /v1/llm/complete - a cached completion. A repeat or
        near-duplicate prompt (see the similarity threshold in the LLM
        Caching policy) is answered from Couchbase without spending a
        single token. ``response["cache"]["status"]`` is one of
        ``hit_exact`` / ``hit_semantic`` / ``miss`` / ``bypass``;
        ``response["usage"]`` and ``response["cost_usd"]`` reflect actual
        provider spend - i.e. zero on any hit. Set ``bypass_cache=True`` for
        a prompt that must always reach the live model."""
        return self._request(
            "POST", "/v1/llm/complete", auth=True,
            json_body={
                "prompt": prompt,
                "provider": provider,
                "model": model,
                "namespace": namespace,
                "bypass_cache": bypass_cache,
                "params": params or {},
            },
        )

    # -- cached context (arbitrary agent-fetched data, not LLM completions) --
    # Same gateway shape as complete() above, for anything else an agent
    # would otherwise cache against its own private database: a warehouse
    # query result, a search-API response, a document lookup. See the
    # appliance's Context Cache page - any agent using these two calls
    # shows up there, which is the whole point of routing through AOM
    # instead of caching locally and invisibly.

    def context_get(self, key: str, *, namespace: Optional[str] = None) -> dict:
        """POST /v1/context/get - exact-key lookup (no semantic matching -
        the value is opaque JSON you supplied, there is nothing to embed).
        Returns ``{"hit": bool, "value": ..., "cache": {...}, "latency_ms": ...}``;
        ``value`` is ``None`` on a miss. `namespace` scopes keys that would
        otherwise collide (default governed by the appliance's Context
        Cache policy)."""
        return self._request(
            "POST", "/v1/context/get", auth=True,
            json_body={"key": key, "namespace": namespace},
        )

    def context_set(
        self,
        key: str,
        value: Any,
        *,
        namespace: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        source_latency_ms: Optional[int] = None,
    ) -> dict:
        """POST /v1/context/set - store a value you just fetched yourself,
        so the next `context_get()` for this key is a Couchbase KV get
        instead of a live fetch. Pass `source_latency_ms` (how long your
        real fetch took) if you have it - it is what makes a future hit's
        "latency avoided" figure on the dashboard mean something, rather
        than reading zero. `ttl_seconds` overrides the policy default for
        just this entry."""
        return self._request(
            "POST", "/v1/context/set", auth=True,
            json_body={
                "key": key,
                "value": value,
                "namespace": namespace,
                "ttl_seconds": ttl_seconds,
                "source_latency_ms": source_latency_ms,
            },
        )

    def cached_context(
        self,
        key: str,
        fetch,
        *,
        namespace: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> Any:
        """The get-or-compute pattern most agents actually want, in one
        call: `context_get(key)`, and on a miss call `fetch()` yourself
        (no arguments, returns the value), time it, `context_set()` the
        result, and return it either way.

            item = client.cached_context(
                f"snowflake::{item_id}",
                lambda: expensive_snowflake_lookup(item_id),
            )
        """
        hit = self.context_get(key, namespace=namespace)
        if hit.get("hit"):
            return hit["value"]
        start = time.time()
        value = fetch()
        elapsed_ms = round((time.time() - start) * 1000)
        self.context_set(
            key, value, namespace=namespace, ttl_seconds=ttl_seconds, source_latency_ms=elapsed_ms
        )
        return value

    # -- agent memory ---------------------------------------------------------
    # Durable, cross-session recall stored in the same Couchbase cluster as
    # everything else in this appliance - not a separate service to run or
    # depend on. All four calls authenticate like discover/invoke/complete;
    # they are scoped by the `user_id` you pass, not by RBAC role.

    def add_memory(
        self,
        user_id: str,
        content: str,
        *,
        session_id: Optional[str] = None,
        memory_type: str = "conversational",
        metadata: Optional[dict] = None,
        ttl_seconds: int = 0,
    ) -> dict:
        """POST /v1/memory - store one memory entry for `user_id`, embedded
        for later semantic recall via `search_memory()`. `memory_type` is
        one of "conversational" (the default - what was said in a
        session), "profile" (durable facts about the user), or "semantic"
        (retrieved knowledge worth remembering). `ttl_seconds` is optional -
        memory is durable by default, unlike the LLM response cache."""
        return self._request(
            "POST", "/v1/memory", auth=True,
            json_body={
                "user_id": user_id,
                "content": content,
                "session_id": session_id,
                "memory_type": memory_type,
                "metadata": metadata or {},
                "ttl_seconds": ttl_seconds,
            },
        )

    def list_memory(
        self,
        user_id: str,
        *,
        session_id: Optional[str] = None,
        memory_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        """GET /v1/memory - chronological listing for one user, optionally
        narrowed to a session or memory type. Use this to re-hydrate recent
        context at the start of a turn; use `search_memory()` when you want
        the most *relevant* entries instead of the most recent ones."""
        params: Dict[str, Any] = {"user_id": user_id, "limit": limit}
        if session_id:
            params["session_id"] = session_id
        if memory_type:
            params["memory_type"] = memory_type
        return self._request("GET", "/v1/memory", auth=True, params=params)["entries"]

    def search_memory(
        self,
        user_id: str,
        query: str,
        *,
        session_id: Optional[str] = None,
        memory_type: Optional[str] = None,
        top_k: int = 5,
    ) -> List[dict]:
        """POST /v1/memory/search - semantic recall: the memory entries for
        `user_id` whose content is closest to `query`, ranked by
        similarity - the same vector-search pattern `discover()` runs over
        the tool catalog, scoped to one user's memory instead of one
        role's tools."""
        return self._request(
            "POST", "/v1/memory/search", auth=True,
            json_body={
                "user_id": user_id,
                "query": query,
                "session_id": session_id,
                "memory_type": memory_type,
                "top_k": top_k,
            },
        )["entries"]

    def delete_memory(self, memory_id: str) -> bool:
        """DELETE /v1/memory/{memory_id} - remove one entry, e.g. after a
        user asks to be forgotten or a fact turns out to be wrong."""
        return bool(self._request("DELETE", f"/v1/memory/{memory_id}", auth=True)["deleted"])

    def clear_memory(self, user_id: str, *, session_id: Optional[str] = None) -> int:
        """POST /v1/memory/clear - bulk-wipe a user's memory, or just one
        session of it (e.g. clearing short-term conversational memory at
        session end while leaving that user's durable profile memories
        alone). Returns the number of entries removed."""
        return self._request(
            "POST", "/v1/memory/clear", auth=True,
            json_body={"user_id": user_id, "session_id": session_id},
        )["cleared"]

    # -- MCP tool integration ---------------------------------------------------
    # AOM already speaks MCP to every downstream tool server it proxies to
    # (see the appliance's app/mcp_client.py); these helpers make that
    # protocol visible on the client side too, so a discovered tool can be
    # handed directly to any MCP-compatible agent runtime instead of only
    # ever being called through invoke().

    # -- approvals -----------------------------------------------------------
    def approval(self, approval_id: str) -> dict:
        """GET /v1/agent/approvals/{id} - the status of a call this agent
        raised for human approval. Only the agent that raised it can read
        it."""
        return self._request("GET", f"/v1/agent/approvals/{approval_id}", auth=True)["approval"]

    def invoke_with_approval(
        self,
        tool_id: str,
        arguments: Optional[dict] = None,
        *,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 900.0,
    ) -> dict:
        """Invoke a tool that may require human approval, waiting for the
        decision.

        A tool in the approval tier returns 202 with a pending approval
        rather than a result. This helper does the polling loop and then
        re-invokes with the approval, so integrating code sees one call that
        either returns a result or raises.

        The wait is deliberately explicit rather than hidden inside
        :meth:`invoke`: an agent framework that does not know a call can
        block for minutes will time out somewhere less helpful, so the
        blocking version is the one you have to ask for by name.
        """
        first = self.invoke(tool_id, arguments)
        if first.get("status") != "pending_approval":
            return first

        approval_id = first["approval"]["approval_id"]
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            time.sleep(poll_seconds)
            status = self.approval(approval_id)["status"]
            if status == "approved":
                return self.invoke(tool_id, arguments, approval_id=approval_id)
            if status in ("denied", "expired"):
                raise AOMAuthorizationError(
                    f"The call to '{tool_id}' was {status} by a reviewer", 403, status
                )
        raise AOMError(
            f"Timed out after {timeout_seconds:.0f}s waiting for a decision on '{tool_id}' "
            f"(approval {approval_id} is still pending)"
        )

    # -- knowledge -----------------------------------------------------------
    def search_knowledge(self, query: str, top_k: int = 5, document_id: Optional[str] = None) -> List[dict]:
        """POST /v1/agent/knowledge/search - retrieval over the governed
        knowledge base. Filtered by the caller's role in the same Couchbase
        Search request that ranks by similarity, so a chunk this role may
        not read is never a candidate."""
        body = {"query": query, "top_k": top_k}
        if document_id:
            body["document_id"] = document_id
        return self._request("POST", "/v1/agent/knowledge/search", auth=True, json_body=body)["results"]

    def catalog(self) -> List[dict]:
        """GET /v1/catalog - the full vetted tool catalog (no API key
        required): every tool's id, name, description, input_schema,
        allowed_roles and trust status. `discover()` alone doesn't carry
        `input_schema`; this is how `discover_mcp_tools()` fills it in."""
        return self._request("GET", "/v1/catalog")["tools"]

    def discover_mcp_tools(self, query: str, top_k: int = 5) -> List[dict]:
        """Like `discover()`, but returns each matched tool already
        converted to a standard MCP tool definition
        (``{"name", "description", "inputSchema"}``) - ready to hand to any
        MCP-compatible agent runtime or tool-calling API without writing
        the conversion yourself. `name` is the AOM tool_id; pass it
        straight to `invoke()` (or its alias `invoke_mcp_tool()`)."""
        discovered = self.discover(query, top_k=top_k)
        matched_ids = [t["tool_id"] for t in discovered.get("tools", [])]
        if not matched_ids:
            return []
        catalog_by_id = {t["tool_id"]: t for t in self.catalog()}
        return [to_mcp_tool(catalog_by_id[tid]) for tid in matched_ids if tid in catalog_by_id]

    def invoke_mcp_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        """Alias for `invoke()` using MCP tool-call terminology - `name`
        here is the AOM tool_id, which doubles as the MCP tool name
        returned by `discover_mcp_tools()`."""
        return self.invoke(name, arguments=arguments)


class AgentRun:
    """The context manager returned by :meth:`AOMClient.run`.

    Everything the agent does inside the block shares one trace ID, so the
    appliance records it as a single run: the discovery, each tool call and
    its result, each completion, and any memory recall, in the order they
    happened. That is the difference between a trace you can debug from and
    a pile of unrelated single-call traces.
    """

    def __init__(self, client: "AOMClient", name: Optional[str] = None, trace_id: Optional[str] = None) -> None:
        self._client = client
        self.name = name
        self.trace_id = trace_id or uuid.uuid4().hex

    def __enter__(self) -> "AgentRun":
        if self._client._trace_id is not None:
            raise AOMError(
                "This client is already inside a run() block. Nested runs would describe a call "
                "structure the agent does not actually have - close the outer run first, or use a "
                "second AOMClient for genuinely concurrent work."
            )
        self._client._trace_id = self.trace_id
        return self

    def __exit__(self, exc_type, exc, tb) -> "bool":
        self._client._trace_id = None
        return False

    def __repr__(self) -> str:
        return f"AgentRun(trace_id={self.trace_id!r}, name={self.name!r})"
