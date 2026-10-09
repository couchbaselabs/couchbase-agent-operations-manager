# Couchbase Agent Operations Manager

<img width="3456" height="2098" alt="image" src="https://github.com/user-attachments/assets/bec71859-ce6b-4482-94e9-ec72525286fb" />

Couchbase Agent Operations Manager (AOM) is a self-hosted control plane for
AI agents, built on Couchbase Server Enterprise Edition that eases adoption
of the Couchbase AI Data Plane. Migrating an agent's existing memory to 
Couchbase Agent Memory for Context Caching and LLM Caching is simplified,
allowing existing and new agents to rapidly benefit from the Couchbase AI Data Plane.

- **Tools** - MCP tool definitions are embedded and stored in Couchbase, and
  agents discover them with a single Search request that combines RBAC and
  trust filtering with vector similarity. Every invocation is re-authorized
  before it is proxied downstream, and tools are scanned for hijacking and
  definition drift.
- **Models** - completions for Claude, ChatGPT and Gemini go through one
  endpoint, with exact and semantic response caching, rate limits, token and
  spend budgets, and PII guardrails.
- **Memory and knowledge** - per-user agent memory with consolidation, a
  context cache, and a knowledge base for RAG with role-filtered retrieval.
- **Oversight** - agent identities with key rotation and OIDC, audit logging of
  all agent operations with tools, run traces, evaluations with a regression
  gate, an audit log with SIEM forwarding, and a web dashboard for all of it.

A Python SDK and Claude, ChatGPT and Gemini skills (under **Tools** in the
dashboard) connect new or existing agents. AOM deploys with Docker Compose or
a Helm chart.

## The problem this solves

Most MCP-using agents today treat every configured MCP server, and every
tool it advertises, as trusted by default. There is usually no login, no
per-tool authorization, and no record of who called what. That default
model creates three well-known failure modes:

- **Unauthorized local code execution** - a tool from an unreviewed server
  gets run just because the agent was pointed at it.
- **Hidden prompt injection** - a tool's *description* (not its output) can
  carry instructions the LLM treats as trusted system text, because
  nothing distinguishes "text from a vetted source" from "text from
  anywhere."
- **Over-privileged access** - any caller can invoke any tool on any
  configured server, including destructive admin actions, because nothing
  ties a request to who is actually asking.

The Couchbase Agent Operations Manager sits between your agents and the world
of MCP tool servers and closes all three: agents never talk to a downstream
MCP server directly. They authenticate to this appliance, ask it to *discover*
tools for a task (RBAC + vector-search pre-filter, never a full unfiltered
tool dump), and ask it to *invoke* whichever tool they picked - re-checked
against Couchbase independently before anything is proxied downstream. Every
decision is written to an append-only audit log.

It also runs a dedicated **MCP Tool Hijacking detector** (see
[MCP Tool Hijacking detection](#mcp-tool-hijacking-detection) below) - the
indirect-prompt-injection variant where a malicious tool description or
response tries to steer the agent's next action, up to and including
calling a completely different, higher-privilege tool. RBAC and trust
review stop an *unregistered* tool from ever being reachable; hijacking
detection is what catches a *registered, reviewed* tool whose description
or live output has been poisoned.

The same "one governed choke point" argument applies to the *other* half of
an agent's traffic - its model calls - which is what **[LLM caching for
agents](#llm-caching-for-agents)** adds: agents route completions through
`/v1/llm/complete` against Claude, ChatGPT or Gemini, answers are cached in
Couchbase by exact hash and by vector similarity, and the tokens a repeat
question would have cost are never spent. Cache invalidation is policy, not
guesswork - TTL, reuse limits, model/policy/catalog change detection, scope
and namespace, never-cache rules and manual purge - and a dashboard reports
what the caching actually saved.

## Architecture

```
                     ┌─────────────────────────────────────────────┐
  Your AI agent  ───▶│  Couchbase Agent Operations Manager (API)   │
  (or the bundled    │  - authenticates callers (API key -> role)  │
   Agent Tool Audit) │  - discover: RBAC + vector Search pre-filter│
                     │  - invoke: re-checked, then proxied         │
                     └─────────────────────────────────────────────┘
                                          │
                                          ▼
                                    ┌───────────┐
                   MCP servers  ───▶│ Couchbase │  servers / tools /
                   (registered      │           │  agent_memory (conversational /
                    & trusted)      │           │    profile / semantic) /
                                    │           │  identities / access_log
                                    │           │  llm_cache / llm_cache_log
                                    └───────────┘
                                          ▲
                                          │
                                    ┌───────────┐
                                    │  Dashboard │  React + TS + Vite
                                    │    (UI)    │  admin console
                                    └───────────┘
```

Five containers:

- **couchbase** - Couchbase Server, Enterprise Edition. Required (not a
  preference): the vector-typed index field this appliance's core feature
  depends on is rejected outright by Community Edition. Free to run for
  development/testing under Couchbase's standard license.
- **couchbase-init** - one-shot provisioning: bucket/scope/collections
  (`servers`, `tools`, `identities`, `access_log`, `llm_cache`,
  `llm_cache_log`, `settings`) and primary indexes.
- **sample-mcp-servers** - six bundled mock MCP tool servers so the
  appliance is testable immediately, no real credentials needed: `jira`,
  `zendesk`, `snowflake` (well-behaved), `docs-search` and `web-search`
  (MCP Tool Hijacking fixtures - see below), and `shadow-diagnostics` (an
  intentionally unregistered server). Remove this container whenever you
  no longer need the samples.
- **operations-manager** - the appliance itself. Authenticates callers, ingests
  only explicitly-registered/trusted servers' tool catalogs into Couchbase
  with embeddings + `allowed_roles` + `trust_status`, answers discovery
  requests with one Couchbase Search request combining vector kNN with an
  RBAC/trust pre-filter, re-checks authorization before proxying any
  invoke, and writes an audit-log entry for every decision. Also runs the
  MCP Tool Hijacking detector: a metadata scan at ingest time, a response
  scan on every invoke, and a background monitor that re-scans the catalog
  on a timer. Finally, it is the LLM caching gateway - see [LLM caching for
  agents](#llm-caching-for-agents).
- **ui** - the admin dashboard (React + TypeScript + Vite, served by nginx): a
  live findings/insights feed, server registration, the tool catalog, roles,
  the audit log, and an Agent Tool Audit for calling discover/invoke directly.

## RBAC model

Three seed roles, each with its own API key (see `.env.example`):

- `support_agent` - Jira (read) + Zendesk (read/write)
- `finance_analyst` - Snowflake read-only analytics
- `admin` - everything trusted, including the two high-risk Snowflake admin
  tools (`manage_users`, `manage_warehouse`)

Roles themselves are reviewable, code-owned config in
`operations-manager/app/rbac_policy.py` - they're the kind of thing a security
team reviews in a PR, not something added through a UI. **Servers**,
though, are meant to change without a redeploy: register new ones from the
Servers page (or `POST /v1/servers`) and their tools get ingested with a
deny-by-default policy (admin-only, `risk_level: unclassified`) unless you
assign default allowed roles at registration time, or add a reviewed
override to `TOOL_POLICY`. Couchbase's `tools` collection - not the Python
file - is the actual runtime source of truth the operations manager queries on
every request.

## Installation requirements

Two ways to deploy, both running the same five pieces: Couchbase Server
Enterprise, a one-time provisioning step, the operations manager (API +
embedding model), the dashboard, and the optional sample MCP servers.

| | Docker Compose | Kubernetes (Helm) |
|---|---|---|
| Best for | A laptop, a demo, or a single VM | A shared or production-like environment |
| You need | Docker Engine with the Compose v2 plugin, or Docker Desktop | Kubernetes with a default StorageClass (ReadWriteOnce volumes), Helm 3, and a registry the cluster can pull from (or a single-node K3s host, via `deploy/deploy.sh`) |
| CPU architecture | x86_64 or arm64 | x86_64 or arm64 |
| How | [Run it](#run-it) below | [Helm chart README](./helm/couchbase-agent-operations-manager) |

### Docker Compose: CPU, RAM and disk

| | Minimum | Recommended |
|---|---|---|
| CPU | 4 cores | 8 cores |
| RAM available to Docker | 8 GB | 12-16 GB |
| Free disk | 30 GB | 50 GB |

Where it goes:

- **Couchbase Server** reserves 3 GB of service quotas by default (data
  2048 MB + index 512 MB + search 512 MB), plus roughly 1.5 GB for the
  query service and its own runtime - about 4.5 GB in all. The quotas are
  set by `COUCHBASE_CLUSTER_RAMSIZE`, `COUCHBASE_INDEX_RAMSIZE`,
  `COUCHBASE_FTS_RAMSIZE` and `COUCHBASE_BUCKET_RAMSIZE` in `.env`; lower
  them only for short demos (see the comment in `docker-compose.yml`).
- **The operations manager** uses about 1.5 GB with the default embedding
  model, up to about 3 GB under load. Each additional *local* embedding
  model loaded for a knowledge set adds up to about 2 GB (BGE-M3 and
  multilingual-E5-large are the largest); hosted models add nothing.
- **The dashboard and sample MCP servers** need under 0.5 GB together.
- **Disk:** about 2 GB for the Couchbase image and 2-3 GB for the
  operations-manager image (CPU-only PyTorch), plus Docker build cache,
  the Couchbase data volume (grows with the audit log, caches and
  Knowledge Base - plan on 20 GB, like the Helm default), and the
  embedding-model cache (about 100 MB for the default model, up to a few
  GB if you add local models).

On Docker Desktop, raise the memory limit under **Settings → Resources**
before the first `docker compose up` - Couchbase fails to start, or is
killed shortly after, when it can't get its quota.

### Kubernetes: CPU, RAM and disk

The chart's defaults in
[`values.yaml`](./helm/couchbase-agent-operations-manager/values.yaml):

| Component | CPU request / limit | Memory request / limit | Persistent storage |
|---|---|---|---|
| Couchbase Server | 500m / 2 | 2 GiB / 6 GiB | 20 GiB |
| Operations manager | 500m / 2 | 1.5 GiB / 3 GiB | 5 GiB (embedding-model cache) |
| Sample MCP servers (optional) | 100m / 500m | 128 MiB / 256 MiB | - |
| Dashboard (nginx) | 100m / 250m | 64 MiB / 128 MiB | - |
| **Total** | **1.2 / 4.75 vCPU** | **~3.7 GiB / ~9.4 GiB** | **25 GiB** |

The provisioning Job runs briefly after each install or upgrade and sets
no resource requests of its own.

Size nodes to the limits rather than the requests: Couchbase's 6 GiB limit
is the value proven in a real install (3 GiB was OOM-killed seconds after
start). For a single node - K3s, for example - that means at least
**4 vCPU, 16 GB RAM and 50 GB of disk** (the 25 GiB of volumes, about 5 GB
of images, and room for the OS and system pods). Raise
`operationsManager.resources.limits.memory` before setting
`operationsManager.embeddingMaxLoadedModels` above 2 to keep several large
local embedding models loaded.

**Using an external Couchbase cluster** (see
[below](#using-an-external-couchbase-enterprise-server)) removes the
Couchbase row: the appliance itself then needs about 0.7 vCPU / 1.7 GiB
requested and 2.75 vCPU / 3.4 GiB at its limits, plus the 5 GiB model
cache.

## Run it

```bash
cp .env.example .env
# optional: add ANTHROPIC_API_KEY, OPENAI_API_KEY and/or GEMINI_API_KEY to .env
# (without them, LLM caching answers misses from a labelled stub - see below)
./scripts/setup-corporate-ca.sh
docker compose up --build
```

or, for a clearer view of the multi-container startup sequence:

```bash
./start.sh
```

> **On a corporate laptop behind a TLS-inspecting proxy** (Zscaler, Netskope,
> Palo Alto GlobalProtect, etc.), `pip install`/`npm install` inside the build
> containers will fail with a self-signed-certificate error unless they trust
> your org's proxy CA. Run this once first:
>
> ```bash
> ./scripts/setup-corporate-ca.sh
> ```
>
> It exports the CA(s) your Mac already trusts into `certs/` (gitignored,
> machine-specific) so the Docker builds - and the operations-manager
> container's embedding-model download at startup - can trust them too.

First boot downloads the Couchbase Enterprise image and a local embedding
model (~100MB, cached afterwards) - give it a few minutes. Then open:

- **Dashboard**: <https://localhost> (log in as `admin` - you'll be
  asked to set that account's password the first time)
- **Operations Manager API**: <https://localhost:8090> (see `/docs` for the
  OpenAPI UI - also reachable same-origin through the dashboard at
  `https://localhost/docs`, which is what the "API Documentation" nav
  item under Tools links to)
- **Couchbase Web Console**: <http://localhost:8091> (`Administrator` /
  `CouchbaseDemo123!` by default - Couchbase Server's own console isn't
  covered by this appliance's TLS setup)

Both the dashboard and the API serve HTTPS with a self-signed certificate
by default, so your browser will warn about it and `curl`/SDK calls need
`-k`/`verify=False` until you install a real one - see
[HTTPS / TLS](#https--tls) below.

LLM caching is on by default and needs no API key to try - see
[LLM caching for agents](#llm-caching-for-agents).

`docker compose down -v` gives you a fully clean start (drops the
Couchbase and embedding-model-cache volumes).

**Running this on Kubernetes instead?** See
[helm/couchbase-agent-operations-manager](./helm/couchbase-agent-operations-manager)
for a Helm chart covering the same five-piece topology (Couchbase as a
StatefulSet, provisioning as a Job instead of docker-compose.yml's
always-on init container, the sample MCP servers, operations-manager, and
the dashboard). You build the three custom images yourself and
push them to a registry your cluster can pull from - read that chart's
README first, in particular "Before you install". For a single-node K3s
cluster, `deploy/deploy.sh` builds the images on the node, imports them into
K3s, and installs the chart with
`helm/couchbase-agent-operations-manager/values-k3s.yaml`.

### Deploying with Helm: external Couchbase and LLM API keys

The chart README covers both in full: see
[Using an external Couchbase Enterprise server](./helm/couchbase-agent-operations-manager/README.md#using-an-external-couchbase-enterprise-server)
and
[LLM provider API keys](./helm/couchbase-agent-operations-manager/README.md#llm-provider-api-keys).
In short: prepare the bucket, scope and RBAC user exactly as in the next
section, keep the Couchbase password and the Anthropic, OpenAI
and Gemini keys in a values file that stays out of git (not on the
command line with `--set`), and restart the operations-manager
Deployment after adding or rotating a key.

### Using an external Couchbase Enterprise server

By default `docker compose up` runs and fully manages its own bundled
Couchbase Server container - cluster init, RAM quotas, and bucket/scope/
collection/index provisioning all run against it on every startup (see
`couchbase-init/init.sh`). To point this appliance at a Couchbase
Enterprise server you already run and manage yourself instead:

1. In `.env`, set `COMPOSE_PROFILES=` (empty, or remove the line
   entirely). The bundled `couchbase` and `couchbase-init` containers only
   start under the `local-couchbase` profile, and are deliberately not
   started at all in external mode - `init.sh` reasserts cluster-wide
   data/index/fts RAM quotas on every run, which is fine for a container
   this appliance owns outright and not something you want run against a
   cluster other workloads share.
2. Set `COUCHBASE_CONNECTION_STRING` and `COUCHBASE_SEARCH_HOST` to your
   server, e.g. `couchbases://cb.example.internal` (TLS, typical for a
   real Enterprise cluster) and `cb.example.internal`.
3. Prepare the cluster as described in
   [Preparing an external cluster](#preparing-an-external-cluster) below:
   the bucket and scope, a dedicated RBAC user, and network access.
4. Set `COUCHBASE_BUCKET`, `COUCHBASE_SCOPE`, `COUCHBASE_USERNAME` and
   `COUCHBASE_PASSWORD` to match what you created.

Then `docker compose up --build` as usual - `sample-mcp-servers` and
`operations-manager` still come up the same way, just against your server
instead of the bundled one. Switch back to the bundled container at any
time by restoring `COMPOSE_PROFILES=local-couchbase`.

#### Preparing an external cluster

These steps apply to both Docker Compose and the Helm chart
(`couchbase.enabled=false`). In both, the bundled provisioning step
(`couchbase-init`) is skipped entirely in external mode, and
operations-manager's own startup creates everything inside the scope
instead.

**Cluster requirements.** Couchbase Server Enterprise Edition with the
Data, Index, Query and Search services running. Community Edition rejects
the vector-typed index field this appliance depends on.

**Bucket and scope (you create these).** Create a Couchbase-type bucket
and, inside it, a scope - these two are the only things operations-manager
won't create on its own. Both default to `agent_operations`; set
`COUCHBASE_BUCKET`/`COUCHBASE_SCOPE` if you use other names. Give the
bucket at least 1 GB of RAM quota (the bundled cluster's default); much
less pushes most reads to disk under real agent traffic.

Everything below the scope is created automatically at startup (see
`operations-manager/app/couchbase_client.py`): every collection, a primary
index on each, the secondary GSI indexes the dashboard pages depend on
(`SECONDARY_INDEXES`), and the Search/vector indexes. All of it is
`IF NOT EXISTS`, so this is the same self-healing path that lets an
appliance upgrade add a new collection or index without rerunning
`couchbase-init`. Creation is best-effort: if a step fails (usually a
missing role), operations-manager logs a `Could not create collection` or
`Could not ensure index` warning and carries on, so check the logs on
first boot. On a large existing collection an index build can outlast the
query timeout; the server finishes it in the background.

**A dedicated RBAC user.** Because operations-manager creates collections
and indexes itself, a plain read/write user isn't enough - it will
connect, log warnings when startup can't create collections and indexes,
and then fail on every page that needs one of them. It does *not* need `bucket_admin` or `cluster_admin`, and
nothing it needs reaches outside its own bucket:

| What operations-manager does | Role | Granted on |
|---|---|---|
| Reads/writes documents, TTLs, counters | `data_reader`, `data_writer` | `<bucket>:<scope>` |
| Dashboard queries and aggregates | `query_select` | `<bucket>:<scope>` |
| Purges, eviction and status updates via N1QL | `query_update`, `query_delete` | `<bucket>:<scope>` |
| Creates collections at startup | `scope_admin` ("Manage Scopes") | `<bucket>:<scope>` |
| Creates primary and secondary indexes | `query_manage_index` | `<bucket>:<scope>` |
| Creates the Search/vector indexes (REST, port 8094) | `fts_admin` | `<bucket>` (bucket-level only) |
| Runs vector and Search queries | `fts_searcher` | `<bucket>:<scope>` |

```bash
couchbase-cli user-manage -c cb.example.internal -u Administrator -p '<admin-password>' \
  --set --auth-domain local \
  --rbac-username aom --rbac-password '<password>' \
  --roles 'data_reader[<bucket>:<scope>],data_writer[<bucket>:<scope>],query_select[<bucket>:<scope>],query_update[<bucket>:<scope>],query_delete[<bucket>:<scope>],query_manage_index[<bucket>:<scope>],scope_admin[<bucket>:<scope>],fts_searcher[<bucket>:<scope>],fts_admin[<bucket>]'
```

`scope_admin`, `query_manage_index` and `fts_admin` are what let an
upgrade add a new collection or index without anyone stepping in. You can
remove them after first boot if your security review requires it, but
every later upgrade that adds a collection or index will then need those
created by hand before the new version starts (the full list is
`ALL_COLLECTIONS` and `SECONDARY_INDEXES` in `couchbase_client.py`).

**Network access.** The SDK connection follows your connection string
(`couchbases://` for TLS), but two other calls currently use plain HTTP,
so these ports must be reachable from operations-manager even on a
TLS-only cluster:

- **8094** (Search REST API) - creating and checking the Search/vector
  indexes. The port is `COUCHBASE_SEARCH_PORT` in operations-manager's
  config; on Helm you can override it through
  `operationsManager.extraEnv`, but `docker-compose.yml` doesn't pass it
  through yet, so under Compose it has to be 8094.
- **8091** (cluster REST API) - Helm only: the operations-manager Pod's
  `wait-for-dependencies` initContainer polls it until the bucket and
  scope exist. If only 18091 is open, the Pod stays in `Init` forever.

## HTTPS / TLS

The dashboard (nginx, published on the standard HTTPS port 443) and the Operations Manager API
(uvicorn, port 8090) both serve HTTPS by default, including the internal
proxy hop nginx makes to the API. Each image bakes in a self-signed
certificate at build time (`operations-manager/Dockerfile`,
`ui/Dockerfile`) so a fresh checkout gets TLS with zero configuration -
your browser will warn about the self-signed cert, and `curl`/SDK calls
need `-k` / `verify=False` (see the [Developer SDK](#developer-sdk)
section) until you swap in a real one.

**Using your own certificate - Settings page (recommended).** Log in as an
admin and open Settings → HTTPS Certificate: paste or upload a PEM
certificate (a leaf cert, or a leaf + intermediate chain concatenated) and
its matching unencrypted private key, validate the pair, then install.
This writes straight into `tls-shared`, a Docker volume shared by both
`operations-manager` and `ui` (see `docker-compose.yml`) - one upload
covers the dashboard and the API. Neither server hot-reloads a changed
certificate, so restart both afterward:

```bash
docker compose restart operations-manager ui
```

The page also shows the certificate currently in use (subject, issuer,
validity, and whether it's still the self-signed default) and can revert
back to that default if needed.

**Using your own certificate - bind mount (alternative).** If you'd rather
manage the files yourself instead of using the Settings page, drop a
`server.crt`/`server.key` pair into `operations-manager/tls/` and
`ui/tls/` (both are gitignored - never commit private keys), then in
`docker-compose.yml` comment out each service's `tls-shared:` volume line
and uncomment the bind-mount line right below it:

```yaml
# operations-manager service
volumes:
  - ./operations-manager/tls:/app/tls:ro

# ui service
volumes:
  - ./ui/tls:/etc/nginx/tls:ro
```

Only one mount can occupy each of those paths, so don't leave both
uncommented. Restart with `docker compose up --build` and both containers
pick up the mounted cert instead of the shared volume's - no other
configuration changes.

**Disabling TLS.** If you need plain HTTP (e.g. running behind a load
balancer that already terminates TLS), set `DISABLE_TLS=true` for
`operations-manager` and `OPERATIONS_MANAGER_SCHEME=http` for `ui` in your
`.env`, so nginx's proxy hop matches the API's actual scheme. The two
settings must agree, or nginx will fail to reach the API.

**Corporate CA for LDAP.** This is unrelated to the two settings above.
If you connect Settings → LDAP Authentication to a directory whose LDAPS
certificate (or StartTLS cert) is signed by an internal corporate CA, use
the "Install corporate CA certificate" option on that page to upload the
CA's PEM so the appliance can validate the LDAP server's certificate. This
is also unrelated to `scripts/setup-corporate-ca.sh`, which is a
build-time-only helper for trusting a TLS-inspecting proxy CA during
`docker compose build` (see the note under [Run it](#run-it)).

## Security Hardening

Beyond HTTPS/TLS, this repository has been hardened against a set of
industry security baselines: CIS Benchmarks, NIST SP 800-53 / SP 800-123,
DISA STIG control objectives, and PCI DSS v4.0. That work spans the
FastAPI backend (CORS lockdown, security response headers, a 12-character
password policy, login lockout after repeated failed attempts, auth
audit logging, default-credential warnings), the Docker Compose stack
(non-root containers, dropped Linux capabilities, `no-new-privileges`,
Couchbase's admin/data ports and the sample MCP servers bound to
localhost only), and nginx (a hardened TLS cipher suite, `server_tokens
off`, and the same security headers on the dashboard).

See [SECURITY_HARDENING.md](./SECURITY_HARDENING.md) for the full
change-by-change writeup, the specific standard/requirement each change
addresses, what was reviewed and already found compliant, and - just as
important - the gaps that remain (MFA for dashboard login and password
history are the two biggest) along with why they're not silently glossed
over.

## Using the dashboard

- **Dashboard** - stat cards (open findings, registered/trusted servers,
  tools ingested, access events), an access-volume chart, an allow/deny/
  error donut, and the highest-severity open findings.
- **MCP Servers** - the registered server list; register a new one (server
  ID, label, owner, MCP URL, trust status, default allowed roles) and its
  catalog is ingested immediately; re-ingest or unregister existing ones.
- **Tool Catalog** - every tool actually stored in Couchbase's registry,
  filterable by server/role - the full transparency view of what
  discovery can ever return.
- **Roles & RBAC** - the three seed roles and how many tools each can
  reach. API keys live in environment variables, never in the UI or API
  responses - only a masked `...last4` label is ever shown.
- **Threat Detection** - the MCP Tool Hijacking surface: quarantined tools
  with their matched signals and a one-click Release action, recently
  flagged live responses, cross-tool hijack chain findings, and the last
  background scan time. See [MCP Tool Hijacking
  detection](#mcp-tool-hijacking-detection) below.
- **Insights** - findings derived from the current catalog, server
  registry, and recent audit log: quarantined tools, cross-tool hijack
  chains, unclassified tools defaulting to admin-only, trusted servers
  with an empty catalog, repeated invalid API key attempts, invoke
  attempts against unregistered/untrusted tools, repeated RBAC denials for
  a role/tool pair, and critical-risk tool usage. Everything except the
  quarantine state itself is recomputed on every load, nothing extra
  stored.
- **LLM Caching -> Cache Dashboard** - tokens saved, estimated cost saved,
  hit rate and latency avoided; hits-vs-provider-calls over the last 12
  hours; how requests were resolved (exact / semantic / provider call /
  bypassed / error); savings broken down by provider and model; the cache
  contents with each entry's live policy verdict; and the recent cache
  event stream.
- **LLM Caching -> Providers & Policy** - pick Claude, ChatGPT or Gemini
  and a model, tune exact/semantic matching, configure every invalidation
  rule, and send a test completion to watch a miss turn into a hit. See
  [LLM caching for agents](#llm-caching-for-agents).
- **Audit Log** - every discover/invoke/authenticate decision, live-
  refreshing, filterable by action and decision.
- **Agent Tool Audit** - paste in a role's API key and call `discover`/`invoke`
  directly (no LLM in the loop) to see exactly what the RBAC + vector
  pre-filter returns. Try `shadow-diagnostics::run_diagnostic` as a tool
  ID in the Invoke panel - it's intentionally never registered, so it's
  denied no matter which role you use.
- **Tools -> Developer SDK** - install guide and quickstart for the official
  Python client (`aom_sdk`) - discover/invoke, cached completions, agent
  memory, and MCP tool integration - plus download buttons for the SDK
  itself and for AI-assistant integration skills (Claude/ChatGPT/Gemini).
  See [Developer SDK](#developer-sdk) below.
- **Tools -> API Documentation** - operations-manager's full REST API
  reference (every route, request/response schema, and a try-it-out
  console), generated straight from the code so it can't drift out of
  sync. Opens in a new tab at this same origin's `/docs` - the dashboard's
  nginx proxies it straight through from operations-manager (see
  `ui/nginx.conf.template`), so it works the same way whether
  operations-manager itself is reachable directly (docker-compose.yml
  publishes it on `:8090`) or not (the Helm chart keeps it
  cluster-internal only).

## MCP Tool Hijacking detection

MCP Tool Hijacking - usually delivered as a Tool Poisoning Attack - is an
indirect prompt injection vulnerability that's structural to MCP, not a
bug in any one server: an LLM ingests tool *descriptions* and tool *call
results* into the same trusted working context it reasons over, with no
built-in way to tell "text from a vetted source" apart from "text from
anywhere." A hidden instruction in one tool's metadata or output can
shadow, override, or redirect execution toward a completely different,
higher-privilege tool - data exfiltration, privilege escalation, and
silent execution (especially under an "always allow" auto-approval
config) are the usual payoffs. RBAC and server-trust review, on their
own, only stop tools that were never registered in the first place; they
don't catch a tool that looked fine at review time and was compromised
afterward, or a clean tool whose live response is what's actually
poisoned. This appliance runs three complementary defenses for that
(`operations-manager/app/hijack_detection.py`):

1. **Metadata poisoning**, caught at ingest time. Every tool's name,
   description, and input-schema property descriptions are scanned
   against a pattern bank (instruction-override language, covert/silent-
   execution cues, data-exfiltration cues, hidden-content markers like
   HTML comments or zero-width characters, and privilege-escalation
   language) the moment it's ingested. A match quarantines the tool
   immediately - `trust_status` is forced to `quarantined`, which the
   RBAC + vector Search pre-filter already excludes, the same way an
   untrusted server's tools are excluded. It's never discoverable or
   invokable by any role until released from the Threat Detection page.
2. **Response payload poisoning**, caught live. A tool's description can
   be completely clean and still return a poisoned payload at call time -
   a compromised web page, a tampered ticket body. That can't be caught
   at ingest because it doesn't exist yet, so every successful `invoke`
   response is scanned instead. A match doesn't withhold the response
   (the same words that flag an attack show up in a lot of harmless data
   too), but it's logged loudly: flagged on that audit-log entry and
   surfaced on Threat Detection and Insights.
3. **Cross-tool hijack chains**, the actual mechanism the first two
   defenses exist to catch in the act: a response-poisoning-flagged
   invoke by some caller, followed within `HIJACK_CHAIN_WINDOW_SECONDS`
   (default 120s) by that same caller invoking a materially higher-risk
   tool, is exactly the shape a successful hijack takes - the poisoned
   response steering the next tool call. This is a timing correlation
   over the audit log, not a confirmed compromise, so it's surfaced as a
   lead to investigate rather than an automatic block.

A background monitor also re-scans every already-ingested tool's stored
description against the current pattern bank on a timer
(`HIJACK_SCAN_INTERVAL_MINUTES`, default 5) with no MCP round-trip - this
is what catches a tool that was ingested before hijack detection existed,
or before a pattern-bank update, without hammering downstream MCP
servers to do it. A manual release or quarantine from the Threat
Detection or Tool Catalog page is remembered (`hijack_manual_override`)
so the next scan pass doesn't silently revert an admin's decision.

**Try it**: `docs-search::search_docs` (a registered, trusted sample
server) is quarantined automatically the moment it's ingested - open the
Threat Detection page to see why, and Release it to confirm it becomes
invokable again. `web-search::fetch_page` ingests and invokes completely
normally - its description is clean - but its mock response is poisoned;
invoke it from the Agent Tool Audit as any role, then invoke
`snowflake::manage_users` shortly after as `admin`, and watch a critical
cross-tool hijack chain finding appear on Insights and Threat Detection.

## LLM caching for agents

The same argument as the tool gateway, applied to model calls: an agent
that routes its completions through one governed choke point gets policy,
an audit trail - and a cache. Point an agent at `POST /v1/llm/complete`
with the API key it already uses for tool discovery and the operations
manager answers from Couchbase whenever it can, so the tokens are never
spent at all.

### Choosing the LLM

Claude, ChatGPT, Gemini and Databricks Model Serving are all selectable
from **LLM Caching -> Providers & Policy**, each with its own model list
and list-price estimates (`operations-manager/app/llm_cache.py`). Set
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `GEMINI_API_KEY` in `.env` to
proxy calls for real. For Databricks, set `DATABRICKS_HOST` (the
workspace URL) and `DATABRICKS_TOKEN`; each "model" is a serving endpoint
name, and `DATABRICKS_SERVING_ENDPOINTS` (comma-separated) adds your own
custom or provisioned-throughput endpoints to the list. On Kubernetes, see
[LLM provider API keys](./helm/couchbase-agent-operations-manager/README.md#llm-provider-api-keys)
in the chart README. Keys are read at startup, so restart
operations-manager after adding or changing one
(`docker compose restart operations-manager`).

**No key is required to try it.** A provider with no key configured
answers a cache miss from a clearly-labelled deterministic stub, so
caching, savings accounting and invalidation all behave identically with
no outbound network access - the same "works on first boot" posture as the
bundled sample MCP servers. A key that *is* configured and then fails
raises, rather than silently writing fabricated text into the cache.

### How a prompt matches

1. **Exact** - SHA-256 of the normalized prompt (whitespace collapsed,
   case-folded) plus provider, model, namespace, scope and parameters.
   Resolved with a single KV get on a deterministic document ID.
2. **Semantic** - the fallback that catches paraphrases, reusing the
   catalog's own pattern: a Couchbase Search request combining vector kNN
   over the prompt embedding with a Conjunction pre-filter on provider,
   model, scope and namespace, so an entry belonging to another model or
   another tenant can never be returned however similar the prompt.
   Anything below the configured similarity threshold is a miss.

### Cache invalidation

Everything below is configurable during setup, evaluated by one function
(`llm_cache.evaluate_entry`) that the read path, the background sweeper
and the Cache Entries table all share - so what the UI shows and what the
gateway does cannot drift apart.

| Option | What it does |
|---|---|
| **TTL** | Written as a Couchbase document expiry *and* checked on read, so the cluster reclaims space even if the sweeper never runs. |
| **Stale-while-revalidate** | Grace window after the TTL during which a past-due answer is still served. |
| **Max entries + eviction policy** | LRU, LFU or FIFO once the cache is full. |
| **Max reuses per entry** | Retire an answer after N hits so a hot prompt is periodically re-verified against the provider. |
| **On model change** | Drop answers produced by the previously selected model. Entries created by an explicit per-request override are kept. |
| **On policy change** | Drop everything when a setting that changes what an answer *means* moves - namespace, cache scope, similarity threshold, generation parameters. |
| **On catalog change** | Drop everything when the vetted tool catalog changes, since an agent's answer can depend on which tools it was allowed to see. |
| **Cache scope** | `global`, `per_role` or `per_subject` - trade hit rate for isolation. |
| **Namespace** | A soft invalidation lever: bump it (after a prompt-template change, say) and older entries stop matching without deleting anything. |
| **Never-cache rules** | Regex patterns for time-sensitive prompts, plus RBAC roles that always bypass the cache. |
| **Manual purge** | Everything, or narrowed to one provider, model or namespace. |

A background sweeper applies the rules on a timer to entries nobody reads;
a read that notices a stale entry deletes it on the spot; and saving a
policy that invalidates runs the sweep immediately, so the setup page
reports what it just invalidated.

### Calling it

```bash
curl -k -X POST https://localhost:8090/v1/llm/complete \
  -H "Authorization: Bearer demo-admin-4c56" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Summarise the RBAC model in three sentences."}'
```

The response carries the answer plus `cache.status`
(`hit_exact` / `hit_semantic` / `miss` / `bypass`), the similarity that
earned a semantic hit, token usage, and what the hit saved. Send the same
prompt twice to watch the second one cost nothing. `provider` and `model`
can be overridden per request; `bypass_cache: true` forces a live call,
and `semantic: false` limits that call to exact matches (for prompts that
embed fetched data, where a near-identical prompt can carry different
facts).

| Endpoint | Purpose |
|---|---|
| `GET /v1/llm/providers` | Selectable LLMs, their models and pricing, and which have a key configured (never the key). |
| `GET` / `PUT /v1/llm/config` | Read and save the cache policy. Every value is re-validated server-side. |
| `POST /v1/llm/complete` | The caching gateway. Authenticated exactly like discover/invoke. |
| `GET /v1/llm/dashboard` | Savings, hit rate, hourly series and per-model breakdown. |
| `GET /v1/llm/cache` | Cache contents with each entry's live policy verdict. |
| `POST /v1/llm/cache/purge` | Manual invalidation, optionally filtered. |
| `POST /v1/llm/cache/sweep` | Run the invalidation sweeper now. |
| `DELETE /v1/llm/cache/{entry_id}` | Invalidate one entry. |

## Developer SDK

A typed Python client (`aom_sdk`) for the discover / invoke / complete /
memory gateway ships with this appliance, so integrating an agent doesn't
mean hand-rolling bearer headers and JSON payloads against the raw REST
API. Get it from **Tools -> Developer SDK** in the dashboard - the page
has the full install guide, a quickstart, and a Download SDK button - or
from `operations-manager/sdk/` directly in this repo. The download is
built fresh from that source on every request (`GET /v1/sdk/download`),
so it always matches the API this appliance is actually running.

```python
from aom_sdk import AOMClient

client = AOMClient("https://localhost:8090", api_key="demo-support-agent-9f21", verify=False)

discovered = client.discover("look up a customer's open support tickets")
result = client.invoke(discovered["tools"][0]["tool_id"], arguments={})

answer = client.complete("Summarize this ticket thread in two sentences.")
print(answer["response"], answer["cache"]["status"])

client.add_memory("user-42", "Prefers responses in metric units.", memory_type="profile")
relevant = client.search_memory("user-42", "does this user use metric or imperial?")
```

Non-2xx responses raise typed exceptions (`AOMAuthenticationError`,
`AOMAuthorizationError`, `AOMNotFoundError`, `AOMServerError`,
`AOMConnectionError`) instead of a bare HTTP status code. See
`operations-manager/sdk/README.md` and the bundled `examples/` for the
rest, including a worked example of the LLM-caching cost/latency savings
described above at agent-fleet scale.

### Agent memory

Durable, cross-session recall stored in the same Couchbase cluster as
everything else in this appliance (`agent_memory` collection + its own
Search vector index) - not a separate service to run. `POST /v1/memory`
embeds and stores an entry scoped to a `user_id` (and optionally a
`session_id`/`memory_type`); `POST /v1/memory/search` recalls the entries
closest in meaning to a new query, the same RBAC-free vector-search
pattern `discover_tools()` runs over the tool catalog. `GET /v1/memory`,
`DELETE /v1/memory/{memory_id}` and `POST /v1/memory/clear` round out the
surface. All four authenticate exactly like discover/invoke/complete.

### MCP tool integration

AOM already speaks MCP to every downstream tool server it proxies to (see
`operations-manager/app/mcp_client.py`); the SDK makes that protocol
visible on the client side too: `client.discover_mcp_tools(query)` returns
matched tools already converted to standard MCP tool definitions
(`{"name", "description", "inputSchema"}`), and the optional
`aom_sdk.mcp_server` bridge (`pip install "couchbase-aom-sdk[mcp]"`) runs
this appliance as a real local MCP server over stdio, so any MCP host can
attach to it directly - still governed by AOM's RBAC and audit trail.

### AI assistant integration skills

The same integration knowledge as the guide above, packaged so a coding
assistant can apply it to a codebase directly: a real Claude Skill
(`operations-manager/skills/claude/SKILL.md`), plus equivalent packages for
ChatGPT and Gemini (`operations-manager/skills/{chatgpt,gemini}/`) - those
two platforms have no single portable skill-file format, so they ship as
plain instructions text with a README on where to paste it (Custom GPT
instructions, an Assistants/Responses system message, an `AGENTS.md` file,
a `GEMINI.md` context file, or a Gem's instructions). All three download
from **Tools -> Developer SDK**, or `GET /v1/skills/{claude,chatgpt,gemini}/download`.

## Registering your own MCP servers

Point the Servers page (or `POST /v1/servers`) at any Streamable-HTTP MCP
endpoint your infrastructure runs. A server's tools are only ever ingested
if it's marked `trusted` - leave a newly-added server `untrusted` until
you've reviewed it, then flip it to trusted and hit Re-ingest.

```bash
# /v1/servers is part of the dashboard's admin surface, so this needs a
# logged-in session cookie (log in via /v1/auth/login and pass its
# Set-Cookie back with -b) - the payload shape below is what matters here.
curl -k -X POST https://localhost:8090/v1/servers \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -d '{
        "server_id": "billing-service",
        "label": "Billing Service (Internal)",
        "owner": "Platform Team",
        "mcp_url": "http://billing-service.internal:9000/mcp",
        "trust_status": "trusted",
        "default_allowed_roles": ["admin"]
      }'
```

## Notes

- The bundled sample MCP servers return small, representative mock data -
  no real Jira/Zendesk/Snowflake credentials, and no real network access,
  are needed to try the appliance (or the hijacking-detection fixtures)
  out of the box.
- API keys in `.env.example` are placeholder values only - rotate them
  before exposing this appliance beyond your laptop.
- Audit log entries expire after `AUDIT_LOG_RETENTION_HOURS` (default 30
  days) via a Couchbase document TTL.
- LLM cache events expire after `LLM_CACHE_LOG_RETENTION_HOURS` (default
  30 days). The savings dashboard is computed from those events, so that
  setting is also how far back "tokens saved" can look.
- Cost figures on the LLM Caching dashboard are list-price estimates from
  the table in `operations-manager/app/llm_cache.py`, not billing data.
  Edit that table for your own negotiated rates.
- Caching is most defensible at temperature 0: a deterministic prompt
  should have a deterministic answer. The default policy sets it there.
- The hijack pattern bank is heuristic, not proof of malicious intent - it
  is deliberately tuned toward catching real attacks over avoiding every
  false positive, since the cost of a false positive is one click on
  Release and the cost of a false negative is a live prompt-injection
  payload sitting in the catalog unnoticed.

## License

Couchbase Agent Operations Manager is released under the [MIT License](./LICENSE).

It runs on Couchbase Server Enterprise Edition, which is licensed separately
under Couchbase's own terms; the MIT license covers this repository's code
only.
