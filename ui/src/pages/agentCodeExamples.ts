// Generated from the agent scripts these examples were written and tested as -
// keep the Python valid if you edit it here (copy it out and run `python -m py_compile`).

export type CacheLayer = { layer: string; what: string; key: string; ttl: string };
export type AgentExample = {
  id: string;
  label: string;
  filename: string;
  tagline: string;
  install: string;
  env: Array<[string, string]>;
  note: string;
  cache: CacheLayer[];
  code: string;
};

export const AGENT_EXAMPLES: AgentExample[] = [
  {
    id: "snowflake",
    label: "Snowflake Agent",
    filename: "snowflake_agent.py",
    tagline: "Natural-language finance analytics over Snowflake, with every warehouse call going through AOM's governed snowflake::query tool.",
    install: "pip install ./couchbase-aom-sdk-*",
    env: [["AOM_BASE_URL", "https://localhost:8090"], ["AOM_API_KEY", "demo-finance-analyst-7e83 (any finance_analyst or admin key)"]],
    note: "Runs as-is against the bundled demo stack: Snowflake is a seeded MCP server, so there's no warehouse connection to configure. RBAC, audit and guardrails apply to every query.",
    cache: [
      { layer: "Context", what: "Table list (snowflake::list_tables)", key: "tables:<warehouse>", ttl: "6 h" },
      { layer: "Context", what: "Query result", key: "sql:<sha256 of normalized SQL>", ttl: "15 min" },
      { layer: "LLM · semantic", what: "Question → SQL", key: "namespace snowflake-analytics:nl2sql", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "Final answer (embeds rows)", key: "namespace snowflake-analytics:answer", ttl: "policy TTL" },
    ],
    code: `"""
Snowflake analytics agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers natural-language finance questions against Snowflake. Every
warehouse call goes through AOM's governed snowflake::query tool (RBAC,
audit, guardrails), never a direct connection, so it runs as-is against
the bundled demo stack.

    pip install ./couchbase-aom-sdk-*      # from Tools -> Developer SDK
    export AOM_BASE_URL=https://localhost:8090
    export AOM_API_KEY=demo-finance-analyst-7e83   # any finance_analyst/admin key
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

from aom_sdk import AOMClient, AOMAuthorizationError, AOMGuardrailError, AOMRateLimitError

NAMESPACE = "snowflake-analytics"
WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "ANALYTICS_WH")
SCHEMA_TTL_S = 6 * 3600     # table lists change rarely
RESULT_TTL_S = 15 * 60      # analytics results go stale - keep this short
MAX_ROWS = 200              # keep cached values under the policy's max_value_bytes (64 KB default)

client = AOMClient(
    base_url=os.environ.get("AOM_BASE_URL", "https://localhost:8090"),
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="snowflake-analytics-agent",
)


def sql_key(sql: str) -> str:
    normalized = re.sub(r"\\s+", " ", sql.strip().rstrip(";")).lower()
    return "sql:" + hashlib.sha256(normalized.encode()).hexdigest()[:32]


def strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^\`\`\`[a-zA-Z]*\\s*", "", text)
    return re.sub(r"\\s*\`\`\`$", "", text).strip()


def assert_read_only(sql: str) -> None:
    if not re.match(r"^\\s*(select|with)\\b", sql, re.IGNORECASE) or ";" in sql.strip().rstrip(";"):
        raise ValueError(f"Refusing to run a non-SELECT or multi-statement query: {sql!r}")


def list_tables() -> list:
    # Context Cache: one governed tool call every 6 hours instead of one per question.
    result = client.cached_context(
        f"tables:{WAREHOUSE}",
        lambda: client.invoke("snowflake::list_tables", {})["result"],
        namespace=NAMESPACE,
        ttl_seconds=SCHEMA_TTL_S,
    )
    return result.get("tables", [])


def question_to_sql(question: str, tables: list) -> str:
    # LLM Cache with semantic matching ON: "revenue by day last week" and
    # "daily revenue for the past 7 days" resolve to the same cached SQL.
    # Keep the prompt question-led - a long shared preamble makes every
    # prompt look alike. If your questions differ mainly by literal values
    # (dates, IDs), pass semantic=False here too.
    resp = client.complete(
        f"Question: {question}\\n"
        f"Write ONE read-only Snowflake SQL query that answers it. "
        f"Available tables: {', '.join(tables)}. Return only the SQL.",
        namespace=f"{NAMESPACE}:nl2sql",
    )
    sql = strip_fences(resp["response"])
    assert_read_only(sql)
    return sql


def run_query(sql: str) -> dict:
    # Context Cache: identical SQL from any agent on this appliance is a KV get.
    def fetch() -> dict:
        result = client.invoke("snowflake::query", {"sql": sql, "warehouse": WAREHOUSE})["result"]
        result["rows"] = result.get("rows", [])[:MAX_ROWS]
        return result

    return client.cached_context(sql_key(sql), fetch, namespace=NAMESPACE, ttl_seconds=RESULT_TTL_S)


def answer(question: str, sql: str, result: dict) -> dict:
    # LLM Cache, exact-match only: the prompt embeds live data, so a
    # near-identical prompt could carry different numbers.
    return client.complete(
        f"Question: {question}\\nSQL: {sql}\\n"
        f"Rows (JSON): {json.dumps(result['rows'], default=str)}\\n"
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
        except AOMAuthorizationError:
            sys.exit("This API key's role can't call snowflake::query - use a finance_analyst or admin key.")
        except AOMGuardrailError as exc:
            sys.exit(f"Blocked by AOM guardrails: {exc}")
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\\nSQL: {sql}")
        print(f"LLM cache: {final['cache']['status']} | cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "What was daily revenue over the last week?")
`,
  },
  {
    id: "databricks",
    label: "Databricks Agent",
    filename: "databricks_agent.py",
    tagline: "Questions over Unity Catalog tables through a Databricks SQL warehouse, answered by a Databricks-served model through AOM's LLM gateway.",
    install: "pip install ./couchbase-aom-sdk-* databricks-sql-connector",
    env: [["AOM_BASE_URL / AOM_API_KEY", "your appliance and agent key"], ["DATABRICKS_SERVER_HOSTNAME", "adb-….azuredatabricks.net"], ["DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/…"], ["DATABRICKS_TOKEN", "dapi…"], ["DATABRICKS_TABLES", "main.sales.orders,main.sales.customers"]],
    note: "Uses provider=\"databricks\" on complete(), so the appliance needs the Databricks provider enabled under Providers & Policy. A Context Cache hit never wakes a stopped SQL warehouse.",
    cache: [
      { layer: "Context", what: "Table schema (DESCRIBE TABLE)", key: "schema:<catalog.schema.table>", ttl: "6 h" },
      { layer: "Context", what: "Query result", key: "sql:<sha256 of normalized SQL>", ttl: "15 min" },
      { layer: "LLM · semantic", what: "Question → SQL", key: "namespace databricks-lakehouse:nl2sql", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "Final answer (embeds rows)", key: "namespace databricks-lakehouse:answer", ttl: "policy TTL" },
    ],
    code: `"""
Databricks lakehouse agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers questions over Unity Catalog tables through a Databricks SQL
warehouse, using a Databricks-served model through AOM's LLM gateway.

    pip install ./couchbase-aom-sdk-* databricks-sql-connector
    export AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=aom_...
    export DATABRICKS_SERVER_HOSTNAME=adb-1234567890123456.7.azuredatabricks.net
    export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123def456
    export DATABRICKS_TOKEN=dapi...
    export DATABRICKS_TABLES=main.sales.orders,main.sales.customers
    python databricks_agent.py "Top 5 customers by order value this quarter"

The appliance needs the \`databricks\` provider enabled (DATABRICKS_TOKEN and
DATABRICKS_HOST on the operations manager) - see LLM Caching -> Providers &
Policy. Drop provider= below to use the appliance's default model instead.

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

from databricks import sql as dbsql

from aom_sdk import AOMClient, AOMRateLimitError

NAMESPACE = "databricks-lakehouse"
LLM_PROVIDER = "databricks"
LLM_MODEL = os.environ.get("DATABRICKS_LLM_MODEL", "databricks-meta-llama-3-3-70b-instruct")
TABLES = [t.strip() for t in os.environ.get("DATABRICKS_TABLES", "main.sales.orders").split(",") if t.strip()]
SCHEMA_TTL_S = 6 * 3600
RESULT_TTL_S = 15 * 60
MAX_ROWS = 200

client = AOMClient(
    base_url=os.environ.get("AOM_BASE_URL", "https://localhost:8090"),
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="databricks-lakehouse-agent",
)


def _connect():
    return dbsql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )


def _execute(statement: str) -> list:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(statement)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchmany(MAX_ROWS)]
    # Decimals, dates and timestamps -> JSON-safe values for the cache.
    return json.loads(json.dumps(rows, default=str))


def sql_key(sql: str) -> str:
    normalized = re.sub(r"\\s+", " ", sql.strip().rstrip(";")).lower()
    return "sql:" + hashlib.sha256(normalized.encode()).hexdigest()[:32]


def strip_fences(text: str) -> str:
    text = re.sub(r"^\`\`\`[a-zA-Z]*\\s*", "", text.strip())
    return re.sub(r"\\s*\`\`\`$", "", text).strip()


def assert_read_only(sql: str) -> None:
    if not re.match(r"^\\s*(select|with)\\b", sql, re.IGNORECASE) or ";" in sql.strip().rstrip(";"):
        raise ValueError(f"Refusing to run a non-SELECT or multi-statement query: {sql!r}")


def table_schema(table: str) -> list:
    # Context Cache: DESCRIBE once per table every 6 hours, shared by every agent.
    return client.cached_context(
        f"schema:{table}",
        lambda: [
            {"col": r["col_name"], "type": r["data_type"]}
            for r in _execute(f"DESCRIBE TABLE {table}")
            if r.get("col_name") and not r["col_name"].startswith("#")
        ],
        namespace=NAMESPACE,
        ttl_seconds=SCHEMA_TTL_S,
    )


def question_to_sql(question: str) -> str:
    schemas = "; ".join(
        f"{t}({', '.join(c['col'] + ' ' + c['type'] for c in table_schema(t))})" for t in TABLES
    )
    # LLM Cache, semantic ON - paraphrased questions reuse the cached SQL.
    resp = client.complete(
        f"Question: {question}\\n"
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
    with client.run(name=f"databricks: {question[:60]}") as run:
        try:
            sql = question_to_sql(question)
            rows = run_query(sql)
            # LLM Cache, exact-match only: the prompt carries live rows.
            final = client.complete(
                f"Question: {question}\\nSQL: {sql}\\nRows (JSON): {json.dumps(rows)}\\n"
                "Answer in 2-3 sentences with the key figures.",
                provider=LLM_PROVIDER,
                model=LLM_MODEL,
                namespace=f"{NAMESPACE}:answer",
                semantic=False,
            )
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\\nSQL: {sql}")
        print(f"LLM cache: {final['cache']['status']} | cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "Top 5 customers by order value this quarter")
`,
  },
  {
    id: "bigquery",
    label: "BigQuery Agent",
    filename: "bigquery_agent.py",
    tagline: "Questions over a BigQuery dataset, with a free dry-run cost check and a bytes-billed cap before any query runs, and Gemini through AOM's LLM gateway.",
    install: "pip install ./couchbase-aom-sdk-* google-cloud-bigquery",
    env: [["AOM_BASE_URL / AOM_API_KEY", "your appliance and agent key"], ["GCP_PROJECT", "my-project"], ["BQ_DATASET", "my-project.analytics"], ["BQ_MAX_BYTES", "10737418240 (optional per-query cap)"], ["GOOGLE_APPLICATION_CREDENTIALS", "or gcloud application-default login"]],
    note: "BigQuery bills by bytes scanned, so every Context Cache hit is on-demand spend avoided, not just latency. Uses provider=\"google\" on complete().",
    cache: [
      { layer: "Context", what: "Dataset schema (INFORMATION_SCHEMA)", key: "schema:<project.dataset>", ttl: "6 h" },
      { layer: "Context", what: "Query result + bytes billed", key: "sql:<sha256 of normalized SQL>", ttl: "30 min" },
      { layer: "LLM · semantic", what: "Question → SQL", key: "namespace bigquery-analytics:nl2sql", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "Final answer (embeds rows)", key: "namespace bigquery-analytics:answer", ttl: "policy TTL" },
    ],
    code: `"""
BigQuery analytics agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers questions over a BigQuery dataset, with a dry-run cost check
before any query is billed and Gemini served through AOM's LLM gateway.

    pip install ./couchbase-aom-sdk-* google-cloud-bigquery
    gcloud auth application-default login     # or GOOGLE_APPLICATION_CREDENTIALS
    export AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=aom_...
    export GCP_PROJECT=my-project BQ_DATASET=my-project.analytics
    python bigquery_agent.py "Which 5 products had the most returns last month?"

The appliance needs the \`google\` provider enabled (GEMINI_API_KEY on the
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

from google.cloud import bigquery

from aom_sdk import AOMClient, AOMRateLimitError

NAMESPACE = "bigquery-analytics"
LLM_PROVIDER = "google"
LLM_MODEL = os.environ.get("BQ_LLM_MODEL", "gemini-2.5-flash")
DATASET = os.environ["BQ_DATASET"]                  # "project.dataset"
MAX_BYTES_BILLED = int(os.environ.get("BQ_MAX_BYTES", 10 * 1024**3))  # hard cap per query: 10 GiB
SCHEMA_TTL_S = 6 * 3600
RESULT_TTL_S = 30 * 60
MAX_ROWS = 200

bq = bigquery.Client(project=os.environ.get("GCP_PROJECT"))
client = AOMClient(
    base_url=os.environ.get("AOM_BASE_URL", "https://localhost:8090"),
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="bigquery-analytics-agent",
)


def sql_key(sql: str) -> str:
    normalized = re.sub(r"\\s+", " ", sql.strip().rstrip(";")).lower()
    return "sql:" + hashlib.sha256(normalized.encode()).hexdigest()[:32]


def strip_fences(text: str) -> str:
    text = re.sub(r"^\`\`\`[a-zA-Z]*\\s*", "", text.strip())
    return re.sub(r"\\s*\`\`\`$", "", text).strip()


def assert_read_only(sql: str) -> None:
    if not re.match(r"^\\s*(select|with)\\b", sql, re.IGNORECASE) or ";" in sql.strip().rstrip(";"):
        raise ValueError(f"Refusing to run a non-SELECT or multi-statement query: {sql!r}")


def dataset_schema() -> dict:
    # Context Cache: INFORMATION_SCHEMA read once every 6 hours, shared by every agent.
    def fetch() -> dict:
        sql = (
            f"SELECT table_name, column_name, data_type "
            f"FROM \`{DATASET}\`.INFORMATION_SCHEMA.COLUMNS ORDER BY table_name, ordinal_position"
        )
        schema: dict = {}
        for row in bq.query(sql).result():
            schema.setdefault(row["table_name"], []).append(f"{row['column_name']} {row['data_type']}")
        return schema

    return client.cached_context(f"schema:{DATASET}", fetch, namespace=NAMESPACE, ttl_seconds=SCHEMA_TTL_S)


def question_to_sql(question: str) -> str:
    tables = "; ".join(f"\`{DATASET}.{t}\`({', '.join(cols)})" for t, cols in dataset_schema().items())
    # LLM Cache, semantic ON - paraphrased questions reuse the cached SQL.
    resp = client.complete(
        f"Question: {question}\\n"
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
    def fetch() -> dict:
        # Dry run first: free, and tells us what the real query would bill.
        dry = bq.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        if dry.total_bytes_processed > MAX_BYTES_BILLED:
            raise ValueError(f"Query would scan {dry.total_bytes_processed:,} bytes - over the cap.")
        job = bq.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED))
        rows = [dict(r.items()) for r in job.result(max_results=MAX_ROWS)]
        return {
            "rows": json.loads(json.dumps(rows, default=str)),
            "bytes_billed": job.total_bytes_billed or 0,
        }

    # Context Cache: a hit costs zero bytes scanned.
    return client.cached_context(sql_key(sql), fetch, namespace=NAMESPACE, ttl_seconds=RESULT_TTL_S)


def main(question: str) -> None:
    with client.run(name=f"bigquery: {question[:60]}") as run:
        try:
            sql = question_to_sql(question)
            result = run_query(sql)
            # LLM Cache, exact-match only: the prompt carries live rows.
            final = client.complete(
                f"Question: {question}\\nSQL: {sql}\\nRows (JSON): {json.dumps(result['rows'])}\\n"
                "Answer in 2-3 sentences with the key figures.",
                provider=LLM_PROVIDER,
                model=LLM_MODEL,
                namespace=f"{NAMESPACE}:answer",
                semantic=False,
            )
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\\nSQL: {sql}")
        print(f"LLM cache: {final['cache']['status']} | cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "Which 5 products had the most returns last month?")
`,
  },
  {
    id: "s3",
    label: "S3 Agent",
    filename: "s3_agent.py",
    tagline: "Answers questions over text documents under an S3 prefix: list once, summarize each document once, answer from the summaries.",
    install: "pip install ./couchbase-aom-sdk-* boto3",
    env: [["AOM_BASE_URL / AOM_API_KEY", "your appliance and agent key"], ["S3_BUCKET", "acme-contracts"], ["S3_PREFIX", "suppliers/2026/"], ["AWS_PROFILE", "or any standard boto3 credential source"]],
    note: "Object keys include the ETag, so the cache is content-addressed: a re-uploaded document gets a new key and can never be served stale. An unchanged document is summarized once, ever, across every agent and question.",
    cache: [
      { layer: "Context", what: "Prefix listing (ListObjectsV2)", key: "list:<bucket>/<prefix>", ttl: "5 min" },
      { layer: "Context", what: "Object text", key: "s3://<bucket>/<key>@<etag>", ttl: "7 days" },
      { layer: "LLM · exact", what: "Per-document summary", key: "namespace s3-documents:summary", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "Final answer (embeds summaries)", key: "namespace s3-documents:answer", ttl: "policy TTL" },
    ],
    code: `"""
S3 document agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers questions over text documents (contracts, policies, reports - .txt,
.md, .csv, .json) under an S3 prefix: list the prefix, summarize each
document once, then answer from the summaries.

    pip install ./couchbase-aom-sdk-* boto3
    export AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=aom_...
    export AWS_PROFILE=...                 # or any standard boto3 credential source
    export S3_BUCKET=acme-contracts S3_PREFIX=suppliers/2026/
    python s3_agent.py "Which supplier contracts auto-renew in Q1?"

Caching layers:
  1. Context Cache  - the prefix listing (5 min), and each object's text
                      keyed by bucket/key@ETag. A new upload has a new ETag,
                      so it can never be served stale - the TTL can be long.
  2. LLM Cache      - per-document summaries, exact-match on the ETag-
                      versioned text: an unchanged document is summarized once,
                      ever, across every agent and every question.
  3. LLM Cache      - the final answer, exact-match only.
"""
import os
import sys

import boto3

from aom_sdk import AOMClient, AOMRateLimitError

NAMESPACE = "s3-documents"
BUCKET = os.environ["S3_BUCKET"]
PREFIX = os.environ.get("S3_PREFIX", "")
TEXT_SUFFIXES = (".txt", ".md", ".csv", ".json")
LISTING_TTL_S = 5 * 60
OBJECT_TTL_S = 7 * 24 * 3600    # safe: the key includes the ETag
MAX_DOCS = 20
MAX_CHARS = 48_000              # stay under the Context Cache max_value_bytes (64 KB default)

s3 = boto3.client("s3")
client = AOMClient(
    base_url=os.environ.get("AOM_BASE_URL", "https://localhost:8090"),
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="s3-document-agent",
)


def list_documents() -> list:
    # Context Cache: one ListObjectsV2 sweep per 5 minutes instead of per question.
    def fetch() -> list:
        docs = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
            for obj in page.get("Contents", []):
                if obj["Key"].lower().endswith(TEXT_SUFFIXES):
                    docs.append({"key": obj["Key"], "etag": obj["ETag"].strip('"'), "size": obj["Size"]})
        return docs[:MAX_DOCS]

    return client.cached_context(f"list:{BUCKET}/{PREFIX}", fetch, namespace=NAMESPACE, ttl_seconds=LISTING_TTL_S)


def document_text(doc: dict) -> str:
    # Context Cache keyed on the ETag - content-addressed, so never stale.
    def fetch() -> str:
        body = s3.get_object(Bucket=BUCKET, Key=doc["key"], IfMatch=doc["etag"])["Body"].read()
        return body.decode("utf-8", errors="replace")[:MAX_CHARS]

    return client.cached_context(
        f"s3://{BUCKET}/{doc['key']}@{doc['etag']}", fetch, namespace=NAMESPACE, ttl_seconds=OBJECT_TTL_S
    )


def summarize(doc: dict) -> str:
    # LLM Cache, exact-match: same document text -> same prompt -> zero tokens
    # after the first summary. semantic=False because two near-identical
    # contracts can differ in exactly the clause that matters.
    resp = client.complete(
        f"Document: s3://{BUCKET}/{doc['key']} (etag {doc['etag']})\\n\\n{document_text(doc)}\\n\\n"
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
            summaries = "\\n\\n".join(f"[{d['key']}]\\n{summarize(d)}" for d in docs)
            # LLM Cache, exact-match only: the prompt embeds document content.
            final = client.complete(
                f"Question: {question}\\n\\nDocument summaries:\\n{summaries}\\n\\n"
                "Answer using only these documents and cite the S3 keys you relied on.",
                namespace=f"{NAMESPACE}:answer",
                semantic=False,
            )
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\\n{len(docs)} documents | LLM cache: {final['cache']['status']} | "
              f"cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "Which supplier contracts auto-renew in Q1?")
`,
  },
  {
    id: "oracle-erp",
    label: "Oracle ERP Agent",
    filename: "oracle_erp_agent.py",
    tagline: "Procurement assistant over Oracle Fusion Cloud ERP REST: PO status, the supplier behind it, and that supplier's unpaid invoices.",
    install: "pip install ./couchbase-aom-sdk-* requests",
    env: [["AOM_BASE_URL / AOM_API_KEY", "your appliance and agent key"], ["ORACLE_ERP_URL", "https://acme.fa.us2.oraclecloud.com"], ["ORACLE_ERP_USER / ORACLE_ERP_PASSWORD", "an integration user"], ["ORACLE_ERP_REST_VERSION", "11.13.18.05 (default)"]],
    note: "Read-only by design, with TTLs matched to how fast each record changes. Route writes (holding an invoice, cancelling a PO) through an AOM-registered MCP tool in the approval tier and call invoke_with_approval(). Check resource attribute names against your Fusion release.",
    cache: [
      { layer: "Context", what: "Supplier master", key: "supplier:<name>", ttl: "6 h" },
      { layer: "Context", what: "PO header", key: "po:<order number>", ttl: "5 min" },
      { layer: "Context", what: "Unpaid supplier invoices", key: "invoices:<supplier>", ttl: "10 min" },
      { layer: "LLM · semantic", what: "PO status explanation", key: "namespace oracle-erp:status", ttl: "policy TTL" },
      { layer: "LLM · exact", what: "PO-number extraction, final answer", key: "namespace oracle-erp:extract / :answer", ttl: "policy TTL" },
    ],
    code: `"""
Oracle ERP procurement agent - Couchbase AOM SDK + LLM Caching + Context Caching.

Answers purchase-order questions against Oracle Fusion Cloud ERP's REST API:
PO status, the supplier behind it, and that supplier's open invoices.

    pip install ./couchbase-aom-sdk-* requests
    export AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=aom_...
    export ORACLE_ERP_URL=https://acme.fa.us2.oraclecloud.com
    export ORACLE_ERP_USER=integration.user ORACLE_ERP_PASSWORD=...
    python oracle_erp_agent.py "Where is PO 1004532 and do we owe that supplier anything?"

Resource paths and attribute names follow the Fusion SCM/Financials REST
API (version 11.13.18.05) - adjust REST_VERSION and the q= filters to
your release. This agent is read-only by design: route writes (holding an
invoice, cancelling a PO) through an AOM-registered MCP tool in the
approval tier and call client.invoke_with_approval() so a human signs off.

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

import requests

from aom_sdk import AOMClient, AOMRateLimitError

NAMESPACE = "oracle-erp"
ERP_URL = os.environ["ORACLE_ERP_URL"].rstrip("/")
REST_VERSION = os.environ.get("ORACLE_ERP_REST_VERSION", "11.13.18.05")
SUPPLIER_TTL_S = 6 * 3600
PO_TTL_S = 5 * 60
INVOICE_TTL_S = 10 * 60
# Fusion REST rows carry dozens of attributes - cache only what the agent
# reads, to stay well under the Context Cache max_value_bytes (64 KB default).
PO_FIELDS = ("OrderNumber", "Status", "Supplier", "Total", "CurrencyCode", "Buyer", "CreationDate")
SUPPLIER_FIELDS = ("Supplier", "SupplierNumber", "Status", "TaxpayerCountry")
INVOICE_FIELDS = ("InvoiceNumber", "InvoiceAmount", "InvoiceCurrency", "TermsDate", "DueDate", "PaidStatus")

erp = requests.Session()
erp.auth = (os.environ["ORACLE_ERP_USER"], os.environ["ORACLE_ERP_PASSWORD"])
erp.headers["Accept"] = "application/json"

client = AOMClient(
    base_url=os.environ.get("AOM_BASE_URL", "https://localhost:8090"),
    api_key=os.environ["AOM_API_KEY"],
    verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    agent_id="oracle-erp-procurement-agent",
)


def erp_get(resource: str, q: str, limit: int = 25) -> list:
    url = f"{ERP_URL}/fscmRestApi/resources/{REST_VERSION}/{resource}"
    resp = erp.get(url, params={"q": q, "onlyData": "true", "limit": limit}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("items", [])


def pick(row: dict, fields: tuple) -> dict:
    return {f: row.get(f) for f in fields if row.get(f) is not None}


def quote(value: str) -> str:
    return value.replace("'", "''")


def extract_po_number(question: str) -> str:
    # Cheap path first; fall back to the model only when there's no obvious number.
    match = re.search(r"\\b\\d{5,}\\b", question)
    if match:
        return match.group(0)
    # LLM Cache, exact-match: the answer depends on literal text in the question.
    resp = client.complete(
        f"Extract the purchase order number from this request. Reply with only the number, or NONE.\\n\\n{question}",
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
        items = erp_get("purchaseOrders", f"OrderNumber='{quote(po_number)}'", limit=1)
        if not items:
            raise LookupError(f"PO {po_number} not found")
        return pick(items[0], PO_FIELDS)

    return client.cached_context(f"po:{po_number}", fetch, namespace=NAMESPACE, ttl_seconds=PO_TTL_S)


def supplier(name: str) -> dict:
    # Context Cache, long TTL - supplier master data rarely changes.
    def fetch() -> dict:
        items = erp_get("suppliers", f"Supplier='{quote(name)}'", limit=1)
        return pick(items[0], SUPPLIER_FIELDS) if items else {"Supplier": name}

    return client.cached_context(f"supplier:{name}", fetch, namespace=NAMESPACE, ttl_seconds=SUPPLIER_TTL_S)


def open_invoices(supplier_name: str) -> list:
    # Context Cache, medium TTL - invoices post and pay throughout the day.
    return client.cached_context(
        f"invoices:{supplier_name}",
        lambda: [
            pick(i, INVOICE_FIELDS)
            for i in erp_get("invoices", f"Supplier='{quote(supplier_name)}';PaidStatus<>'Paid'")
        ],
        namespace=NAMESPACE,
        ttl_seconds=INVOICE_TTL_S,
    )


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
            supplier_name = po.get("Supplier", "")
            vendor = supplier(supplier_name) if supplier_name else {}
            invoices = open_invoices(supplier_name) if supplier_name else []
            status_note = explain_status(po.get("Status", "Unknown"))

            invoice_lines = "\\n".join(
                f"- {i.get('InvoiceNumber')}: {i.get('InvoiceAmount')} {i.get('InvoiceCurrency', '')} "
                f"due {i.get('TermsDate') or i.get('DueDate', 'n/a')}"
                for i in invoices
            ) or "none"
            # LLM Cache, exact-match only: the prompt carries live ERP records.
            final = client.complete(
                f"Question: {question}\\n"
                f"PO {po_number}: status {po.get('Status')}, total {po.get('Total')} {po.get('CurrencyCode', '')}, "
                f"buyer {po.get('Buyer')}, created {po.get('CreationDate')}.\\n"
                f"Status meaning: {status_note}\\n"
                f"Supplier: {supplier_name} (number {vendor.get('SupplierNumber', 'n/a')}).\\n"
                f"Unpaid invoices from this supplier:\\n{invoice_lines}\\n"
                "Answer the question in 3-4 sentences.",
                namespace=f"{NAMESPACE}:answer",
                semantic=False,
            )
        except LookupError as exc:
            sys.exit(str(exc))
        except AOMRateLimitError as exc:
            sys.exit(f"Rate limit or budget hit ({exc.limit}); retry in {exc.retry_after}s.")

        print(final["response"])
        print(f"\\nLLM cache: {final['cache']['status']} | cost \${final['cost_usd']:.5f} | trace {run.trace_id}")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "Where is PO 1004532 and do we owe that supplier anything?")
`,
  },
];
