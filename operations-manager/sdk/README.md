# Couchbase Agent Operations Manager - Developer SDK

Official Python client for the Couchbase Agent Operations Manager: a thin,
typed wrapper around the appliance's REST gateway so your agent code never
hand-rolls bearer-token headers or JSON payloads for `discover` / `invoke` /
`complete`.

Get the appliance's own overview and architecture from the repo README;
this package only covers the client.

## Install

Unzip this package, then from inside the `couchbase-aom-sdk-*` folder:

```bash
pip install .
```

Editable install, for iterating against the SDK source itself:

```bash
pip install -e .
```

Requires Python 3.8+ and `requests`.

## TLS

The bundled Docker Compose stack serves HTTPS everywhere (dashboard and
API) with a self-signed certificate baked in by default. An admin can
install a real one from the appliance's dashboard under Settings -> HTTPS
Certificate, with no file changes needed - see the "HTTPS / TLS" section
of the main `couchbase-agent-operations-manager` repo's README for that
page and the manual (bind-mount) alternative. `AOMClient` verifies
certificates by default (`verify=True`), which rejects the self-signed
default; pass `verify=False` (as the examples below do, via
`AOM_VERIFY_SSL=false`) until a real certificate is installed, or point
`verify` at the exported cert file instead of disabling verification
entirely.

## Getting a credential

Every authenticated call carries a bearer credential, and there are two
kinds:

- **An agent key**, issued from the dashboard under **Settings -> Agent
  Identities**. Each one belongs to an agent record with an owner, a role,
  an optional end date and an optional restriction to a subset of that
  role's tools. Keys are prefixed `aom_` and stored only as a SHA-256, so
  a key that goes missing is *rotated*, not looked up.
- **An OIDC access token** from your own identity provider, where the
  appliance is configured to federate identity. Same field, same header;
  the appliance works out which kind it is.

Both go in `api_key=`. Read it from the environment - never hardcode one:

```python
client = AOMClient(
    base_url=os.environ["AOM_BASE_URL"],
    api_key=os.environ["AOM_API_KEY"],
    agent_id="ticket-triage",      # optional, labels this agent's runs
    session_id=conversation_id,    # optional, groups runs by conversation
)
```

An agent key can be rotated (the previous key keeps working for a grace
window, then expires), expired on a date, or revoked outright - none of
which needs a restart. If calls start failing with 401, the message says
which of those happened, so a revocation never looks like a typo.

The three demo keys a fresh appliance seeds are published in its README
and `.env.example`. They are marked as seeded on the Agent Identities
page; revoke them before the deployment is reachable by anything you care
about.

## Quickstart

```python
from aom_sdk import AOMClient

client = AOMClient(
    base_url="https://localhost:8090",  # your operations-manager origin
    api_key="demo-support-agent-9f21",  # your RBAC role's API key
    verify=False,  # the bundled Docker Compose stack's cert is self-signed by
                   # default - drop this once you've installed a real one
)

# 1. Discover tools for a task - RBAC + vector-search pre-filtered, never a
#    full unfiltered tool dump.
discovered = client.discover("look up a customer's open support tickets")
for tool in discovered["tools"]:
    print(tool["tool_id"], tool["name"])

# 2. Invoke the one you picked - re-checked against Couchbase independently,
#    then proxied to its real MCP server.
result = client.invoke(discovered["tools"][0]["tool_id"], arguments={})
print(result["result"])

# 3. Route model calls through the same gateway to get response caching -
#    a repeat or near-duplicate prompt costs zero tokens.
answer = client.complete("Summarize this ticket thread in two sentences.")
print(answer["response"], answer["cache"]["status"])

# 4. Remember things about this user across sessions.
client.add_memory("user-42", "Prefers responses in metric units.", memory_type="profile")
for m in client.search_memory("user-42", "does this user use metric or imperial?"):
    print(m["content"], m["similarity"])

# 5. Cache your own data-source lookups too, not just LLM output.
order = client.cached_context("order-4821", lambda: fetch_order_from_erp("order-4821"))
```

## Agent memory

Durable, cross-session recall stored in the same Couchbase cluster as
everything else in this appliance - not a separate service to stand up.
`add_memory()` embeds and stores an entry scoped to a `user_id` (and
optionally a `session_id`); `search_memory()` recalls the entries closest
in meaning to a new query, the same vector-search idea `discover()` runs
over the tool catalog. `list_memory()`, `delete_memory()` and
`clear_memory()` round out the CRUD surface. See `examples/agent_memory.py`.

Three conventional `memory_type` values - `conversational` (the default;
what was said in a session), `profile` (durable facts about the user),
and `semantic` (retrieved knowledge worth remembering) - are labels for
your own filtering, not enforced behavior.

## MCP tool integration

AOM already speaks MCP to every downstream tool server it proxies to; this
SDK makes that protocol visible on the client side too:

- `client.discover_mcp_tools(query)` - like `discover()`, but returns each
  matched tool already converted to a standard MCP tool definition
  (`{"name", "description", "inputSchema"}`), ready to hand to any
  MCP-compatible agent runtime or tool-calling API.
- `client.invoke_mcp_tool(name, arguments)` - alias for `invoke()` using
  MCP tool-call terminology.
- `aom_sdk.mcp_server` - an optional bridge (`pip install
  "couchbase-aom-sdk[mcp]"`) that runs this appliance as a real local MCP
  server over stdio, so any MCP host (Claude Desktop, Claude Code, etc.)
  can attach to it directly and reach every tool your API key's role is
  authorized for - still governed by AOM's RBAC and audit trail. Run it
  with:

  ```bash
  AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=demo-support-agent-9f21 \
      AOM_VERIFY_SSL=false python -m aom_sdk.mcp_server
  ```

  See `examples/mcp_tools.py`.

## Why route completions through `complete()` too

Agents tend to ask a small set of questions over and over, reworded every
time, across sessions and users. Every one of those calls is a fresh,
billed round trip to the model provider unless something recognizes the
repeat. `client.complete()` sends the prompt to `/v1/llm/complete` instead
of the provider directly, where it's matched against Couchbase first - by
an exact hash for a byte-for-byte repeat, or by vector similarity for a
paraphrase - and only reaches the provider on a genuine miss. See "Why
route model calls through the SDK too" on the appliance's **Tools ->
Developer SDK** page for the cost/latency math behind this.

## Context caching

`complete()` caches what an LLM *generates*. `context_get()`/`context_set()`
cache what an agent *fetches* from its own data source - a warehouse row,
an API response, a catalog lookup - so a repeat lookup for the same key is
a Couchbase KV read instead of a live round trip to whatever was slow the
first time. Matching is exact-key only (there's no embedding to compare
against for an opaque value you supplied), and the value you get back on a
hit is exactly what you stored:

```python
key = "sku-77213"
hit = client.context_get(key, namespace="procurement-quotes")
if hit["hit"]:
    quote = hit["value"]
else:
    quote = fetch_supplier_quote(key)  # your own slow lookup
    client.context_set(key, quote, namespace="procurement-quotes")
```

`cached_context()` collapses that get-or-compute pattern into one call -
`fetch` only runs on a miss, and the elapsed time of that fetch is reported
back to AOM automatically so its dashboard's "latency avoided" figure
reflects your actual data source, not a guess:

```python
quote = client.cached_context(
    "sku-77213",
    lambda: fetch_supplier_quote("sku-77213"),
    namespace="procurement-quotes",
    ttl_seconds=3600,
)
```

Every agent using either method - any agent, not just this SDK's own
examples - shows up on the appliance's **Context Cache** page (left nav,
under LLM Caching), the same way every `complete()` caller shows up on
**Cache Dashboard**. See `examples/context_caching.py`.

## Grouping calls into a run

Every call is traced. By default each one is its own single-span trace,
which is enough to see it on the appliance's **Traces** page but tells you
nothing about what it belonged to. Wrap the work an agent does for one
task in `run()` and every call inside it shares a trace ID, so the page
shows one run - the discovery, each tool call and its result, each
completion, each memory recall - in the order they happened:

```python
with client.run(name="resolve ticket 4821") as run:
    tools = client.discover("look up a customer's open support tickets")
    result = client.invoke(tools["tools"][0]["tool_id"])
    answer = client.complete("Summarise that ticket in two sentences.")
    print(run.trace_id)
```

Pass `agent_id=` and `session_id=` to `AOMClient(...)` to have every run
labelled with which agent and conversation it belonged to.

## Tools that need a human first

An appliance can put high-risk tools behind human approval. `invoke()`
then returns a pending approval instead of a result:

```python
result = client.invoke("snowflake::manage_users", {"user": "alice"})
if result.get("status") == "pending_approval":
    print(result["approval"]["approval_id"], "is waiting for a reviewer")
```

Poll `client.approval(approval_id)` and re-invoke with
`approval_id=` once it reads `approved`, or let the SDK do it:

```python
result = client.invoke_with_approval("snowflake::manage_users", {"user": "alice"})
```

That blocks until a person decides, so it is opt-in by name rather than
hidden inside `invoke()` - a framework that does not expect a call to take
minutes will time out somewhere less helpful.

An approval is bound to the exact arguments a reviewer saw, is single-use,
and expires into a denial. Re-invoking with different arguments raises
`AOMApprovalError` rather than running.

## Knowledge retrieval

If the appliance has a knowledge base, retrieve from it with the same
role-scoped guarantee discovery gives for tools - a chunk outside your
role is never a candidate, however well it matches:

```python
for chunk in client.search_knowledge("what is our refund window?"):
    print(chunk["document_title"], chunk["score"])
    print(chunk["content"])
```

This replaces a separate RAG stack for knowledge that should be governed
the same way everything else the agent touches is.

## Error handling

Every non-2xx response raises a typed exception from `aom_sdk`:

| Exception | Raised on |
|---|---|
| `AOMConnectionError` | Could not reach the operations manager at all |
| `AOMAuthenticationError` | 401 - missing or invalid API key |
| `AOMAuthorizationError` | 403 - role not authorized for that tool |
| `AOMNotFoundError` | 404 - unknown tool/server/entry |
| `AOMServerError` | 5xx - operations manager or downstream MCP server failed |
| `AOMError` | Base class; any other 4xx |

```python
from aom_sdk import AOMClient, AOMAuthorizationError

client = AOMClient("https://localhost:8090", api_key="demo-support-agent-9f21", verify=False)
try:
    client.invoke("billing-service::refund_customer", arguments={"order_id": "123"})
except AOMAuthorizationError as exc:
    print(f"Not authorized: {exc}")
```

## Limits, budgets and guardrails

The appliance can bound what a caller consumes and refuse payloads that
carry personal data or an injection signal. Both surface as typed
exceptions, and both are worth handling explicitly because the right
response differs:

```python
from aom_sdk import AOMGuardrailError, AOMRateLimitError, AOMScopeError

try:
    answer = client.complete(prompt)
except AOMRateLimitError as exc:
    # exc.limit names which ceiling was hit. Backing off makes sense for a
    # per-minute rate; a daily spend budget will not clear for hours, so
    # that one is worth surfacing rather than sleeping on.
    if exc.limit in ("requests", "tool_calls") and exc.retry_after:
        time.sleep(exc.retry_after)
    else:
        raise
except AOMGuardrailError:
    # A policy decision, not a bug. Retrying the same payload will not help.
    ...
except AOMScopeError:
    # This agent was narrowed to a subset of its role's tools.
    ...
```

Note that a prompt or answer containing personal data is never cached -
the appliance bypasses the cache rather than storing a redacted copy,
because a cache is only useful while a hit and a miss return the same
answer. `answer["cache"]["reason"]` says so when it happens.

## Examples

The bundled `examples/` directory runs against the appliance's own sample
servers, so each one works on a fresh install with no real credentials:

| Example | What it shows |
|---|---|
| `quickstart.py` | Discover, invoke, complete, remember - the whole gateway in one file. |
| `traced_run.py` | Grouping an agent's work into one traced run. |
| `approvals.py` | A tool held for human approval, polled and then executed. |
| `knowledge_and_guardrails.py` | Role-filtered retrieval, and what a refused payload looks like. |
| `agent_memory.py` | Durable, semantic recall across sessions. |
| `llm_caching.py` | What the response cache saves at agent-fleet scale. |
| `context_caching.py` | Caching an agent's own data-source lookups with `context_get`/`context_set`/`cached_context`. |
| `mcp_tools.py` | Using AOM's catalog as MCP tool definitions, or as an MCP server. |

```bash
AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=demo-support-agent-9f21 \
    AOM_VERIFY_SSL=false python examples/quickstart.py
```

## Full API reference

This SDK wraps a deliberate subset of the appliance's REST API. The
gateway's own OpenAPI docs (`/docs` on the operations-manager origin) and
the appliance's repo README are the source of truth for every endpoint,
including the admin surface (server registration, roles, audit log, cache
administration) that most agent code never needs.

## License

MIT - see the repository's [LICENSE](../../LICENSE).
