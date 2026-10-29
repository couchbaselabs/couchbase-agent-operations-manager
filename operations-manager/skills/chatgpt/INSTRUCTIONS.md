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
- A data pipeline, ETL job or CI step should load documents or warehouse
  exports (Snowflake, Databricks, BigQuery) into the appliance's Knowledge
  Base, or keep a table there fresh on a schedule - that is Step 10.

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
`health()`) a bearer credential - either an agent key issued from **Settings ->
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
    # Optional: pin the LLM every complete() from this client uses. Leave
    # both unset to let the appliance's policy decide - the operator's
    # global default, or a per-role / per-agent override assigned to this
    # key. `model` without `provider` is rejected.
    provider=os.environ.get("AOM_LLM_PROVIDER"),   # "anthropic" | "openai" | "google"
    model=os.environ.get("AOM_LLM_MODEL"),
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
result = client.invoke(tool["tool_id"], arguments={"customer_id": customer_id})
```

Always pass the arguments the tool's `input_schema` requires. `discover()`
returns ids and descriptions only; `client.catalog()` returns the same
tools *with* their schemas, so join the two when the codebase needs to
build arguments programmatically (`examples/all_in_one.py` in the SDK
shows this). An invoke with `{}` passes authorization at the gateway and
then fails inside the downstream MCP server - the SDK reports that as a
502 `AOMServerError` whose message carries the downstream cause (a 401
from the tool server, a connection refusal, a validation error), so read
it rather than retrying.

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
print(answer["provider"], answer["model"], answer["policy"])  # what actually served it
```

Which provider and model answer is decided, in precedence order, by the
call (`complete(..., provider=, model=)`), then the client defaults
(`AOMClient(provider=, model=)`), then the appliance's policy: the
operator's global default, or a per-role / per-agent-identity override
assigned to this key on **LLM Caching -> Providers & policy**. Prefer
leaving it to the policy unless the codebase has a real reason to pin a
model - that keeps the choice in the operator's hands and visible on the
dashboard's "Savings by provider & model" table. `answer["policy"]` names
the override that applied, or is `None` under the global policy.

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

## Step 10 - load the knowledge base from a pipeline (operator side)

Steps 1-9 are the agent side, authenticated with an agent key. Putting
documents *in* is an operator decision - who may read a document is fixed
at upload time - so it sits behind the dashboard login, and agent keys
cannot do it. When the codebase is a data pipeline, an ETL job or a CI
step that should feed the appliance (typically a Snowflake, Databricks or
BigQuery export), use `AOMAdminClient` from the same package:

```python
from aom_sdk import AOMAdminClient

admin = AOMAdminClient(os.environ["AOM_DASHBOARD_URL"],      # the dashboard origin, e.g. https://host:3111
                       username=os.environ["AOM_USERNAME"],
                       password=os.environ["AOM_PASSWORD"])

# stream a local export (up to 5 GB by default; nothing buffered in memory)
job = admin.upload_knowledge("orders_0_0_0.csv.gz", allowed_roles=["finance_analyst"],
                             dump={"source": "snowflake", "table_name": "SALES.PUBLIC.ORDERS"})
admin.wait_for_job(job["job_id"])

# or let the appliance pull from s3:// gs:// az:// https:// (credential used once, never stored)
job = admin.import_knowledge("gs://exports/orders.parquet", allowed_roles=["finance_analyst"],
                             auth={"service_account_json": sa_json}, dump={"source": "bigquery"})
```

Rules of thumb:

- Point it at the **dashboard** origin (port 3111 on the bundled stack),
  not the API port: that is where the session cookie is set and where
  nginx allows unbuffered multi-gigabyte uploads. The inline
  `POST /v1/knowledge` JSON path caps at 20 MB - never base64 a large
  file through it.
- Pass `dump=` for table exports (CSV/TSV, JSON/NDJSON, Parquet, Avro,
  plain or gzip/zip) so rows are chunked with the schema attached; omit
  it for documents (text, Markdown, PDF).
- Every call returns an *ingestion job*; `wait_for_job()` polls it and
  raises `AOMError` with the job's own error text if it fails. Do not
  treat the 202 as success.
- If the pipeline exists only to export a table on a schedule, replace it
  with a **warehouse sync** (`admin.create_warehouse_sync({...})` with a
  `cron` or `interval` schedule, `mode: "replace"` or `"snapshot"`): the
  appliance then runs the query itself. Call
  `admin.test_warehouse_sync(vendor, connection, query)` first - it
  authenticates and previews 10 rows before anything is saved.
- `AOM_PASSWORD` is a dashboard credential: read it from the environment
  or a secret store, never commit it, and prefer a dedicated local
  account for pipelines.
- `admin.roles()` / `create_role()` exist for pipelines that provision
  roles alongside documents. Roles are admin-managed now (Roles & RBAC
  page), so do not hardcode the built-in list in the codebase - read it.

For a one-off load from a shell, the appliance repo ships
`scripts/kb-upload.py`, which does the same thing without writing code.

## Error handling

Use the SDK's typed exceptions rather than checking HTTP status codes:
`AOMConnectionError`, `AOMAuthenticationError`, `AOMAuthorizationError`,
`AOMNotFoundError`, `AOMServerError`, and `AOMError` as the base class.
`AOMAdminClient` raises the same family. Match the codebase's existing error-handling style when wrapping these.

## After integrating

- Document `AOM_BASE_URL`/`AOM_API_KEY` wherever the project documents
  setup (README, `.env.example`, etc.).
- Add or update a test that exercises the new `AOMClient` usage against a
  mock/fake, not a live appliance.
- Never commit a real API key - replace any hardcoded key used during
  exploration with an environment variable read before finishing.
