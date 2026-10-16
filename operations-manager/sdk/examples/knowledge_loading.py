"""
Operator example: load the Knowledge Base at scale with AOMAdminClient.

Three ways in, from smallest to largest:
  1. stream a local file (a Snowflake/Databricks/BigQuery unload up to 5 GB);
  2. queue a cloud import straight from the bucket the export landed in;
  3. create a warehouse sync - a scheduled query the appliance runs itself.

All three produce an *ingestion job* you can follow; the result is a
document whose chunks are retrievable by the roles you name here and
nobody else (see knowledge_and_guardrails.py for the agent side).

Run with:
    AOM_DASHBOARD_URL=https://localhost:3111 AOM_USERNAME=admin AOM_PASSWORD=... \\
        python examples/knowledge_loading.py orders_0_0_0.csv.gz

AOMAdminClient signs in with a dashboard account (local or LDAP) - the
agent API keys deliberately cannot add documents. Point it at the
dashboard origin (port 3111 on the bundled stack), which is where the
session cookie lives and where nginx is configured for unbuffered uploads.
"""
import os
import sys

from aom_sdk import AOMAdminClient, AOMError


def progress(job: dict) -> None:
    p = job["progress"]
    pct = f" ({p['percent']}%)" if p.get("percent") is not None else ""
    print(f"  {job['status']}/{job['phase']}: {p['rows_parsed']:,} rows, {p['chunks_written']:,} chunks{pct}")


def main() -> None:
    admin = AOMAdminClient(
        os.environ.get("AOM_DASHBOARD_URL", "https://localhost:3111"),
        username=os.environ.get("AOM_USERNAME", "admin"),
        password=os.environ["AOM_PASSWORD"],
        verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    )
    roles = ["finance_analyst"]

    # 1. Stream a local warehouse export. `dump` tells the parser it is a
    #    table (one chunk per row group, with the schema up front) rather
    #    than prose; omit it for Markdown, text or PDF documents.
    if len(sys.argv) > 1:
        path = sys.argv[1]
        job = admin.upload_knowledge(
            path,
            allowed_roles=roles,
            dump={"source": "snowflake", "table_name": "SALES.PUBLIC.ORDERS"},
            on_progress=lambda sent, total: print(f"\r  uploading {sent * 100 // max(total, 1)}%", end=""),
        )
        print(f"\nqueued upload job {job['job_id']}")
        job = admin.wait_for_job(job["job_id"], on_progress=progress)
        print(f"document {job['document_id']} ready\n")

    # 2. Or let the appliance pull the object itself. The credential is
    #    used for that one download and never stored.
    if os.environ.get("EXPORT_URI"):
        job = admin.import_knowledge(
            os.environ["EXPORT_URI"],  # s3://..., gs://..., az://..., or an https:// signed URL
            allowed_roles=roles,
            auth={"service_account_json": open(os.environ["GCP_SA_JSON"]).read()} if os.environ.get("GCP_SA_JSON") else None,
            dump={"source": "bigquery", "table_name": "shop.orders"},
        )
        job = admin.wait_for_job(job["job_id"], on_progress=progress)
        print(f"imported as document {job['document_id']}\n")

    # 3. Or stop exporting altogether: a warehouse sync runs your query on
    #    a schedule and keeps one document current ("replace" mode) or
    #    keeps dated copies ("snapshot" mode with a TTL).
    if os.environ.get("SNOWFLAKE_ACCOUNT"):
        connection = {
            "account": os.environ["SNOWFLAKE_ACCOUNT"],
            "user": os.environ["SNOWFLAKE_USER"],
            "auth_method": "keypair",
            "private_key": open(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]).read(),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        }
        check = admin.test_warehouse_sync("snowflake", connection, "SELECT * FROM SALES.PUBLIC.ORDERS")
        print("connection test:", check["detail"], "-", check.get("columns"))
        if check["ok"]:
            sync = admin.create_warehouse_sync({
                "name": "Orders (daily)",
                "vendor": "snowflake",
                "connection": connection,
                "query": "SELECT * FROM SALES.PUBLIC.ORDERS WHERE ORDER_DATE >= DATEADD(day, -90, CURRENT_DATE)",
                "table_name": "SALES.PUBLIC.ORDERS",
                "allowed_roles": roles,
                "schedule": {"kind": "cron", "cron": "0 2 * * *"},
                "mode": "replace",
            })
            job = admin.run_warehouse_sync(sync["sync_id"])  # first run now, the rest at 02:00
            admin.wait_for_job(job["job_id"], on_progress=progress)
            print(f"sync {sync['sync_id']} ran; next at {admin.warehouse_syncs()['syncs'][0]['next_run_at']}")


if __name__ == "__main__":
    try:
        main()
    except AOMError as exc:
        sys.exit(f"error: {exc}")
