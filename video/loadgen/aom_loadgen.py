#!/usr/bin/env python3
"""AOM load generator — Python edition (same plan as aom_loadgen_browser.js).

Issues 100 agent identities across 7 roles (creating 4 extra roles if missing), then drives
discover / invoke / complete / memory / context traffic from each agent's own Bearer key so
every AOM page (Topology, Limits & Budgets, Audit Log, Traces, LLM & Context Cache, Memory)
fills with multi-agent, multi-role usage.

Usage:
  pip install requests
  python3 aom_loadgen.py --base https://45.52.200.223:3111 --user admin --password '...' setup
  python3 aom_loadgen.py --base https://45.52.200.223:3111 run --minutes 20 --concurrency 8
  python3 aom_loadgen.py --base https://45.52.200.223:3111 run --minutes 1440 --concurrency 8 --diurnal   # realistic 24h
  python3 aom_loadgen.py --base ... revoke      # revoke every [loadgen] agent afterwards

Agent keys are stored in ./aom_loadgen_agents.json (treat it as a secret).
"""
import argparse, json, os, random, sys, threading, time
import requests
requests.packages.urllib3.disable_warnings()

EXTRA_ROLES = [
    ("procurement_buyer", "Procurement Buyer - PR and PO status, supplier pricing and oversupply checks."),
    ("ops_engineer", "Operations Engineer - warehouse and lakehouse health, read-only metrics."),
    ("data_steward", "Data Steward - catalog, lineage and table discovery across the warehouses."),
    ("sales_assistant", "Sales Assistant - customer 360 summaries, ticket lookups and order status."),
]
PLAN = {  # role: (count, name stem, owner, description)
    "finance_analyst": (22, "Spend Analyst", "Finance Operations", "Supplier spend, price variance and savings narration across every org."),
    "support_agent": (22, "Helpdesk Copilot", "Support Engineering", "Answers how-do-I questions and tracks tickets for requesters."),
    "procurement_buyer": (18, "PO Status Agent", "Procurement", "Where is my PO? Oversupply and PO-readiness checks for buyers."),
    "ops_engineer": (12, "Warehouse Watch", "Platform Team", "Lakehouse and warehouse health, metrics and table freshness."),
    "data_steward": (10, "Catalog Steward", "Data Platform Team", "Table discovery, lineage and schema questions."),
    "sales_assistant": (12, "Account Copilot", "Sales Enablement", "Customer 360 summaries, open tickets and order status."),
    "admin": (4, "Ops Admin Agent", "Platform Team", "Scheduled admin checks and maintenance queries."),
}
Q = {
    "finance_analyst": dict(disc=["supplier pricing history from the data warehouse", "business metrics to validate purchase order readiness", "list warehouse tables for spend analysis"],
        inv=[("snowflake::query", {"sql": "select supplier, avg(unit_price) from quotes group by supplier"}), ("snowflake::get_metrics", {"metric_types": ["avg_savings_pct", "total_spend"], "start_date": "2026-09-01", "end_date": "2026-10-01"}), ("databricks-procurement::get_metrics", {"metric_types": ["avg_savings_pct"], "start_date": "2026-09-01", "end_date": "2026-10-01"}), ("databricks-procurement::query", {"sql": "select * from supplier_quotes limit 20"}), ("bigquery-procurement::compare_suppliers", {"item_id": "ITM-1042"})],
        prompts=["Summarise the price variance for item ITM-1042 across the three incumbent suppliers.", "Which supplier offers the best qualified price for pumps this quarter?", "Explain the savings calculation on PR #70336 in two sentences.", "Is PR #70689 ready to convert to a PO? Answer from the facts provided only.", "List the top five categories by spend for Houston Plant 01."],
        ctx=["supplier-quotes:ITM-1042", "metrics:2026-09", "org:H01:open-prs"]),
    "support_agent": dict(disc=["look up a customer's open support tickets", "search issues about login failures", "escalate a ticket to tier two"],
        inv=[("zendesk::search_tickets", {"query": "login failure", "status": "open"}), ("zendesk::get_ticket", {"ticket_id": "ZD-48213"}), ("jira::search_issues", {"jql": "project = OPS AND status = Open"}), ("jira::get_issue", {"issue_key": "OPS-4821"}), ("web-search::fetch_page", {"url": "https://example.com/kb/sso"})],
        prompts=["How do I reset my SSO password for the Procurement Command Center?", "Summarise ticket ZD-48213 for a handover note.", "What are the three most common causes of a stuck approval in the Source-to-Pay flow?", "Draft a two-line reply telling a requester their PR is awaiting budget approval."],
        ctx=["kb:sso-reset", "ticket:ZD-48213", "faq:approvals"]),
    "procurement_buyer": dict(disc=["check purchase order status for a requester", "inventory and open purchase orders across every org", "supplier pricing for an item"],
        inv=[("databricks-procurement::query", {"sql": "select status from purchase_orders where pr_id = 70336"}), ("bigquery-procurement::list_items", {}), ("snowflake-procurement::get_metrics", {"metric_types": ["open_prs"], "start_date": "2026-10-01", "end_date": "2026-10-18"})],
        prompts=["Where is my PO for PR #70336?", "Explain the oversupply signal on this PR. (ref #70334)", "Is this PR PO-ready? (ref #70689)", "Which open PRs for Odessa Field Yard can be fulfilled from existing inventory?"],
        ctx=["inventory:OFY", "po:70336", "prs:open:H01"]),
    "ops_engineer": dict(disc=["warehouse health metrics", "list tables in the lakehouse", "query recent load failures"],
        inv=[("databricks-procurement::list_tables", {}), ("snowflake::list_tables", {"database": "ANALYTICS"}), ("databricks-procurement::get_metrics", {"metric_types": ["total_items"], "start_date": "2026-10-17", "end_date": "2026-10-18"})],
        prompts=["Summarise the warehouse load health for the last 24 hours.", "Which tables were refreshed in the last hour?", "Explain what a stale partition means for the spend dashboard."],
        ctx=["health:lakehouse", "tables:analytics", "freshness:supplier_quotes"]),
    "data_steward": dict(disc=["find tables containing supplier data", "schema for the quotes table", "lineage for revenue_daily"],
        inv=[("bigquery-procurement::list_tables", {}), ("snowflake::list_tables", {"database": "ANALYTICS"}), ("databricks-procurement::list_items", {})],
        prompts=["Which tables hold supplier master data?", "Describe the columns of the supplier_quotes table.", "What is the retention policy for transactions?"],
        ctx=["schema:supplier_quotes", "lineage:revenue_daily", "glossary:supplier"]),
    "sales_assistant": dict(disc=["customer 360 summary", "open tickets for an account", "order status for a customer"],
        inv=[("zendesk::search_tickets", {"query": "Acme", "status": "open"}), ("jira::search_issues", {"jql": "labels = acme"}), ("web-search::fetch_page", {"url": "https://example.com/acme"})],
        prompts=["Give me a customer 360 summary for Acme Industrial.", "What is my priority for today across my accounts?", "Check inventory for the Acme renewal bundle."],
        ctx=["c360:acme", "orders:acme", "priority:today"]),
    "admin": dict(disc=["manage warehouse users", "warehouse maintenance"],
        inv=[("snowflake::list_tables", {"database": "ANALYTICS"}), ("snowflake::manage_users", {"action": "list"})],
        prompts=["List the maintenance tasks due this week."], ctx=["maint:weekly"]),
}
DIURNAL = [.18,.14,.12,.12,.15,.22,.38,.62,.85,.95,1.0,.92,.72,.9,1.0,.96,.8,.62,.48,.4,.34,.3,.26,.22]  # multiplier by local hour
ROLE_WEIGHTS = {"finance_analyst": 5, "support_agent": 4, "procurement_buyer": 4, "ops_engineer": 2, "data_steward": 1, "sales_assistant": 2, "admin": 1}
AGENTS_FILE = "aom_loadgen_agents.json"

def admin_session(base, user, password):
    s = requests.Session(); s.verify = False
    r = s.post(f"{base}/v1/auth/login", json={"username": user, "password": password}); r.raise_for_status(); return s

def setup(base, user, password):
    if os.path.exists(AGENTS_FILE):
        print("agents file exists; reusing"); return
    s = admin_session(base, user, password)
    for rid, desc in EXTRA_ROLES:
        r = s.post(f"{base}/v1/roles", json={"id": rid, "description": desc}); print("role", rid, r.status_code)
    agents = []
    for role, (count, stem, owner, desc) in PLAN.items():
        for i in range(1, count + 1):
            name = f"{stem} {i:02d}"
            r = s.post(f"{base}/v1/agents", json={"name": name, "role": role, "owner": owner, "description": desc + " [loadgen]"})
            if r.ok:
                j = r.json(); agents.append({"name": name, "role": role, "key": j["api_key"], "id": j["agent"].get("agent_id") or j["agent"].get("id"), "user": f"user-{role}-{i}"})
            else:
                print("agent", name, r.status_code, r.text[:120])
    json.dump(agents, open(AGENTS_FILE, "w"), indent=1); print("issued", len(agents))

def run(base, minutes, concurrency, diurnal=False, think_ms=1500):
    agents = json.load(open(AGENTS_FILE)); stats = {k: 0 for k in "discover invoke complete memory_add memory_search ctx_get ctx_set denied errors r429".split()}
    lock = threading.Lock(); end = time.time() + minutes * 60
    def J(path, body, key):
        r = requests.post(f"{base}{path}", json=body, headers={"Authorization": f"Bearer {key}"}, verify=False, timeout=60)
        try: j = r.json()
        except Exception: j = None
        return r.status_code, j
    def bump(k):
        with lock: stats[k] += 1
    def step(a):
        q = Q[a["role"]]; kind = random.choices(["discover", "invoke", "complete", "memory", "context"], [3, 4, 5, 2, 3])[0]
        try:
            if kind == "discover": st, _ = J("/v1/tools/discover", {"query": random.choice(q["disc"]), "top_k": 5}, a["key"]); bump("discover")
            elif kind == "invoke":
                tool, args = random.choice(q["inv"]); st, _ = J("/v1/tools/invoke", {"tool_id": tool, "arguments": args}, a["key"]); bump("invoke")
                if st == 403: bump("denied")
            elif kind == "complete":
                p = random.choice(q["prompts"]); p = p if random.random() < 0.88 else f"{p} (ref #{70000 + random.randint(0, 999)})"
                st, _ = J("/v1/llm/complete", {"prompt": p, "namespace": a["role"]}, a["key"]); bump("complete")
            elif kind == "memory":
                if random.random() < 0.5: st, _ = J("/v1/memory", {"user_id": a["user"], "content": "User asked: " + random.choice(q["prompts"]), "session_id": f"s-{int(time.time()) % 7}", "memory_type": "conversational"}, a["key"]); bump("memory_add")
                else: st, _ = J("/v1/memory/search", {"user_id": a["user"], "query": "what does this user usually ask for?", "top_k": 5}, a["key"]); bump("memory_search")
            else:
                key = random.choice(q["ctx"]); st, j = J("/v1/context/get", {"key": key, "namespace": a["role"]}, a["key"]); bump("ctx_get")
                if st == 200 and j and j.get("hit") is False:
                    st, _ = J("/v1/context/set", {"key": key, "value": {"fetched_at": time.time(), "rows": 12}, "namespace": a["role"], "ttl_seconds": 3600, "source_latency_ms": random.randint(180, 780)}, a["key"]); bump("ctx_set")
            if st == 429: bump("r429")
            elif st >= 400 and st != 403: bump("errors")
        except Exception:
            bump("errors")
    def worker():
        roles = list(ROLE_WEIGHTS); w = list(ROLE_WEIGHTS.values())
        while time.time() < end:
            role = random.choices(roles, w)[0]; step(random.choice([a for a in agents if a["role"] == role]))
            if diurnal:
                pace = DIURNAL[time.localtime().tm_hour] * random.uniform(0.85, 1.15); time.sleep(think_ms / 1000 / max(0.05, pace))
    ts = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    [t.start() for t in ts]
    while any(t.is_alive() for t in ts):
        time.sleep(30); print(time.strftime("%H:%M:%S"), stats)
    print("done", stats)

def revoke(base, user, password):
    s = admin_session(base, user, password)
    for a in s.get(f"{base}/v1/agents").json().get("agents", []):
        if "[loadgen]" in (a.get("description") or ""):
            print("revoke", a["name"], s.post(f"{base}/v1/agents/{a.get('agent_id') or a.get('id')}/revoke", json={}).status_code)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--base", required=True); ap.add_argument("--user", default="admin"); ap.add_argument("--password", default=os.environ.get("AOM_PASSWORD", ""))
    sub = ap.add_subparsers(dest="cmd", required=True); sub.add_parser("setup"); r = sub.add_parser("run"); r.add_argument("--minutes", type=int, default=20); r.add_argument("--concurrency", type=int, default=8); r.add_argument("--diurnal", action="store_true", help="pace by a day/night curve for a realistic 24h run"); r.add_argument("--think-ms", type=int, default=1500); sub.add_parser("revoke")
    a = ap.parse_args()
    if a.cmd == "setup": setup(a.base, a.user, a.password)
    elif a.cmd == "run": run(a.base, a.minutes, a.concurrency, a.diurnal, a.think_ms)
    else: revoke(a.base, a.user, a.password)
