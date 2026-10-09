// Generated from the agent scripts these examples were written and tested as -
// keep the Python valid if you edit it here (copy it out and run `python -m py_compile`).

export type CacheLayer = { layer: string; what: string; key: string; ttl: string };
export type SetupStep = {
  title: string;
  // Inline `code` spans in backticks are rendered as <code>.
  body: string;
  fields?: Array<[string, string]>;
  code?: string;
};

export type AgentSetup = { title: string; intro: string; steps: SetupStep[] };

export type AgentExample = {
  id: string;
  label: string;
  filename: string;
  tagline: string;
  install: string;
  // [variable, example value, what to supply]
  env: Array<[string, string, string]>;
  cache: CacheLayer[];
  setup?: AgentSetup;
  code: string;
};

export const AGENT_EXAMPLES: AgentExample[] = [
  {
    id: "snowflake",
    label: "Snowflake Agent",
    filename: "snowflake_agent.py",
    tagline: "Natural-language finance analytics over Snowflake, where every warehouse query is an AOM-governed MCP tool call to your Snowflake MCP server.",
    install: "pip install ./couchbase-aom-sdk-*      # the zip from Tools → Developer SDK",
    env: [
      ["AOM_BASE_URL", "https://aom.example.com:8090", "Your AOM operations-manager URL - port 8090 by default. On Helm/Kubernetes installs, where that port isn't exposed, use your dashboard URL instead."],
      ["AOM_API_KEY", "aom_…", "The Agent Identity key from step 4."],
      ["AOM_VERIFY_SSL", "true", "`true` once AOM has a trusted certificate; `false` while it's self-signed."],
      ["SNOWFLAKE_MCP_SERVER", "snowflake-prod", "The Server ID you registered in step 2."],
      ["SNOWFLAKE_QUERY_TOOL", "run_query", "Your query tool's name, from Tool Catalog."],
      ["SNOWFLAKE_SQL_ARG", "sql", "That tool's SQL argument name."],
      ["SNOWFLAKE_LIST_TOOL", "list_tables", "Your table-listing tool, or leave unset if there isn't one."],
      ["SNOWFLAKE_WAREHOUSE", "ANALYTICS_WH", "Optional - sent as a `warehouse` argument only if set."],
    ],
    cache: [
      { layer: "Context", what: "Table list", key: "tables:<server_id>", ttl: "6 h" },
      { layer: "Context", what: "Query result", key: "sql:<server_id>:<sha256 of normalized SQL>", ttl: "15 min" },
      { layer: "LLM · semantic", what: "Question → SQL", key: "namespace snowflake-analytics:nl2sql", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "Final answer (embeds rows)", key: "namespace snowflake-analytics:answer", ttl: "policy TTL" },
    ],
    setup: {
      "title": "Use your own Snowflake MCP server",
      "intro": "The agent only ever talks to AOM. AOM calls your MCP server, and your MCP server holds the Snowflake credentials. Steps 2-4 need an admin login to this dashboard - server registration and Agent Identities can't be done with an API key.",
      "steps": [
        {
          "title": "Run your MCP server where AOM can reach it",
          "body": "AOM connects to MCP servers over Streamable HTTP only - a stdio-only server needs an HTTP transport or a stdio-to-HTTP bridge in front of it. The URL has to be reachable from the operations manager, not just from your laptop: on Kubernetes, use the Service's in-cluster name, e.g. `http://<service>.<namespace>.svc.cluster.local:<port>/mcp`. Give the server's Snowflake user a role with read-only grants on only the warehouse and databases this agent needs - AOM decides who may call a tool, and Snowflake still decides what that tool can read."
        },
        {
          "title": "Register it in AOM",
          "body": "MCP Servers → Register server, then Register & ingest. Tools are pulled into the catalog as soon as a trusted server is registered.",
          "fields": [
            [
              "Server ID",
              "Anything except `snowflake`, e.g. `snowflake-prod`. AOM's bundled sample Snowflake server already uses `snowflake` and is re-created on every restart. This ID becomes the prefix of every tool ID."
            ],
            [
              "MCP URL",
              "Your Streamable HTTP endpoint from step 1."
            ],
            [
              "Trust status",
              "`trusted` - an untrusted server is registered but its tools aren't ingested."
            ],
            [
              "Default allowed roles",
              "`finance_analyst` and `admin`. Roles are fixed in AOM: `support_agent`, `finance_analyst`, `admin`."
            ],
            [
              "Authentication",
              "How AOM authenticates to your server: none, bearer token, custom header or basic. The credential is encrypted at rest and never shown again."
            ]
          ]
        },
        {
          "title": "Review the ingested tools",
          "body": "Open Tool Catalog and note the exact tool IDs, e.g. `snowflake-prod::run_query`, plus the query tool's SQL argument name - the script needs both. Tools registered through the form get the server's default roles with risk level `unclassified`, which Insights flags for review, and a tool whose description looks like a prompt-injection attempt can be quarantined automatically. To set reviewed per-tool risk levels up front, register through `POST /v1/servers` on API Documentation (while signed in as admin) and include `tool_policies`. If the server also exposes write or admin tools, mark them `critical` and turn on Approvals so a person signs off before they run.",
          "code": "\"tool_policies\": {\n  \"run_query\":   {\"allowed_roles\": [\"finance_analyst\", \"admin\"], \"risk_level\": \"low\"},\n  \"list_tables\": {\"allowed_roles\": [\"finance_analyst\", \"admin\"], \"risk_level\": \"low\"}\n}"
        },
        {
          "title": "Issue an Agent Identity",
          "body": "Settings → Agent Identities → Issue agent. Recommended rather than strictly required - any key whose role can call the tools will work - but a per-agent key attributes every call on Traces and Audit Log to this agent, can be limited to just these tools, and can be rotated or revoked without affecting anyone else. Copy the key when it's shown: it's stored only as a hash and can't be displayed again.",
          "fields": [
            [
              "Role",
              "`finance_analyst`"
            ],
            [
              "Restrict to specific tools",
              "The query and table-listing tools from step 3, so this agent can't call anything else its role allows."
            ],
            [
              "Expires",
              "Optional - set one for anything short-lived."
            ]
          ]
        },
        {
          "title": "Point the script at it",
          "body": "Set these alongside `AOM_BASE_URL`. Leave `SNOWFLAKE_LIST_TOOL` unset if your server has no table-listing tool - the model is then told to use fully-qualified table names. The script accepts query results shaped as a list of rows or as `rows` / `data` / `results` in an object.",
          "code": "export AOM_API_KEY=aom_...                  # the Agent Identity key from step 4\nexport SNOWFLAKE_MCP_SERVER=snowflake-prod  # Server ID from step 2\nexport SNOWFLAKE_QUERY_TOOL=run_query       # tool names from step 3\nexport SNOWFLAKE_SQL_ARG=sql\nexport SNOWFLAKE_LIST_TOOL=list_tables\npython snowflake_agent.py \"What was daily revenue over the last week?\""
        },
        {
          "title": "Check the appliance settings it relies on",
          "body": "Providers & Policy needs a live LLM provider key - without one, `complete()` answers from an offline stub and the generated SQL won't be real. Optionally, cap this agent's call rate and spend under Limits & Budgets, and review Guardrails & PII for what warehouse results are allowed to contain."
        },
        {
          "title": "Verify",
          "body": "Run the same question twice. The first run appears as one trace on Traces; the second should come back from Context Cache and LLM Cache with no tool calls at all. If the script reports the tool isn't in the catalog, the Server ID or tool name doesn't match Tool Catalog; if it reports the role or scope can't call it, fix the server's allowed roles or the Agent Identity's tool list."
        }
      ]
    },
    code: `"""
Snowflake analytics agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers natural-language finance questions against Snowflake. The agent
never connects to Snowflake itself: every warehouse call is an AOM-governed
MCP tool call (RBAC, agent scope, audit, guardrails) to your Snowflake MCP
server, which holds the Snowflake credentials. Register the server in AOM
(MCP Servers), issue an Agent Identity scoped to its tools, then:

    pip install ./couchbase-aom-sdk-*           # from Tools -> Developer SDK
    export AOM_BASE_URL=https://aom.example.com:8090
    export AOM_API_KEY=aom_...                  # the Agent Identity's key
    export AOM_VERIFY_SSL=true                  # false while AOM's cert is self-signed
    export SNOWFLAKE_MCP_SERVER=snowflake-prod  # the Server ID you registered
    export SNOWFLAKE_QUERY_TOOL=run_query       # tool names as shown in Tool Catalog
    export SNOWFLAKE_SQL_ARG=sql                # that tool's SQL argument name
    export SNOWFLAKE_LIST_TOOL=list_tables      # or leave unset if your server has none
    python snowflake_agent.py "What was daily revenue over the last week?"

Caching layers:
  1. Context Cache  - table list (6 h) and each query result (15 min), keyed
                      by a hash of the normalized SQL.
  2. LLM Cache      - NL -> SQL with semantic matching, so a reworded
                      question reuses the SQL already written for it.
  3. LLM Cache      - the final answer, exact-match only (it embeds data).
"""
import hashlib
import json
import os
import re
import sys

from aom_sdk import (
    AOMAuthorizationError,
    AOMClient,
    AOMGuardrailError,
    AOMNotFoundError,
    AOMRateLimitError,
    AOMScopeError,
)

NAMESPACE = "snowflake-analytics"
MCP_SERVER = os.environ["SNOWFLAKE_MCP_SERVER"]                  # AOM Server ID
QUERY_TOOL = os.environ["SNOWFLAKE_QUERY_TOOL"]
SQL_ARG = os.environ.get("SNOWFLAKE_SQL_ARG", "sql")
LIST_TOOL = os.environ.get("SNOWFLAKE_LIST_TOOL", "")               # unset/empty = skip
WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "")  # sent as \`warehouse\` only if set
SCHEMA_TTL_S = 6 * 3600     # table lists change rarely
RESULT_TTL_S = 15 * 60      # analytics results go stale - keep this short
MAX_ROWS = 200              # keep cached values under the policy's max_value_bytes (64 KB default)

client = AOMClient(
    base_url=os.environ["AOM_BASE_URL"],
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="snowflake-analytics-agent",
)


def tool_id(name: str) -> str:
    # AOM tool IDs are "<server_id>::<tool_name>".
    return f"{MCP_SERVER}::{name}"


def sql_key(sql: str) -> str:
    normalized = re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()
    return f"sql:{MCP_SERVER}:" + hashlib.sha256(normalized.encode()).hexdigest()[:32]


def rows_of(result) -> list:
    """MCP servers shape query results differently - accept the common ones."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("rows", "data", "results", "result"):
            if isinstance(result.get(key), list):
                return result[key]
    return [result]


def strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^\`\`\`[a-zA-Z]*\s*", "", text)
    return re.sub(r"\s*\`\`\`$", "", text).strip()


def assert_read_only(sql: str) -> None:
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE) or ";" in sql.strip().rstrip(";"):
        raise ValueError(f"Refusing to run a non-SELECT or multi-statement query: {sql!r}")


def list_tables() -> list:
    if not LIST_TOOL:
        return []

    def fetch() -> list:
        result = client.invoke(tool_id(LIST_TOOL), {})["result"]
        tables = result["tables"] if isinstance(result, dict) and "tables" in result else rows_of(result)
        return [t if isinstance(t, str) else json.dumps(t, default=str) for t in tables][:500]

    # Context Cache: one governed tool call every 6 hours instead of one per question.
    return client.cached_context(f"tables:{MCP_SERVER}", fetch, namespace=NAMESPACE, ttl_seconds=SCHEMA_TTL_S)


def question_to_sql(question: str, tables: list) -> str:
    # LLM Cache with semantic matching ON: "revenue by day last week" and
    # "daily revenue for the past 7 days" resolve to the same cached SQL.
    # Keep the prompt question-led - a long shared preamble makes every
    # prompt look alike. If your questions differ mainly by literal values
    # (dates, IDs), pass semantic=False here too.
    hint = f"Available tables: {', '.join(tables)}. " if tables else "Use fully-qualified table names. "
    resp = client.complete(
        f"Question: {question}\n"
        f"Write ONE read-only Snowflake SQL query that answers it. {hint}Return only the SQL.",
        namespace=f"{NAMESPACE}:nl2sql",
    )
    sql = strip_fences(resp["response"])
    assert_read_only(sql)
    return sql


def run_query(sql: str) -> dict:
    # Context Cache: identical SQL from any agent on this appliance is a KV get.
    def fetch() -> dict:
        args = {SQL_ARG: sql}
        if WAREHOUSE:
            args["warehouse"] = WAREHOUSE
        result = client.invoke(tool_id(QUERY_TOOL), args)["result"]
        return {"rows": json.loads(json.dumps(rows_of(result)[:MAX_ROWS], default=str))}

    return client.cached_context(sql_key(sql), fetch, namespace=NAMESPACE, ttl_seconds=RESULT_TTL_S)


def answer(question: str, sql: str, result: dict) -> dict:
    # LLM Cache, exact-match only: the prompt embeds live data, so a
    # near-identical prompt could carry different numbers.
    return client.complete(
        f"Question: {question}\nSQL: {sql}\n"
        f"Rows (JSON): {json.dumps(result['rows'], default=str)}\n"
        "Answer in 2-3 sentences with the key figures.",
        namespace=f"{NAMESPACE}:answer",
        semantic=False,
    )


def main(question: str) -> None:
    with client.run(name=f"snowflake: {question[:60]}") as run:
        try:
            tables = list_tables()
            sql = question_to_sql(question, tables)
            result = run_query(sql)
            final = answer(question, sql, result)
        except AOMNotFoundError:
            sys.exit(f"No '{tool_id(QUERY_TOOL)}' in the Tool Catalog - check SNOWFLAKE_MCP_SERVER / *_TOOL.")
        except AOMScopeError:
            sys.exit("This Agent Identity isn't scoped to that tool - add it under Settings -> Agent Identities.")
        except AOMAuthorizationError:
            sys.exit(f"This key's role can't call {tool_id(QUERY_TOOL)} - grant the role on the server, or use another key.")
        except AOMGuardrailError as exc:
            sys.exit(f"Blocked by AOM guardrails: {exc}")
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\nSQL: {sql}")
        print(f"LLM cache: {final['cache']['status']} | cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "What was daily revenue over the last week?")
`,
  },
  {
    id: "databricks",
    label: "Databricks Agent",
    filename: "databricks_agent.py",
    tagline: "Questions over Unity Catalog tables - through AOM-governed MCP tool calls to your Databricks MCP server, or a direct SQL warehouse connection - answered by a Databricks-served model through AOM's LLM gateway.",
    install: "pip install ./couchbase-aom-sdk-*         # both modes\npip install databricks-sql-connector      # direct mode only",
    env: [
      ["AOM_BASE_URL", "https://aom.example.com:8090", "Your AOM operations-manager URL - port 8090 by default. On Helm/Kubernetes installs, where that port isn't exposed, use your dashboard URL instead."],
      ["AOM_API_KEY", "aom_…", "The Agent Identity key from step 4."],
      ["AOM_VERIFY_SSL", "true", "`true` once AOM has a trusted certificate; `false` while it's self-signed."],
      ["DATABRICKS_TABLES", "main.sales.orders,main.sales.customers", "Comma-separated tables the agent may query."],
      ["DATABRICKS_MCP_SERVER", "databricks-prod", "Governed: the Server ID from step 2. Leave unset for direct mode."],
      ["DATABRICKS_QUERY_TOOL", "execute_sql", "Governed: your SQL tool's name, from Tool Catalog."],
      ["DATABRICKS_SQL_ARG", "sql", "Governed: that tool's SQL argument name."],
      ["DATABRICKS_SERVER_HOSTNAME", "adb-1234567890123456.7.azuredatabricks.net", "Direct: your workspace hostname."],
      ["DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc123def456", "Direct: your SQL warehouse's HTTP path."],
      ["DATABRICKS_TOKEN", "dapi…", "Direct: a token for a principal with read access to those tables."],
    ],
    cache: [
      { layer: "Context", what: "Table schema (DESCRIBE TABLE)", key: "schema:<source>:<catalog.schema.table>", ttl: "6 h" },
      { layer: "Context", what: "Query result", key: "sql:<source>:<sha256 of normalized SQL>", ttl: "15 min" },
      { layer: "LLM · semantic", what: "Question → SQL", key: "namespace databricks-lakehouse:nl2sql", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "Final answer (embeds rows)", key: "namespace databricks-lakehouse:answer", ttl: "policy TTL" },
    ],
    setup: {
      "title": "Use your own Databricks MCP server",
      "intro": "In governed mode the agent only ever talks to AOM. AOM calls your MCP server, and your MCP server holds the Databricks credentials. Steps 2-4 need an admin login to this dashboard - server registration and Agent Identities can't be done with an API key.",
      "steps": [
        {
          "title": "Run your MCP server where AOM can reach it",
          "body": "AOM connects to MCP servers over Streamable HTTP only - a stdio-only server needs an HTTP transport or a stdio-to-HTTP bridge in front of it. The URL has to be reachable from the operations manager, not just from your laptop: on Kubernetes, use the Service's in-cluster name, e.g. `http://<service>.<namespace>.svc.cluster.local:<port>/mcp`. Give the server a service principal or token with `USE CATALOG`, `USE SCHEMA` and `SELECT` on only the catalogs and schemas this agent needs. AOM decides who may call a tool; Databricks still decides what that tool can reach."
        },
        {
          "title": "Register it in AOM",
          "body": "MCP Servers → Register server, then Register & ingest. Tools are pulled into the catalog as soon as a trusted server is registered.",
          "fields": [
            [
              "Server ID",
              "A short, stable ID such as `databricks-prod`. It becomes the prefix of every tool ID, and the script's `DATABRICKS_MCP_SERVER` must match it."
            ],
            [
              "MCP URL",
              "Your Streamable HTTP endpoint from step 1."
            ],
            [
              "Trust status",
              "`trusted` - an untrusted server is registered but its tools aren't ingested."
            ],
            [
              "Default allowed roles",
              "`finance_analyst` and `admin`. Roles are fixed in AOM: `support_agent`, `finance_analyst`, `admin`."
            ],
            [
              "Authentication",
              "How AOM authenticates to your server: none, bearer token, custom header or basic. The credential is encrypted at rest and never shown again."
            ]
          ]
        },
        {
          "title": "Review the ingested tools",
          "body": "Open Tool Catalog and note the exact tool IDs, e.g. `databricks-prod::execute_sql`, and their argument names - the script needs both. The agent needs only one tool - a SQL tool - because it reads table schemas by sending `DESCRIBE TABLE` through it too. Tools registered through the form get the server's default roles with risk level `unclassified`, which Insights flags for review, and a tool whose description looks like a prompt-injection attempt can be quarantined automatically. To set reviewed per-tool risk levels up front, register through `POST /v1/servers` on API Documentation (while signed in as admin) and include `tool_policies`. If the server also exposes write or admin tools, mark them `critical` and turn on Approvals so a person signs off before they run.",
          "code": "\"tool_policies\": {\n  \"execute_sql\": {\"allowed_roles\": [\"finance_analyst\", \"admin\"], \"risk_level\": \"low\"}\n}"
        },
        {
          "title": "Issue an Agent Identity",
          "body": "Settings → Agent Identities → Issue agent. Recommended rather than strictly required - any key whose role can call the tools will work - but a per-agent key attributes every call on Traces and Audit Log to this agent, can be limited to just these tools, and can be rotated or revoked without affecting anyone else. Copy the key when it's shown: it's stored only as a hash and can't be displayed again.",
          "fields": [
            [
              "Role",
              "`finance_analyst`"
            ],
            [
              "Restrict to specific tools",
              "The SQL tool from step 3 - it's the only one this agent calls."
            ],
            [
              "Expires",
              "Optional - set one for anything short-lived."
            ]
          ]
        },
        {
          "title": "Point the script at it",
          "body": "Setting `DATABRICKS_MCP_SERVER` switches the script to governed mode; unset it to go back to a direct connection. Set these alongside `AOM_BASE_URL`.",
          "code": "export AOM_API_KEY=aom_...                     # the Agent Identity key from step 4\nexport DATABRICKS_MCP_SERVER=databricks-prod   # Server ID from step 2\nexport DATABRICKS_QUERY_TOOL=execute_sql       # tool name from step 3\nexport DATABRICKS_SQL_ARG=sql\nexport DATABRICKS_TABLES=main.sales.orders,main.sales.customers\npython databricks_agent.py \"Top 5 customers by order value this quarter\""
        },
        {
          "title": "Check the appliance settings it relies on",
          "body": "Providers & Policy needs the `databricks` provider enabled (`DATABRICKS_TOKEN` and `DATABRICKS_HOST` on the operations manager), or drop `provider=` in the script to use the appliance default. Without a live provider key, `complete()` answers from an offline stub. Optionally, cap this agent's call rate and spend under Limits & Budgets, and review Guardrails & PII for what Databricks results are allowed to contain."
        },
        {
          "title": "Verify",
          "body": "Run the same question twice. The first run appears as one trace on Traces, with each tool call in it; the second should come back from Context Cache and LLM Cache with no tool calls at all. If the script reports a tool isn't in the catalog, the Server ID or tool name doesn't match Tool Catalog; if it reports the role or scope can't call it, fix the server's allowed roles or the Agent Identity's tool list."
        }
      ]
    },
    code: `"""
Databricks lakehouse agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers questions over Unity Catalog tables, using a Databricks-served model
through AOM's LLM gateway. Two ways to reach the data:

  Governed (recommended) - set DATABRICKS_MCP_SERVER and every SQL statement
  is an AOM tool call to your Databricks MCP server (RBAC, agent scope, audit,
  guardrails); the MCP server holds the Databricks credentials:

    pip install ./couchbase-aom-sdk-*      # from Tools -> Developer SDK
    export AOM_BASE_URL=https://aom.example.com:8090
    export AOM_API_KEY=aom_...                     # your Agent Identity's key
    export AOM_VERIFY_SSL=true                     # false while AOM's cert is self-signed
    export DATABRICKS_MCP_SERVER=databricks-prod   # the Server ID you registered
    export DATABRICKS_QUERY_TOOL=execute_sql       # tool name as shown in Tool Catalog
    export DATABRICKS_SQL_ARG=sql                  # that tool's SQL argument name
    export DATABRICKS_TABLES=main.sales.orders,main.sales.customers
    python databricks_agent.py "Top 5 customers by order value this quarter"

  Direct - leave DATABRICKS_MCP_SERVER unset and the agent connects to a SQL
  warehouse itself (caching and tracing still go through AOM):

    pip install ./couchbase-aom-sdk-* databricks-sql-connector
    export DATABRICKS_SERVER_HOSTNAME=adb-1234567890123456.7.azuredatabricks.net
    export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123def456
    export DATABRICKS_TOKEN=dapi...

The appliance needs the \`databricks\` LLM provider enabled - see LLM Caching ->
Providers & Policy. Drop provider= below to use the appliance's default model.

Caching layers:
  1. Context Cache  - table schemas (DESCRIBE TABLE, 6 h) and query results
                      (15 min). A cache hit never wakes a stopped SQL warehouse.
  2. LLM Cache      - NL -> SQL with semantic matching.
  3. LLM Cache      - the final answer, exact-match only.
"""
import hashlib
import json
import os
import re
import sys

from aom_sdk import (
    AOMAuthorizationError,
    AOMClient,
    AOMNotFoundError,
    AOMRateLimitError,
    AOMScopeError,
)

NAMESPACE = "databricks-lakehouse"
LLM_PROVIDER = "databricks"
LLM_MODEL = os.environ.get("DATABRICKS_LLM_MODEL", "databricks-meta-llama-3-3-70b-instruct")
TABLES = [t.strip() for t in os.environ.get("DATABRICKS_TABLES", "main.sales.orders").split(",") if t.strip()]
MCP_SERVER = os.environ.get("DATABRICKS_MCP_SERVER", "")      # AOM Server ID; empty = direct
QUERY_TOOL = os.environ.get("DATABRICKS_QUERY_TOOL", "execute_sql")
SQL_ARG = os.environ.get("DATABRICKS_SQL_ARG", "sql")
SOURCE = MCP_SERVER or "direct"
SCHEMA_TTL_S = 6 * 3600
RESULT_TTL_S = 15 * 60
MAX_ROWS = 200

client = AOMClient(
    base_url=os.environ["AOM_BASE_URL"],
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="databricks-lakehouse-agent",
)


def rows_of(result) -> list:
    """MCP servers shape query results differently - accept the common ones."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("rows", "data", "results", "result"):
            if isinstance(result.get(key), list):
                return result[key]
    return [result]


def _execute(statement: str) -> list:
    if MCP_SERVER:
        # Governed: an AOM tool call - RBAC, scope, audit and guardrails apply.
        result = client.invoke(f"{MCP_SERVER}::{QUERY_TOOL}", {SQL_ARG: statement})["result"]
        rows = rows_of(result)[:MAX_ROWS]
    else:
        from databricks import sql as dbsql  # only needed for direct mode

        with dbsql.connect(
            server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
            http_path=os.environ["DATABRICKS_HTTP_PATH"],
            access_token=os.environ["DATABRICKS_TOKEN"],
        ) as conn, conn.cursor() as cur:
            cur.execute(statement)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchmany(MAX_ROWS)]
    # Decimals, dates and timestamps -> JSON-safe values for the cache.
    return json.loads(json.dumps(rows, default=str))


def sql_key(sql: str) -> str:
    normalized = re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()
    return f"sql:{SOURCE}:" + hashlib.sha256(normalized.encode()).hexdigest()[:32]


def strip_fences(text: str) -> str:
    text = re.sub(r"^\`\`\`[a-zA-Z]*\s*", "", text.strip())
    return re.sub(r"\s*\`\`\`$", "", text).strip()


def assert_read_only(sql: str) -> None:
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE) or ";" in sql.strip().rstrip(";"):
        raise ValueError(f"Refusing to run a non-SELECT or multi-statement query: {sql!r}")


def table_schema(table: str) -> list:
    def fetch() -> list:
        cols = []
        for r in _execute(f"DESCRIBE TABLE {table}"):
            if not isinstance(r, dict):
                continue
            name = r.get("col_name") or r.get("column_name") or r.get("name")
            if name and not str(name).startswith("#"):
                cols.append({"col": name, "type": r.get("data_type") or r.get("type") or ""})
        return cols

    # Context Cache: DESCRIBE once per table every 6 hours, shared by every agent.
    return client.cached_context(f"schema:{SOURCE}:{table}", fetch, namespace=NAMESPACE, ttl_seconds=SCHEMA_TTL_S)


def question_to_sql(question: str) -> str:
    schemas = "; ".join(
        f"{t}({', '.join(c['col'] + ' ' + c['type'] for c in table_schema(t))})" for t in TABLES
    )
    # LLM Cache, semantic ON - paraphrased questions reuse the cached SQL.
    resp = client.complete(
        f"Question: {question}\n"
        f"Write ONE read-only Databricks SQL query that answers it. Tables: {schemas}. "
        "Use fully-qualified catalog.schema.table names. Return only the SQL.",
        provider=LLM_PROVIDER,
        model=LLM_MODEL,
        namespace=f"{NAMESPACE}:nl2sql",
    )
    sql = strip_fences(resp["response"])
    assert_read_only(sql)
    return sql


def run_query(sql: str) -> list:
    # Context Cache: a hit is a Couchbase KV get - no warehouse spin-up, no DBUs.
    return client.cached_context(sql_key(sql), lambda: _execute(sql), namespace=NAMESPACE, ttl_seconds=RESULT_TTL_S)


def main(question: str) -> None:
    tool = f"{MCP_SERVER}::{QUERY_TOOL}"
    with client.run(name=f"databricks: {question[:60]}") as run:
        try:
            sql = question_to_sql(question)
            rows = run_query(sql)
            # LLM Cache, exact-match only: the prompt carries live rows.
            final = client.complete(
                f"Question: {question}\nSQL: {sql}\nRows (JSON): {json.dumps(rows)}\n"
                "Answer in 2-3 sentences with the key figures.",
                provider=LLM_PROVIDER,
                model=LLM_MODEL,
                namespace=f"{NAMESPACE}:answer",
                semantic=False,
            )
        except AOMNotFoundError:
            sys.exit(f"No '{tool}' in the Tool Catalog - check DATABRICKS_MCP_SERVER / DATABRICKS_QUERY_TOOL.")
        except AOMScopeError:
            sys.exit("This Agent Identity isn't scoped to that tool - add it under Settings -> Agent Identities.")
        except AOMAuthorizationError:
            sys.exit(f"This key's role can't call {tool} - grant the role on the server, or use another key.")
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\nSQL: {sql}")
        print(f"LLM cache: {final['cache']['status']} | cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "Top 5 customers by order value this quarter")
`,
  },
  {
    id: "bigquery",
    label: "BigQuery Agent",
    filename: "bigquery_agent.py",
    tagline: "Questions over a BigQuery dataset - through AOM-governed MCP tool calls to your BigQuery MCP server, or directly with a dry-run cost check and bytes-billed cap - with Gemini through AOM's LLM gateway.",
    install: "pip install ./couchbase-aom-sdk-*      # both modes\npip install google-cloud-bigquery      # direct mode only",
    env: [
      ["AOM_BASE_URL", "https://aom.example.com:8090", "Your AOM operations-manager URL - port 8090 by default. On Helm/Kubernetes installs, where that port isn't exposed, use your dashboard URL instead."],
      ["AOM_API_KEY", "aom_…", "The Agent Identity key from step 4."],
      ["AOM_VERIFY_SSL", "true", "`true` once AOM has a trusted certificate; `false` while it's self-signed."],
      ["BQ_DATASET", "my-project.analytics", "The dataset to query, as `project.dataset`."],
      ["BQ_MCP_SERVER", "bigquery-prod", "Governed: the Server ID from step 2. Leave unset for direct mode."],
      ["BQ_QUERY_TOOL", "execute_sql", "Governed: your SQL tool's name, from Tool Catalog."],
      ["BQ_SQL_ARG", "sql", "Governed: that tool's SQL argument name."],
      ["GCP_PROJECT", "my-project", "Direct: the project queries are billed to."],
      ["BQ_MAX_BYTES", "10737418240", "Direct, optional: per-query bytes-billed cap (default 10 GiB)."],
      ["GOOGLE_APPLICATION_CREDENTIALS", "/path/to/service-account.json", "Direct: or run `gcloud auth application-default login`."],
    ],
    cache: [
      { layer: "Context", what: "Dataset schema (INFORMATION_SCHEMA)", key: "schema:<source>:<project.dataset>", ttl: "6 h" },
      { layer: "Context", what: "Query result (+ bytes billed, direct)", key: "sql:<source>:<sha256 of normalized SQL>", ttl: "30 min" },
      { layer: "LLM · semantic", what: "Question → SQL", key: "namespace bigquery-analytics:nl2sql", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "Final answer (embeds rows)", key: "namespace bigquery-analytics:answer", ttl: "policy TTL" },
    ],
    setup: {
      "title": "Use your own BigQuery MCP server",
      "intro": "In governed mode the agent only ever talks to AOM. AOM calls your MCP server, and your MCP server holds the BigQuery credentials. Steps 2-4 need an admin login to this dashboard - server registration and Agent Identities can't be done with an API key.",
      "steps": [
        {
          "title": "Run your MCP server where AOM can reach it",
          "body": "AOM connects to MCP servers over Streamable HTTP only - a stdio-only server needs an HTTP transport or a stdio-to-HTTP bridge in front of it. The URL has to be reachable from the operations manager, not just from your laptop: on Kubernetes, use the Service's in-cluster name, e.g. `http://<service>.<namespace>.svc.cluster.local:<port>/mcp`. Give the server a service account with BigQuery Data Viewer on only the datasets this agent needs, and BigQuery Job User on the billing project. AOM decides who may call a tool; BigQuery still decides what that tool can reach."
        },
        {
          "title": "Register it in AOM",
          "body": "MCP Servers → Register server, then Register & ingest. Tools are pulled into the catalog as soon as a trusted server is registered.",
          "fields": [
            [
              "Server ID",
              "A short, stable ID such as `bigquery-prod`. It becomes the prefix of every tool ID, and the script's `BQ_MCP_SERVER` must match it."
            ],
            [
              "MCP URL",
              "Your Streamable HTTP endpoint from step 1."
            ],
            [
              "Trust status",
              "`trusted` - an untrusted server is registered but its tools aren't ingested."
            ],
            [
              "Default allowed roles",
              "`finance_analyst` and `admin`. Roles are fixed in AOM: `support_agent`, `finance_analyst`, `admin`."
            ],
            [
              "Authentication",
              "How AOM authenticates to your server: none, bearer token, custom header or basic. The credential is encrypted at rest and never shown again."
            ]
          ]
        },
        {
          "title": "Review the ingested tools",
          "body": "Open Tool Catalog and note the exact tool IDs, e.g. `bigquery-prod::execute_sql`, and their argument names - the script needs both. The agent needs only one tool - a SQL tool - because it reads the dataset schema by querying `INFORMATION_SCHEMA` through it. In governed mode the script's own dry run and bytes-billed cap don't apply, so enforce a cost limit in the MCP server or with BigQuery custom quotas. Tools registered through the form get the server's default roles with risk level `unclassified`, which Insights flags for review, and a tool whose description looks like a prompt-injection attempt can be quarantined automatically. To set reviewed per-tool risk levels up front, register through `POST /v1/servers` on API Documentation (while signed in as admin) and include `tool_policies`. If the server also exposes write or admin tools, mark them `critical` and turn on Approvals so a person signs off before they run.",
          "code": "\"tool_policies\": {\n  \"execute_sql\": {\"allowed_roles\": [\"finance_analyst\", \"admin\"], \"risk_level\": \"low\"}\n}"
        },
        {
          "title": "Issue an Agent Identity",
          "body": "Settings → Agent Identities → Issue agent. Recommended rather than strictly required - any key whose role can call the tools will work - but a per-agent key attributes every call on Traces and Audit Log to this agent, can be limited to just these tools, and can be rotated or revoked without affecting anyone else. Copy the key when it's shown: it's stored only as a hash and can't be displayed again.",
          "fields": [
            [
              "Role",
              "`finance_analyst`"
            ],
            [
              "Restrict to specific tools",
              "The SQL tool from step 3 - it's the only one this agent calls."
            ],
            [
              "Expires",
              "Optional - set one for anything short-lived."
            ]
          ]
        },
        {
          "title": "Point the script at it",
          "body": "Setting `BQ_MCP_SERVER` switches the script to governed mode; unset it to go back to a direct connection. Set these alongside `AOM_BASE_URL`.",
          "code": "export AOM_API_KEY=aom_...               # the Agent Identity key from step 4\nexport BQ_MCP_SERVER=bigquery-prod       # Server ID from step 2\nexport BQ_QUERY_TOOL=execute_sql         # tool name from step 3\nexport BQ_SQL_ARG=sql\nexport BQ_DATASET=my-project.analytics\npython bigquery_agent.py \"Which 5 products had the most returns last month?\""
        },
        {
          "title": "Check the appliance settings it relies on",
          "body": "Providers & Policy needs the `google` provider enabled (`GEMINI_API_KEY` on the operations manager), or drop `provider=` in the script to use the appliance default. Without a live provider key, `complete()` answers from an offline stub. Optionally, cap this agent's call rate and spend under Limits & Budgets, and review Guardrails & PII for what BigQuery results are allowed to contain."
        },
        {
          "title": "Verify",
          "body": "Run the same question twice. The first run appears as one trace on Traces, with each tool call in it; the second should come back from Context Cache and LLM Cache with no tool calls at all. If the script reports a tool isn't in the catalog, the Server ID or tool name doesn't match Tool Catalog; if it reports the role or scope can't call it, fix the server's allowed roles or the Agent Identity's tool list."
        }
      ]
    },
    code: `"""
BigQuery analytics agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers questions over a BigQuery dataset, with Gemini served through AOM's
LLM gateway. Two ways to reach the data:

  Governed (recommended) - set BQ_MCP_SERVER and every SQL statement is an
  AOM tool call to your BigQuery MCP server (RBAC, agent scope, audit,
  guardrails); the MCP server holds the Google credentials:

    pip install ./couchbase-aom-sdk-*      # from Tools -> Developer SDK
    export AOM_BASE_URL=https://aom.example.com:8090
    export AOM_API_KEY=aom_...                  # your Agent Identity's key
    export AOM_VERIFY_SSL=true                  # false while AOM's cert is self-signed
    export BQ_MCP_SERVER=bigquery-prod          # the Server ID you registered
    export BQ_QUERY_TOOL=execute_sql            # tool name as shown in Tool Catalog
    export BQ_SQL_ARG=sql                       # that tool's SQL argument name
    export BQ_DATASET=my-project.analytics
    python bigquery_agent.py "Which 5 products had the most returns last month?"

  Direct - leave BQ_MCP_SERVER unset and the agent queries BigQuery itself,
  with a free dry-run cost check and a bytes-billed cap on every query:

    pip install ./couchbase-aom-sdk-* google-cloud-bigquery
    gcloud auth application-default login     # or GOOGLE_APPLICATION_CREDENTIALS
    export GCP_PROJECT=my-project

The appliance needs the \`google\` LLM provider enabled (GEMINI_API_KEY on the
operations manager). Drop provider= below to use its default model.

Caching layers:
  1. Context Cache  - dataset schema (6 h) and query results (30 min).
                      BigQuery bills by bytes scanned, so every hit is
                      on-demand spend avoided, not just latency.
  2. LLM Cache      - NL -> SQL with semantic matching.
  3. LLM Cache      - the final answer, exact-match only.
"""
import hashlib
import json
import os
import re
import sys

from aom_sdk import (
    AOMAuthorizationError,
    AOMClient,
    AOMNotFoundError,
    AOMRateLimitError,
    AOMScopeError,
)

NAMESPACE = "bigquery-analytics"
LLM_PROVIDER = "google"
LLM_MODEL = os.environ.get("BQ_LLM_MODEL", "gemini-2.5-flash")
DATASET = os.environ["BQ_DATASET"]                  # "project.dataset"
MCP_SERVER = os.environ.get("BQ_MCP_SERVER", "")    # AOM Server ID; empty = direct
QUERY_TOOL = os.environ.get("BQ_QUERY_TOOL", "execute_sql")
SQL_ARG = os.environ.get("BQ_SQL_ARG", "sql")
SOURCE = MCP_SERVER or "direct"
MAX_BYTES_BILLED = int(os.environ.get("BQ_MAX_BYTES", 10 * 1024**3))  # direct mode cap: 10 GiB
SCHEMA_TTL_S = 6 * 3600
RESULT_TTL_S = 30 * 60
MAX_ROWS = 200

client = AOMClient(
    base_url=os.environ["AOM_BASE_URL"],
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="bigquery-analytics-agent",
)
_bq = None


def bq():
    global _bq
    if _bq is None:
        from google.cloud import bigquery  # only needed for direct mode

        _bq = bigquery.Client(project=os.environ.get("GCP_PROJECT"))
    return _bq


def rows_of(result) -> list:
    """MCP servers shape query results differently - accept the common ones."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("rows", "data", "results", "result"):
            if isinstance(result.get(key), list):
                return result[key]
    return [result]


def _execute(sql: str) -> dict:
    if MCP_SERVER:
        # Governed: an AOM tool call - the MCP server enforces its own cost limits.
        result = client.invoke(f"{MCP_SERVER}::{QUERY_TOOL}", {SQL_ARG: sql})["result"]
        rows, billed = rows_of(result)[:MAX_ROWS], None
    else:
        from google.cloud import bigquery

        # Dry run first: free, and tells us what the real query would bill.
        dry = bq().query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        if dry.total_bytes_processed > MAX_BYTES_BILLED:
            raise ValueError(f"Query would scan {dry.total_bytes_processed:,} bytes - over the cap.")
        job = bq().query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED))
        rows = [dict(r.items()) for r in job.result(max_results=MAX_ROWS)]
        billed = job.total_bytes_billed or 0
    return {"rows": json.loads(json.dumps(rows, default=str)), "bytes_billed": billed}


def sql_key(sql: str) -> str:
    normalized = re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()
    return f"sql:{SOURCE}:" + hashlib.sha256(normalized.encode()).hexdigest()[:32]


def strip_fences(text: str) -> str:
    text = re.sub(r"^\`\`\`[a-zA-Z]*\s*", "", text.strip())
    return re.sub(r"\s*\`\`\`$", "", text).strip()


def assert_read_only(sql: str) -> None:
    if not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE) or ";" in sql.strip().rstrip(";"):
        raise ValueError(f"Refusing to run a non-SELECT or multi-statement query: {sql!r}")


def dataset_schema() -> dict:
    def fetch() -> dict:
        sql = (
            f"SELECT table_name, column_name, data_type "
            f"FROM \`{DATASET}\`.INFORMATION_SCHEMA.COLUMNS ORDER BY table_name, ordinal_position"
        )
        schema: dict = {}
        for row in _execute(sql)["rows"]:
            if isinstance(row, dict) and row.get("table_name"):
                schema.setdefault(row["table_name"], []).append(f"{row.get('column_name')} {row.get('data_type')}")
        return schema

    # Context Cache: INFORMATION_SCHEMA read once every 6 hours, shared by every agent.
    return client.cached_context(f"schema:{SOURCE}:{DATASET}", fetch, namespace=NAMESPACE, ttl_seconds=SCHEMA_TTL_S)


def question_to_sql(question: str) -> str:
    tables = "; ".join(f"\`{DATASET}.{t}\`({', '.join(cols)})" for t, cols in dataset_schema().items())
    # LLM Cache, semantic ON - paraphrased questions reuse the cached SQL.
    resp = client.complete(
        f"Question: {question}\n"
        f"Write ONE read-only GoogleSQL (BigQuery) query that answers it. Tables: {tables}. "
        "Return only the SQL.",
        provider=LLM_PROVIDER,
        model=LLM_MODEL,
        namespace=f"{NAMESPACE}:nl2sql",
    )
    sql = strip_fences(resp["response"])
    assert_read_only(sql)
    return sql


def run_query(sql: str) -> dict:
    # Context Cache: a hit costs zero bytes scanned.
    return client.cached_context(sql_key(sql), lambda: _execute(sql), namespace=NAMESPACE, ttl_seconds=RESULT_TTL_S)


def main(question: str) -> None:
    tool = f"{MCP_SERVER}::{QUERY_TOOL}"
    with client.run(name=f"bigquery: {question[:60]}") as run:
        try:
            sql = question_to_sql(question)
            result = run_query(sql)
            # LLM Cache, exact-match only: the prompt carries live rows.
            final = client.complete(
                f"Question: {question}\nSQL: {sql}\nRows (JSON): {json.dumps(result['rows'])}\n"
                "Answer in 2-3 sentences with the key figures.",
                provider=LLM_PROVIDER,
                model=LLM_MODEL,
                namespace=f"{NAMESPACE}:answer",
                semantic=False,
            )
        except AOMNotFoundError:
            sys.exit(f"No '{tool}' in the Tool Catalog - check BQ_MCP_SERVER / BQ_QUERY_TOOL.")
        except AOMScopeError:
            sys.exit("This Agent Identity isn't scoped to that tool - add it under Settings -> Agent Identities.")
        except AOMAuthorizationError:
            sys.exit(f"This key's role can't call {tool} - grant the role on the server, or use another key.")
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\nSQL: {sql}")
        print(f"LLM cache: {final['cache']['status']} | cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "Which 5 products had the most returns last month?")
`,
  },
  {
    id: "s3",
    label: "S3 Agent",
    filename: "s3_agent.py",
    tagline: "Answers questions over text documents under an S3 prefix - through AOM-governed MCP tool calls to your S3 MCP server, or directly with boto3: list once, summarize each document once, answer from the summaries.",
    install: "pip install ./couchbase-aom-sdk-*   # both modes\npip install boto3                   # direct mode only",
    env: [
      ["AOM_BASE_URL", "https://aom.example.com:8090", "Your AOM operations-manager URL - port 8090 by default. On Helm/Kubernetes installs, where that port isn't exposed, use your dashboard URL instead."],
      ["AOM_API_KEY", "aom_…", "The Agent Identity key from step 4."],
      ["AOM_VERIFY_SSL", "true", "`true` once AOM has a trusted certificate; `false` while it's self-signed."],
      ["S3_BUCKET", "acme-contracts", "The bucket holding the documents."],
      ["S3_PREFIX", "suppliers/2026/", "The prefix to read under."],
      ["S3_MCP_SERVER", "s3-docs", "Governed: the Server ID from step 2. Leave unset for direct mode."],
      ["S3_LIST_TOOL", "list_objects", "Governed: your list tool's name, from Tool Catalog."],
      ["S3_GET_TOOL", "get_object", "Governed: your read tool's name, from Tool Catalog."],
      ["S3_BUCKET_ARG / S3_PREFIX_ARG / S3_KEY_ARG", "bucket / prefix / key", "Governed: those tools' argument names."],
      ["AWS_PROFILE", "contracts-reader", "Direct: or any standard boto3 credential source."],
    ],
    cache: [
      { layer: "Context", what: "Prefix listing", key: "list:<source>:<bucket>/<prefix>", ttl: "5 min" },
      { layer: "Context", what: "Object text", key: "s3://<bucket>/<key>@<etag>", ttl: "7 days (1 h if no ETag)" },
      { layer: "LLM · exact", what: "Per-document summary", key: "namespace s3-documents:summary", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "Final answer (embeds summaries)", key: "namespace s3-documents:answer", ttl: "policy TTL" },
    ],
    setup: {
      "title": "Use your own S3 MCP server",
      "intro": "In governed mode the agent only ever talks to AOM. AOM calls your MCP server, and your MCP server holds the S3 credentials. Steps 2-4 need an admin login to this dashboard - server registration and Agent Identities can't be done with an API key.",
      "steps": [
        {
          "title": "Run your MCP server where AOM can reach it",
          "body": "AOM connects to MCP servers over Streamable HTTP only - a stdio-only server needs an HTTP transport or a stdio-to-HTTP bridge in front of it. The URL has to be reachable from the operations manager, not just from your laptop: on Kubernetes, use the Service's in-cluster name, e.g. `http://<service>.<namespace>.svc.cluster.local:<port>/mcp`. Give the server an IAM role allowed only `s3:ListBucket` on this prefix and `s3:GetObject` on its objects. AOM decides who may call a tool; S3 still decides what that tool can reach."
        },
        {
          "title": "Register it in AOM",
          "body": "MCP Servers → Register server, then Register & ingest. Tools are pulled into the catalog as soon as a trusted server is registered.",
          "fields": [
            [
              "Server ID",
              "A short, stable ID such as `s3-docs`. It becomes the prefix of every tool ID, and the script's `S3_MCP_SERVER` must match it."
            ],
            [
              "MCP URL",
              "Your Streamable HTTP endpoint from step 1."
            ],
            [
              "Trust status",
              "`trusted` - an untrusted server is registered but its tools aren't ingested."
            ],
            [
              "Default allowed roles",
              "The role whose agents should read these documents - e.g. `finance_analyst` for supplier contracts, `support_agent` for support policies - plus `admin`. Roles are fixed in AOM: `support_agent`, `finance_analyst`, `admin`."
            ],
            [
              "Authentication",
              "How AOM authenticates to your server: none, bearer token, custom header or basic. The credential is encrypted at rest and never shown again."
            ]
          ]
        },
        {
          "title": "Review the ingested tools",
          "body": "Open Tool Catalog and note the exact tool IDs, e.g. `s3-docs::list_objects`, and their argument names - the script needs both. The agent calls two tools: one that lists objects under a prefix and one that reads an object's text. If the list tool returns each object's ETag or last-modified time, object text is cached for 7 days under that version; if it returns neither, the cache falls back to 1 hour. Tools registered through the form get the server's default roles with risk level `unclassified`, which Insights flags for review, and a tool whose description looks like a prompt-injection attempt can be quarantined automatically. To set reviewed per-tool risk levels up front, register through `POST /v1/servers` on API Documentation (while signed in as admin) and include `tool_policies`. If the server also exposes write or admin tools, mark them `critical` and turn on Approvals so a person signs off before they run.",
          "code": "\"tool_policies\": {\n  \"list_objects\": {\"allowed_roles\": [\"finance_analyst\", \"admin\"], \"risk_level\": \"low\"},\n  \"get_object\":   {\"allowed_roles\": [\"finance_analyst\", \"admin\"], \"risk_level\": \"low\"}\n}"
        },
        {
          "title": "Issue an Agent Identity",
          "body": "Settings → Agent Identities → Issue agent. Recommended rather than strictly required - any key whose role can call the tools will work - but a per-agent key attributes every call on Traces and Audit Log to this agent, can be limited to just these tools, and can be rotated or revoked without affecting anyone else. Copy the key when it's shown: it's stored only as a hash and can't be displayed again.",
          "fields": [
            [
              "Role",
              "One role - e.g. `finance_analyst` for supplier contracts or `support_agent` for support policies - matching what you granted in step 2."
            ],
            [
              "Restrict to specific tools",
              "The list and get tools from step 3, so this agent can't call anything else its role allows - including any put or delete tools on the same server."
            ],
            [
              "Expires",
              "Optional - set one for anything short-lived."
            ]
          ]
        },
        {
          "title": "Point the script at it",
          "body": "Setting `S3_MCP_SERVER` switches the script to governed mode; unset it to go back to a direct connection. Set these alongside `AOM_BASE_URL`.",
          "code": "export AOM_API_KEY=aom_...            # the Agent Identity key from step 4\nexport S3_MCP_SERVER=s3-docs          # Server ID from step 2\nexport S3_LIST_TOOL=list_objects      # tool names from step 3\nexport S3_GET_TOOL=get_object\nexport S3_BUCKET=acme-contracts S3_PREFIX=suppliers/2026/\npython s3_agent.py \"Which supplier contracts auto-renew in Q1?\""
        },
        {
          "title": "Check the appliance settings it relies on",
          "body": "Providers & Policy needs a live LLM provider key - this agent uses the appliance's default provider. Without a live provider key, `complete()` answers from an offline stub. Optionally, cap this agent's call rate and spend under Limits & Budgets, and review Guardrails & PII for what S3 results are allowed to contain."
        },
        {
          "title": "Verify",
          "body": "Run the same question twice. The first run appears as one trace on Traces, with each tool call in it; the second should come back from Context Cache and LLM Cache with no tool calls at all. If the script reports a tool isn't in the catalog, the Server ID or tool name doesn't match Tool Catalog; if it reports the role or scope can't call it, fix the server's allowed roles or the Agent Identity's tool list."
        }
      ]
    },
    code: `"""
S3 document agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers questions over text documents (contracts, policies, reports - .txt,
.md, .csv, .json) under an S3 prefix: list the prefix, summarize each
document once, then answer from the summaries. Two ways to reach the bucket:

  Governed (recommended) - set S3_MCP_SERVER and every list/read is an AOM
  tool call to your S3 MCP server (RBAC, agent scope, audit, guardrails); the
  MCP server holds the AWS credentials:

    pip install ./couchbase-aom-sdk-*      # from Tools -> Developer SDK
    export AOM_BASE_URL=https://aom.example.com:8090
    export AOM_API_KEY=aom_...                 # your Agent Identity's key
    export AOM_VERIFY_SSL=true                 # false while AOM's cert is self-signed
    export S3_MCP_SERVER=s3-docs               # the Server ID you registered
    export S3_LIST_TOOL=list_objects           # tool names as shown in Tool Catalog
    export S3_GET_TOOL=get_object
    export S3_BUCKET=acme-contracts S3_PREFIX=suppliers/2026/
    python s3_agent.py "Which supplier contracts auto-renew in Q1?"

  Direct - leave S3_MCP_SERVER unset and the agent calls S3 itself:

    pip install ./couchbase-aom-sdk-* boto3
    export AWS_PROFILE=...                 # or any standard boto3 credential source

Caching layers:
  1. Context Cache  - the prefix listing (5 min), and each object's text
                      keyed by bucket/key@ETag. A new upload has a new ETag,
                      so it can never be served stale - the TTL can be long.
  2. LLM Cache      - per-document summaries, exact-match on the ETag-
                      versioned text: an unchanged document is summarized once,
                      ever, across every agent and every question.
  3. LLM Cache      - the final answer, exact-match only.
"""
import json
import os
import sys

from aom_sdk import (
    AOMAuthorizationError,
    AOMClient,
    AOMNotFoundError,
    AOMRateLimitError,
    AOMScopeError,
)

NAMESPACE = "s3-documents"
BUCKET = os.environ["S3_BUCKET"]
PREFIX = os.environ.get("S3_PREFIX", "")
MCP_SERVER = os.environ.get("S3_MCP_SERVER", "")     # AOM Server ID; empty = direct
LIST_TOOL = os.environ.get("S3_LIST_TOOL", "list_objects")
GET_TOOL = os.environ.get("S3_GET_TOOL", "get_object")
BUCKET_ARG = os.environ.get("S3_BUCKET_ARG", "bucket")
PREFIX_ARG = os.environ.get("S3_PREFIX_ARG", "prefix")
KEY_ARG = os.environ.get("S3_KEY_ARG", "key")
TEXT_SUFFIXES = (".txt", ".md", ".csv", ".json")
LISTING_TTL_S = 5 * 60
OBJECT_TTL_S = 7 * 24 * 3600    # safe: the key includes the ETag
UNVERSIONED_TTL_S = 3600        # an MCP server that returns no ETag gets a short TTL
MAX_DOCS = 20
MAX_CHARS = 48_000              # stay under the Context Cache max_value_bytes (64 KB default)

client = AOMClient(
    base_url=os.environ["AOM_BASE_URL"],
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="s3-document-agent",
)
_s3 = None


def s3():
    global _s3
    if _s3 is None:
        import boto3  # only needed for direct mode

        _s3 = boto3.client("s3")
    return _s3


def _first(d: dict, *names):
    return next((d[n] for n in names if d.get(n) not in (None, "")), None)


def _mcp_list() -> list:
    result = client.invoke(f"{MCP_SERVER}::{LIST_TOOL}", {BUCKET_ARG: BUCKET, PREFIX_ARG: PREFIX})["result"]
    items = result
    if isinstance(result, dict):
        items = _first(result, "objects", "Contents", "contents", "items", "keys", "rows", "data") or []
    docs = []
    for obj in items if isinstance(items, list) else []:
        obj = {"Key": obj} if isinstance(obj, str) else obj
        key = _first(obj, "Key", "key", "name")
        version = _first(obj, "ETag", "etag", "LastModified", "last_modified")
        if key:
            docs.append({"key": key, "etag": str(version or "").strip('"'), "size": _first(obj, "Size", "size") or 0})
    return docs


def list_documents() -> list:
    def fetch() -> list:
        if MCP_SERVER:
            docs = _mcp_list()
        else:
            docs = []
            for page in s3().get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
                for obj in page.get("Contents", []):
                    docs.append({"key": obj["Key"], "etag": obj["ETag"].strip('"'), "size": obj["Size"]})
        return [d for d in docs if d["key"].lower().endswith(TEXT_SUFFIXES)][:MAX_DOCS]

    # Context Cache: one listing per 5 minutes instead of one per question.
    source = MCP_SERVER or "direct"
    return client.cached_context(f"list:{source}:{BUCKET}/{PREFIX}", fetch, namespace=NAMESPACE, ttl_seconds=LISTING_TTL_S)


def document_text(doc: dict) -> str:
    def fetch() -> str:
        if MCP_SERVER:
            result = client.invoke(f"{MCP_SERVER}::{GET_TOOL}", {BUCKET_ARG: BUCKET, KEY_ARG: doc["key"]})["result"]
            if isinstance(result, dict):
                result = _first(result, "text", "content", "body", "Body", "data") or json.dumps(result)
            text = result if isinstance(result, str) else json.dumps(result, default=str)
        else:
            body = s3().get_object(Bucket=BUCKET, Key=doc["key"], IfMatch=doc["etag"])["Body"].read()
            text = body.decode("utf-8", errors="replace")
        return text[:MAX_CHARS]

    # Context Cache keyed on the ETag - content-addressed, so never stale.
    version = doc["etag"] or "unversioned"
    return client.cached_context(
        f"s3://{BUCKET}/{doc['key']}@{version}",
        fetch,
        namespace=NAMESPACE,
        ttl_seconds=OBJECT_TTL_S if doc["etag"] else UNVERSIONED_TTL_S,
    )


def summarize(doc: dict) -> str:
    # LLM Cache, exact-match: same document text -> same prompt -> zero tokens
    # after the first summary. semantic=False because two near-identical
    # contracts can differ in exactly the clause that matters.
    resp = client.complete(
        f"Document: s3://{BUCKET}/{doc['key']} (etag {doc['etag']})\n\n{document_text(doc)}\n\n"
        "Summarize this document in 5 bullet points. Always include parties, dates, "
        "amounts, renewal and termination terms when present.",
        namespace=f"{NAMESPACE}:summary",
        semantic=False,
    )
    return resp["response"]


def main(question: str) -> None:
    with client.run(name=f"s3: {question[:60]}") as run:
        try:
            docs = list_documents()
            if not docs:
                sys.exit(f"No text documents under s3://{BUCKET}/{PREFIX}")
            summaries = "\n\n".join(f"[{d['key']}]\n{summarize(d)}" for d in docs)
            # LLM Cache, exact-match only: the prompt embeds document content.
            final = client.complete(
                f"Question: {question}\n\nDocument summaries:\n{summaries}\n\n"
                "Answer using only these documents and cite the S3 keys you relied on.",
                namespace=f"{NAMESPACE}:answer",
                semantic=False,
            )
        except AOMNotFoundError:
            sys.exit(f"'{MCP_SERVER}::{LIST_TOOL}' or '::{GET_TOOL}' isn't in the Tool Catalog - check S3_MCP_SERVER / S3_*_TOOL.")
        except AOMScopeError:
            sys.exit("This Agent Identity isn't scoped to those tools - add them under Settings -> Agent Identities.")
        except AOMAuthorizationError:
            sys.exit(f"This key's role can't call the {MCP_SERVER} tools - grant the role on the server, or use another key.")
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\n{len(docs)} documents | LLM cache: {final['cache']['status']} | "
              f"cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "Which supplier contracts auto-renew in Q1?")
`,
  },
  {
    id: "oracle-erp",
    label: "Oracle ERP Agent",
    filename: "oracle_erp_agent.py",
    tagline: "Procurement assistant over Oracle Fusion Cloud ERP - through AOM-governed MCP tool calls to your Oracle ERP MCP server, or the Fusion REST API directly: PO status, the supplier behind it, and that supplier's unpaid invoices.",
    install: "pip install ./couchbase-aom-sdk-*   # both modes\npip install requests                # direct mode only",
    env: [
      ["AOM_BASE_URL", "https://aom.example.com:8090", "Your AOM operations-manager URL - port 8090 by default. On Helm/Kubernetes installs, where that port isn't exposed, use your dashboard URL instead."],
      ["AOM_API_KEY", "aom_…", "The Agent Identity key from step 4."],
      ["AOM_VERIFY_SSL", "true", "`true` once AOM has a trusted certificate; `false` while it's self-signed."],
      ["ORACLE_MCP_SERVER", "oracle-erp", "Governed: the Server ID from step 2. Leave unset for direct mode."],
      ["ORACLE_PO_TOOL", "get_purchase_order", "Governed: your PO lookup tool, from Tool Catalog."],
      ["ORACLE_SUPPLIER_TOOL", "get_supplier", "Governed: your supplier lookup tool."],
      ["ORACLE_INVOICE_TOOL", "search_invoices", "Governed: your invoice search tool."],
      ["ORACLE_PO_ARG / ORACLE_SUPPLIER_ARG", "order_number / supplier", "Governed: those tools' argument names."],
      ["ORACLE_ERP_URL", "https://acme.fa.us2.oraclecloud.com", "Direct: your Fusion Cloud URL."],
      ["ORACLE_ERP_USER / ORACLE_ERP_PASSWORD", "integration.user / …", "Direct: a read-only integration user."],
      ["ORACLE_ERP_REST_VERSION", "11.13.18.05", "Direct, optional: your Fusion REST API version."],
    ],
    cache: [
      { layer: "Context", what: "Supplier master", key: "supplier:<source>:<name>", ttl: "6 h" },
      { layer: "Context", what: "PO header", key: "po:<source>:<order number>", ttl: "5 min" },
      { layer: "Context", what: "Unpaid supplier invoices", key: "invoices:<source>:<supplier>", ttl: "10 min" },
      { layer: "LLM · semantic", what: "PO status explanation", key: "namespace oracle-erp:status", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "PO-number extraction, final answer", key: "namespace oracle-erp:extract / :answer", ttl: "policy TTL" },
    ],
    setup: {
      "title": "Use your own Oracle ERP MCP server",
      "intro": "In governed mode the agent only ever talks to AOM. AOM calls your MCP server, and your MCP server holds the Oracle ERP credentials. Steps 2-4 need an admin login to this dashboard - server registration and Agent Identities can't be done with an API key.",
      "steps": [
        {
          "title": "Run your MCP server where AOM can reach it",
          "body": "AOM connects to MCP servers over Streamable HTTP only - a stdio-only server needs an HTTP transport or a stdio-to-HTTP bridge in front of it. The URL has to be reachable from the operations manager, not just from your laptop: on Kubernetes, use the Service's in-cluster name, e.g. `http://<service>.<namespace>.svc.cluster.local:<port>/mcp`. Give the server an integration user with read-only procurement and payables access - no create, update or approve privileges. AOM decides who may call a tool; Oracle ERP still decides what that tool can reach."
        },
        {
          "title": "Register it in AOM",
          "body": "MCP Servers → Register server, then Register & ingest. Tools are pulled into the catalog as soon as a trusted server is registered.",
          "fields": [
            [
              "Server ID",
              "A short, stable ID such as `oracle-erp`. It becomes the prefix of every tool ID, and the script's `ORACLE_MCP_SERVER` must match it."
            ],
            [
              "MCP URL",
              "Your Streamable HTTP endpoint from step 1."
            ],
            [
              "Trust status",
              "`trusted` - an untrusted server is registered but its tools aren't ingested."
            ],
            [
              "Default allowed roles",
              "`finance_analyst` and `admin`. Roles are fixed in AOM: `support_agent`, `finance_analyst`, `admin`."
            ],
            [
              "Authentication",
              "How AOM authenticates to your server: none, bearer token, custom header or basic. The credential is encrypted at rest and never shown again."
            ]
          ]
        },
        {
          "title": "Review the ingested tools",
          "body": "Open Tool Catalog and note the exact tool IDs, e.g. `oracle-erp::get_purchase_order`, and their argument names - the script needs both. The agent calls three tools: look up a PO by order number, look up a supplier by name, and list a supplier's invoices (it drops paid ones itself). Results can use Fusion's CamelCase attribute names or snake_case - both are accepted. If you later add write actions such as holding an invoice, call them with `client.invoke_with_approval()` so the agent waits for a reviewer's decision. Tools registered through the form get the server's default roles with risk level `unclassified`, which Insights flags for review, and a tool whose description looks like a prompt-injection attempt can be quarantined automatically. To set reviewed per-tool risk levels up front, register through `POST /v1/servers` on API Documentation (while signed in as admin) and include `tool_policies`. If the server also exposes write or admin tools, mark them `critical` and turn on Approvals so a person signs off before they run.",
          "code": "\"tool_policies\": {\n  \"get_purchase_order\": {\"allowed_roles\": [\"finance_analyst\", \"admin\"], \"risk_level\": \"low\"},\n  \"get_supplier\":       {\"allowed_roles\": [\"finance_analyst\", \"admin\"], \"risk_level\": \"low\"},\n  \"search_invoices\":    {\"allowed_roles\": [\"finance_analyst\", \"admin\"], \"risk_level\": \"low\"},\n  \"hold_invoice\":       {\"allowed_roles\": [\"admin\"], \"risk_level\": \"critical\"}\n}"
        },
        {
          "title": "Issue an Agent Identity",
          "body": "Settings → Agent Identities → Issue agent. Recommended rather than strictly required - any key whose role can call the tools will work - but a per-agent key attributes every call on Traces and Audit Log to this agent, can be limited to just these tools, and can be rotated or revoked without affecting anyone else. Copy the key when it's shown: it's stored only as a hash and can't be displayed again.",
          "fields": [
            [
              "Role",
              "`finance_analyst`"
            ],
            [
              "Restrict to specific tools",
              "The PO, supplier and invoice tools from step 3. Leave any write tools out of this agent's scope entirely."
            ],
            [
              "Expires",
              "Optional - set one for anything short-lived."
            ]
          ]
        },
        {
          "title": "Point the script at it",
          "body": "Setting `ORACLE_MCP_SERVER` switches the script to governed mode; unset it to go back to a direct connection. Set these alongside `AOM_BASE_URL`.",
          "code": "export AOM_API_KEY=aom_...                     # the Agent Identity key from step 4\nexport ORACLE_MCP_SERVER=oracle-erp            # Server ID from step 2\nexport ORACLE_PO_TOOL=get_purchase_order       # tool names from step 3\nexport ORACLE_SUPPLIER_TOOL=get_supplier\nexport ORACLE_INVOICE_TOOL=search_invoices\nexport ORACLE_PO_ARG=order_number ORACLE_SUPPLIER_ARG=supplier\npython oracle_erp_agent.py \"Where is PO 1004532 and do we owe that supplier anything?\""
        },
        {
          "title": "Check the appliance settings it relies on",
          "body": "Providers & Policy needs a live LLM provider key - this agent uses the appliance's default provider. Without a live provider key, `complete()` answers from an offline stub. Optionally, cap this agent's call rate and spend under Limits & Budgets, and review Guardrails & PII for what Oracle ERP results are allowed to contain."
        },
        {
          "title": "Verify",
          "body": "Run the same question twice. The first run appears as one trace on Traces, with each tool call in it; the second should come back from Context Cache and LLM Cache with no tool calls at all. If the script reports a tool isn't in the catalog, the Server ID or tool name doesn't match Tool Catalog; if it reports the role or scope can't call it, fix the server's allowed roles or the Agent Identity's tool list."
        }
      ]
    },
    code: `"""
Oracle ERP procurement agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers purchase-order questions against Oracle Fusion Cloud ERP: PO status,
the supplier behind it, and that supplier's unpaid invoices. Two ways to
reach the ERP:

  Governed (recommended) - set ORACLE_MCP_SERVER and every lookup is an AOM
  tool call to your Oracle ERP MCP server (RBAC, agent scope, audit,
  guardrails); the MCP server holds the ERP credentials:

    pip install ./couchbase-aom-sdk-*      # from Tools -> Developer SDK
    export AOM_BASE_URL=https://aom.example.com:8090
    export AOM_API_KEY=aom_...                        # your Agent Identity's key
    export AOM_VERIFY_SSL=true                        # false while AOM's cert is self-signed
    export ORACLE_MCP_SERVER=oracle-erp               # the Server ID you registered
    export ORACLE_PO_TOOL=get_purchase_order          # tool names as shown in Tool Catalog
    export ORACLE_SUPPLIER_TOOL=get_supplier
    export ORACLE_INVOICE_TOOL=search_invoices
    python oracle_erp_agent.py "Where is PO 1004532 and do we owe that supplier anything?"

  Direct - leave ORACLE_MCP_SERVER unset and the agent calls the Fusion REST
  API itself:

    pip install ./couchbase-aom-sdk-* requests
    export ORACLE_ERP_URL=https://acme.fa.us2.oraclecloud.com
    export ORACLE_ERP_USER=integration.user ORACLE_ERP_PASSWORD=...

Direct-mode resource paths and attribute names follow the Fusion
SCM/Financials REST API (version 11.13.18.05) - adjust REST_VERSION and the
q= filters to your release. This agent is read-only by design: route writes
(holding an invoice, cancelling a PO) through an MCP tool marked critical,
with Approvals on, and call client.invoke_with_approval() so a person signs off.

Caching layers, with TTLs matched to how fast each record actually changes:
  1. Context Cache  - supplier master (6 h), PO header (5 min),
                      supplier invoices (10 min).
  2. LLM Cache      - PO-number extraction from the question (exact-match).
  3. LLM Cache      - status-code explanations: few distinct statuses, so
                      after warm-up this is effectively always a hit.
  4. LLM Cache      - the final answer, exact-match only.
"""
import os
import re
import sys

from aom_sdk import (
    AOMAuthorizationError,
    AOMClient,
    AOMNotFoundError,
    AOMRateLimitError,
    AOMScopeError,
)

NAMESPACE = "oracle-erp"
MCP_SERVER = os.environ.get("ORACLE_MCP_SERVER", "")   # AOM Server ID; empty = direct
PO_TOOL = os.environ.get("ORACLE_PO_TOOL", "get_purchase_order")
PO_ARG = os.environ.get("ORACLE_PO_ARG", "order_number")
SUPPLIER_TOOL = os.environ.get("ORACLE_SUPPLIER_TOOL", "get_supplier")
INVOICE_TOOL = os.environ.get("ORACLE_INVOICE_TOOL", "search_invoices")
SUPPLIER_ARG = os.environ.get("ORACLE_SUPPLIER_ARG", "supplier")
REST_VERSION = os.environ.get("ORACLE_ERP_REST_VERSION", "11.13.18.05")
SOURCE = MCP_SERVER or "direct"
SUPPLIER_TTL_S = 6 * 3600
PO_TTL_S = 5 * 60
INVOICE_TTL_S = 10 * 60
# Fusion REST rows carry dozens of attributes - cache only what the agent
# reads, to stay well under the Context Cache max_value_bytes (64 KB default).
PO_FIELDS = ("OrderNumber", "Status", "Supplier", "Total", "CurrencyCode", "Buyer", "CreationDate")
SUPPLIER_FIELDS = ("Supplier", "SupplierNumber", "Status", "TaxpayerCountry")
INVOICE_FIELDS = ("InvoiceNumber", "InvoiceAmount", "InvoiceCurrency", "TermsDate", "DueDate", "PaidStatus")

client = AOMClient(
    base_url=os.environ["AOM_BASE_URL"],
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="oracle-erp-procurement-agent",
)


_erp = None


def erp_get(resource: str, q: str, limit: int = 25) -> list:
    """Direct mode: one Fusion REST query."""
    global _erp
    if _erp is None:
        import requests  # only needed for direct mode

        _erp = requests.Session()
        _erp.auth = (os.environ["ORACLE_ERP_USER"], os.environ["ORACLE_ERP_PASSWORD"])
        _erp.headers["Accept"] = "application/json"
    url = f"{os.environ['ORACLE_ERP_URL'].rstrip('/')}/fscmRestApi/resources/{REST_VERSION}/{resource}"
    resp = _erp.get(url, params={"q": q, "onlyData": "true", "limit": limit}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("items", [])


def mcp_rows(tool: str, arguments: dict) -> list:
    """Governed mode: an AOM tool call - RBAC, scope, audit and guardrails apply."""
    result = client.invoke(f"{MCP_SERVER}::{tool}", arguments)["result"]
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("items", "rows", "data", "results", "invoices"):
            if isinstance(result.get(key), list):
                return result[key]
        return [result] if result else []
    return []


def field(row: dict, *names, default=None):
    """Fusion REST uses CamelCase; MCP servers often use snake_case - accept both."""
    for name in names:
        for candidate in (name, re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()):
            if row.get(candidate) not in (None, ""):
                return row[candidate]
    return default


def pick(row: dict, fields: tuple) -> dict:
    picked = {f: field(row, f) for f in fields if field(row, f) is not None}
    return picked or {k: row[k] for k in list(row)[:20]}


def quote(value: str) -> str:
    return value.replace("'", "''")


def extract_po_number(question: str) -> str:
    # Cheap path first; fall back to the model only when there's no obvious number.
    match = re.search(r"\b\d{5,}\b", question)
    if match:
        return match.group(0)
    # LLM Cache, exact-match: the answer depends on literal text in the question.
    resp = client.complete(
        f"Extract the purchase order number from this request. Reply with only the number, or NONE.\n\n{question}",
        namespace=f"{NAMESPACE}:extract",
        semantic=False,
    )
    po = resp["response"].strip()
    if po.upper() == "NONE":
        sys.exit("I couldn't find a PO number in that request.")
    return po


def purchase_order(po_number: str) -> dict:
    # Context Cache, short TTL - PO status moves during approval and receiving.
    def fetch() -> dict:
        if MCP_SERVER:
            items = mcp_rows(PO_TOOL, {PO_ARG: po_number})
        else:
            items = erp_get("purchaseOrders", f"OrderNumber='{quote(po_number)}'", limit=1)
        if not items:
            raise LookupError(f"PO {po_number} not found")
        return pick(items[0], PO_FIELDS)

    return client.cached_context(f"po:{SOURCE}:{po_number}", fetch, namespace=NAMESPACE, ttl_seconds=PO_TTL_S)


def supplier(name: str) -> dict:
    # Context Cache, long TTL - supplier master data rarely changes.
    def fetch() -> dict:
        if MCP_SERVER:
            items = mcp_rows(SUPPLIER_TOOL, {SUPPLIER_ARG: name})
        else:
            items = erp_get("suppliers", f"Supplier='{quote(name)}'", limit=1)
        return pick(items[0], SUPPLIER_FIELDS) if items else {"Supplier": name}

    return client.cached_context(f"supplier:{SOURCE}:{name}", fetch, namespace=NAMESPACE, ttl_seconds=SUPPLIER_TTL_S)


def open_invoices(supplier_name: str) -> list:
    def fetch() -> list:
        if MCP_SERVER:
            rows = mcp_rows(INVOICE_TOOL, {SUPPLIER_ARG: supplier_name})
            rows = [r for r in rows if str(field(r, "PaidStatus", default="")).lower() != "paid"]
        else:
            rows = erp_get("invoices", f"Supplier='{quote(supplier_name)}';PaidStatus<>'Paid'")
        return [pick(i, INVOICE_FIELDS) for i in rows[:25]]

    # Context Cache, medium TTL - invoices post and pay throughout the day.
    return client.cached_context(f"invoices:{SOURCE}:{supplier_name}", fetch, namespace=NAMESPACE, ttl_seconds=INVOICE_TTL_S)


def explain_status(status: str) -> str:
    # LLM Cache: a handful of distinct status values -> near-100% hit rate.
    resp = client.complete(
        f"In one sentence for a requester, explain what Oracle Fusion purchase order status "
        f"'{status}' means and what normally happens next.",
        namespace=f"{NAMESPACE}:status",
    )
    return resp["response"]


def main(question: str) -> None:
    with client.run(name=f"oracle-erp: {question[:60]}") as run:
        try:
            po_number = extract_po_number(question)
            po = purchase_order(po_number)
            supplier_name = field(po, "Supplier", "SupplierName", default="")
            vendor = supplier(supplier_name) if supplier_name else {}
            invoices = open_invoices(supplier_name) if supplier_name else []
            status = field(po, "Status", default="Unknown")
            status_note = explain_status(status)

            invoice_lines = "\n".join(
                f"- {field(i, 'InvoiceNumber')}: {field(i, 'InvoiceAmount')} {field(i, 'InvoiceCurrency', default='')} "
                f"due {field(i, 'TermsDate', 'DueDate', default='n/a')}"
                for i in invoices
            ) or "none"
            # LLM Cache, exact-match only: the prompt carries live ERP records.
            final = client.complete(
                f"Question: {question}\n"
                f"PO {po_number}: status {status}, total {field(po, 'Total')} {field(po, 'CurrencyCode', default='')}, "
                f"buyer {field(po, 'Buyer')}, created {field(po, 'CreationDate')}.\n"
                f"Status meaning: {status_note}\n"
                f"Supplier: {supplier_name} (number {field(vendor, 'SupplierNumber', default='n/a')}).\n"
                f"Unpaid invoices from this supplier:\n{invoice_lines}\n"
                "Answer the question in 3-4 sentences.",
                namespace=f"{NAMESPACE}:answer",
                semantic=False,
            )
        except LookupError as exc:
            sys.exit(str(exc))
        except AOMNotFoundError:
            sys.exit(f"A '{MCP_SERVER}::...' tool isn't in the Tool Catalog - check ORACLE_MCP_SERVER / ORACLE_*_TOOL.")
        except AOMScopeError:
            sys.exit("This Agent Identity isn't scoped to those tools - add them under Settings -> Agent Identities.")
        except AOMAuthorizationError:
            sys.exit(f"This key's role can't call the {MCP_SERVER} tools - grant the role on the server, or use another key.")
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\nLLM cache: {final['cache']['status']} | cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "Where is PO 1004532 and do we owe that supplier anything?")
`,
  },
];
