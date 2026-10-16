#!/usr/bin/env python3
"""Bulk-load the Knowledge Base from a shell - the path for multi-gigabyte
warehouse exports that have no business going through a browser tab.

Streams a local file to POST /v1/knowledge/uploads (nothing is read into
memory on either side), or queues a cloud import (s3://, gs://, az://,
abfss://, https://) with POST /v1/knowledge/imports, then follows the job
until it finishes. Uses a dashboard admin login, the same as the Knowledge
Base page - the agent API keys cannot add documents.

Examples
--------
  # a Snowflake unload, downloaded locally
  python3 scripts/kb-upload.py --url https://localhost:3111 --user admin \\
      --roles finance_analyst,admin --dump snowflake --table SALES.PUBLIC.ORDERS \\
      orders_0_0_0.csv.gz

  # straight from the bucket the export landed in (credential used once, never stored)
  python3 scripts/kb-upload.py --url https://localhost:3111 --user admin \\
      --roles finance_analyst --dump bigquery --table shop.orders \\
      --import gs://my-bucket/orders/orders-000000000000.parquet \\
      --gcp-service-account-json ~/sa.json

  # a Databricks Volume via the Files API
  python3 scripts/kb-upload.py --url https://localhost:3111 --user admin \\
      --roles finance_analyst --dump databricks --table main.sales.orders \\
      --import "https://<ws>.cloud.databricks.com/api/2.0/fs/files/Volumes/main/default/exports/orders/part-00000.parquet" \\
      --bearer "$DATABRICKS_TOKEN"

The password is read from AOM_PASSWORD or prompted. --insecure skips TLS
verification for the appliance's self-signed certificate.
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.parse

try:
    import requests
except ImportError:
    sys.exit("needs the 'requests' package: pip install requests")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


class Progress:
    def __init__(self, fh, total):
        self.fh, self.total, self.sent, self.last = fh, total, 0, 0.0

    def __iter__(self):
        while True:
            block = self.fh.read(1 << 20)
            if not block:
                break
            self.sent += len(block)
            if time.time() - self.last > 0.5:
                self.last = time.time()
                pct = f" {self.sent * 100 // self.total}%" if self.total else ""
                sys.stderr.write(f"\r  uploading {human(self.sent)} / {human(self.total)}{pct}   ")
                sys.stderr.flush()
            yield block
        sys.stderr.write("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="local file to stream (omit with --import)")
    ap.add_argument("--url", required=True, help="dashboard URL, e.g. https://192.168.111.5:3111")
    ap.add_argument("--user", default=os.getenv("AOM_USERNAME", "admin"))
    ap.add_argument("--roles", required=True, help="comma-separated roles that may retrieve the document")
    ap.add_argument("--title")
    ap.add_argument("--set", dest="set_id", default=None, help="knowledge set id (default: the default set)")
    ap.add_argument("--dump", choices=["snowflake", "databricks", "bigquery", "other"], help="parse as a table export")
    ap.add_argument("--table", help="table name recorded on every chunk (dumps)")
    ap.add_argument("--header", choices=["auto", "yes", "no"], default="auto", help="CSV/TSV header row")
    ap.add_argument("--columns", help="comma-separated column names when --header no")
    ap.add_argument("--ttl-seconds", type=int)
    ap.add_argument("--expires-at")
    ap.add_argument("--import", dest="import_uri", help="s3://, gs://, az://, abfss:// or https:// object instead of a local file")
    ap.add_argument("--bearer", help="https: bearer token")
    ap.add_argument("--aws-access-key-id")
    ap.add_argument("--aws-secret-access-key")
    ap.add_argument("--aws-region")
    ap.add_argument("--gcp-service-account-json", help="path to a service-account JSON file")
    ap.add_argument("--azure-connection-string")
    ap.add_argument("--azure-account")
    ap.add_argument("--azure-key", help="account key or SAS token")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    ap.add_argument("--no-wait", action="store_true", help="return after queueing, don't follow the job")
    args = ap.parse_args()
    if not args.file and not args.import_uri:
        ap.error("give a local file or --import <uri>")
    if args.file and args.import_uri:
        ap.error("give either a local file or --import, not both")

    base = args.url.rstrip("/")
    s = requests.Session()
    s.verify = not args.insecure
    if args.insecure:
        import urllib3
        urllib3.disable_warnings()
    password = os.getenv("AOM_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    r = s.post(f"{base}/v1/auth/login", json={"username": args.user, "password": password}, timeout=30)
    if r.status_code >= 400:
        sys.exit(f"login failed: {r.status_code} {r.text[:200]}")

    dump = None
    if args.dump:
        dump = {"source": args.dump, "table_name": args.table,
                "header": None if args.header == "auto" else args.header == "yes",
                "column_names": [c.strip() for c in (args.columns or "").split(",") if c.strip()]}

    if args.import_uri:
        auth = {}
        if args.bearer:
            auth = {"mode": "bearer", "secret": args.bearer}
        elif args.aws_access_key_id:
            auth = {"access_key_id": args.aws_access_key_id, "secret": args.aws_secret_access_key or ""}
            if args.aws_region:
                auth["region"] = args.aws_region
        elif args.gcp_service_account_json:
            auth = {"service_account_json": open(os.path.expanduser(args.gcp_service_account_json)).read()}
        elif args.azure_connection_string:
            auth = {"connection_string": args.azure_connection_string}
        elif args.azure_account:
            auth = {"account": args.azure_account, "secret": args.azure_key or ""}
        body = {"uri": args.import_uri, "auth": auth, "title": args.title, "allowed_roles": args.roles.split(","),
                "set_id": args.set_id, "dump": dump, "ttl_seconds": args.ttl_seconds, "expires_at": args.expires_at}
        r = s.post(f"{base}/v1/knowledge/imports", json={k: v for k, v in body.items() if v is not None}, timeout=60)
    else:
        path = args.file
        size = os.path.getsize(path)
        q = {"filename": os.path.basename(path), "allowed_roles": args.roles}
        if args.title:
            q["title"] = args.title
        if args.set_id:
            q["set_id"] = args.set_id
        if args.ttl_seconds:
            q["ttl_seconds"] = args.ttl_seconds
        if args.expires_at:
            q["expires_at"] = args.expires_at
        if dump:
            q["dump_source"] = dump["source"]
            if dump["table_name"]:
                q["table_name"] = dump["table_name"]
            if dump["header"] is not None:
                q["header"] = str(dump["header"]).lower()
            if dump["column_names"]:
                q["column_names"] = ",".join(dump["column_names"])
        print(f"streaming {path} ({human(size)}) ...", file=sys.stderr)
        with open(path, "rb") as fh:
            r = s.post(f"{base}/v1/knowledge/uploads?{urllib.parse.urlencode(q)}", data=Progress(fh, size),
                       headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)}, timeout=(30, 600))
    if r.status_code >= 400:
        try:
            sys.exit(f"error {r.status_code}: {r.json().get('detail')}")
        except ValueError:
            sys.exit(f"error {r.status_code}: {r.text[:300]}")
    job = r.json()["job"]
    print(f"job {job['job_id']} {job['status']}", file=sys.stderr)
    if args.no_wait:
        print(json.dumps(job, indent=2))
        return
    last = ""
    while job["status"] in ("uploading", "queued", "downloading", "running"):
        time.sleep(2)
        job = s.get(f"{base}/v1/knowledge/jobs/{job['job_id']}", timeout=30).json()["job"]
        p = job["progress"]
        line = f"  {job['status']}/{job['phase']}: {human(job['bytes_received'] or 0)} received, {p['rows_parsed']:,} rows, {p['chunks_written']:,} chunks"
        if p.get("percent") is not None:
            line += f" ({p['percent']}%)"
        if line != last:
            sys.stderr.write("\r" + line + "   ")
            sys.stderr.flush()
            last = line
    sys.stderr.write("\n")
    if job["status"] != "done":
        sys.exit(f"job {job['status']}: {job.get('error')}")
    print(f"done: document {job['document_id']}, {job['progress']['chunks_written']:,} chunks, {job['progress']['rows_parsed']:,} rows", file=sys.stderr)
    print(json.dumps(job, indent=2))


if __name__ == "__main__":
    main()
