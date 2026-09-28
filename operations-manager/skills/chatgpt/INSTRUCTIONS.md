# Couchbase Agent Operations Manager SDK integration

You are helping integrate an agent codebase with a Couchbase Agent
Operations Manager (AOM) appliance - the RBAC + vector-search gateway that
sits between an agent and its MCP tool servers - using `aom_sdk`, the
appliance's official Python client. Apply this whether the codebase is
brand new or already has hand-written HTTP calls against the appliance's
REST API that should be replaced.

## When to apply this

- You're asked to connect an agent to AOM / the Operations Manager, add
  tool discovery, or wire up the Couchbase agent gateway.
- You find raw HTTP calls to endpoints under `/v1/tools/`, `/v1/llm/`,
  `/v1/memory/`, or `/v1/agent/` on an AOM appliance and the codebase has no `aom_sdk`
  dependency yet - replace them with the SDK rather than leaving both
  patterns in the same codebase.
- A new agent project needs to call external tools, cache LLM
  completions, remember things about a user, or retrieve from a governed
  knowledge base, with an AOM appliance as the backend.

## Step 1 - get the SDK

`couchbase-aom-sdk` is not published to a public package index; it ships
from the appliance itself. Get it one of two ways:

- The appliance's dashboard: **Tools -> Developer SDK -> Download SDK**.
- Directly: `curl -k -o aom-sdk.zip https://<appliance-host>:8090/v1/sdk/download` (`-k` skips verifying the appliance's certificate - it's self-signed by default; drop it once a real one is installed)
  (default port `8090`; adjust the host for the target deployment).

Then, from the project that will depend on it:

```bash
unzip aom-sdk.zip -d /tmp/aom-sdk
pip install /tmp/aom-sdk/couchbase-aom-sdk-*
```

For a lockfile-based workflow (Poetry, uv, pip-tools), vendor the unzipped
SDK folder into the repo (e.g. `vendor/aom-sdk/`) as a local/path
dependency instead of leaving it in `/tmp`, so the build is reproducible
in CI.

Need the SDK to speak MCP directly (Step 4)? Install the optional extra:
`pip install "/tmp/aom-sdk/couchbase-aom-sdk-*[mcp]"`.

## Step 2 - configure a client

Every call needs the appliance's base URL and (for anything but
`health()`/`roles()`/`catalog()`) a bearer credential - either an agent key issued from **Settings ->
Agent Identities** (prefixed `aom_`), or an OIDC access token from the
organisation's own provider where the appliance federates identity. Both
go in the same field.
Read both from environment variables - never hardcode a key:

```python
import os
from aom_sdk import AOMClient

client = AOMClient(
    base_url=os.environ["AOM_BASE_URL"],   # e.g. "https://localhost:8090"
    api_key=os.environ.get("AOM_API_KEY"),  # an aom_ agent key, or an OIDC token
    # The appliance serves HTTPS with a self-signed certificate by default -
    # this reads AOM_VERIFY_SSL (default "false") the same way the bundled
    # SDK examples do; set it to "true" once a real certificate is installed.
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    # Both optional, both worth setting: they label this agent's runs on
    # the appliance's Traces page.
    agent_id="<short name for this agent>",
    session_id=conversation_id,
)
```

Agent keys can be rotated, expired and revoked from the dashboard, and
are stored only as a hash. If calls start failing with 401, read the
message - it says whether the key was revoked, expired, or rotated past
its grace window.

Add `AOM_BASE_URL`/`AOM_API_KEY` to whatever config system the codebase
already uses (Pydantic settings, a `.env` loader, etc.) rather than
introducing a new one.

## Step 3 - replace tool calling with discover/invoke

Replace any hardcoded tool registry, raw MCP client, or direct tool-server
HTTP calls with the gateway pattern: discover, then invoke.

```python
discovered = client.discover("look up a customer's open support tickets")
tool = discovered["tools"][0]
result = client.invoke(tool["tool_id"], arguments={})
```

Check `result["hijack_warning"]` before trusting `result["result"]` in
anything user-facing - a non-null value means the appliance's hijack
detector flagged the live tool response.

## Step 4 - or bridge as a real MCP server

If the target framework already speaks MCP by URL/command rather than
calling a Python client directly, don't write a custom adapter - point it
at the bundled bridge:

```bash
pip install "couchbase-aom-sdk[mcp]"
AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=<role-api-key> \
    AOM_VERIFY_SSL=false python -m aom_sdk.mcp_server
```

This runs AOM as a local MCP server over stdio: `list_tools` returns the
caller's authorized catalog, `call_tool` invokes through AOM's RBAC and
audit trail. If only the tool *definitions* are needed in MCP shape (e.g.
for an OpenAI-style function-calling API, without running a server), use
`client.discover_mcp_tools(query)` instead - it returns
`{"name", "description", "inputSchema"}` dicts directly, ready to pass as
`tools=[...]` to a Chat Completions/Responses call.

## Step 5 - group each task into a traced run

Every call is traced. Left alone, each is its own single-span trace -
which shows up, but says nothing about what it belonged to. Wrap the work
the agent does for one task so the appliance records it as one run:

```python
with client.run(name="resolve ticket 4821"):
    tools = client.discover(...)
    result = client.invoke(...)
    answer = client.complete(...)
```

Put this at whatever boundary the codebase already treats as "one task" -
a request handler, a graph invocation, a queue consumer - rather than
inventing a new one.

## Step 6 - cache LLM completions

Route repeatable completions (support-style Q&A, summarization, templated
prompts) through the gateway instead of the provider directly, so a
repeat or near-duplicate prompt costs zero tokens:

```python
answer = client.complete("Summarize this ticket thread in two sentences.")
print(answer["response"], answer["cache"]["status"])  # hit_exact/hit_semantic/miss/bypass
```

Use `bypass_cache=True` for prompts that must always reach the live model.

## Step 7 - add agent memory where the codebase tracks user/session state

Replace ad hoc "what does this user prefer" / "what happened earlier"
state (a dict, a database table, a home-grown cache) with AOM's memory API
for durable, semantic recall:

```python
client.add_memory(user_id, "Prefers responses in metric units.", memory_type="profile")
client.add_memory(user_id, "Asked about a damaged order.", session_id=session_id)

relevant = client.search_memory(user_id, "what did they say about their order?")
```

Don't migrate state that's already correctly modeled elsewhere.

## Step 8 - replace a bolted-on RAG stack with governed retrieval

If the codebase has its own vector store purely to answer questions from
the organisation's documents, and those documents should be visible to
some roles and not others, `search_knowledge()` does that with the same
role pre-filter discovery uses - a chunk outside the caller's role is
never a candidate, however well it matches:

```python
for chunk in client.search_knowledge("what is our refund window?"):
    context += f"{chunk['document_title']}: {chunk['content']}\n"
```

Only suggest this where the governance actually matters. A public docs
search with no access rules gains nothing from moving.

## Step 9 - handle the refusals this gateway can return

A governed gateway refuses things an unguarded HTTP client never would,
and each refusal wants a different response:

```python
from aom_sdk import AOMApprovalError, AOMGuardrailError, AOMRateLimitError, AOMScopeError

try:
    result = client.invoke(tool_id, arguments)
except AOMRateLimitError as exc:
    # exc.limit says which ceiling. Backing off is right for a per-minute
    # rate; a daily spend budget will not clear for hours.
    ...
except AOMGuardrailError:
    # Personal data or an injection signal in the payload. A policy
    # decision, not a bug - retrying the same payload will not help.
    ...
except AOMScopeError:
    # This agent is narrowed to a subset of its role's tools.
    ...
```

**Approvals.** A high-risk tool may be held for a human. `invoke()` then
returns `{"status": "pending_approval", "approval": {...}}` rather than a
result - not an error, and code that assumes a result will silently treat
the approval envelope as one. Either check for it, or use
`invoke_with_approval()`, which polls and then executes. Prefer the
explicit check in anything event-driven; `invoke_with_approval()` blocks
for as long as a person takes.

## Error handling

Use the SDK's typed exceptions rather than checking HTTP status codes:
`AOMConnectionError`, `AOMAuthenticationError`, `AOMAuthorizationError`,
`AOMNotFoundError`, `AOMServerError`, and `AOMError` as the base class.
Match the codebase's existing error-handling style when wrapping these.

## After integrating

- Document `AOM_BASE_URL`/`AOM_API_KEY` wherever the project documents
  setup (README, `.env.example`, etc.).
- Add or update a test that exercises the new `AOMClient` usage against a
  mock/fake, not a live appliance.
- Never commit a real API key - replace any hardcoded key used during
  exploration with an environment variable read before finishing.
