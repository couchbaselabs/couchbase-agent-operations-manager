# Monitoring with Prometheus and Grafana

`operations-manager` exposes Prometheus metrics at **`GET /metrics`** on its
API listener (`:8090`, HTTPS unless `DISABLE_TLS=true`). The exporter is
in-process (`operations-manager/app/metrics.py`): a scrape never touches
Couchbase, so a 15s interval is safe on an appliance under load.

| Setting | Default | Meaning |
|---|---|---|
| `METRICS_ENABLED` | `true` | `false` → `/metrics` answers 404 and the counters are no-ops |
| `METRICS_TOKEN` | empty | When set, scrapers must send `Authorization: Bearer <token>` |

The dashboard session middleware never guards `/metrics`; a bearer token is
the only auth on it, and it accepts either of:

- the **scrape token** generated under **Settings -> Monitoring** (only its
  SHA-256 hash is stored; generating a new one invalidates the old one at
  once; it grants nothing but `/metrics`), or
- **`METRICS_TOKEN`** from the environment (what the bundled Prometheus and
  the Helm chart's ServiceMonitor use).

With neither configured the endpoint is open - fine when `:8090` is only
reachable inside the Compose network or the cluster (the Helm chart's
Service is ClusterIP); set one whenever the port is exposed further. The
**Serve /metrics** switch on the same page makes the endpoint answer 404
without a restart.

## Settings -> Monitoring

Everything below can be done from the dashboard (admin only; stored in
Couchbase at `settings::monitoring`, so it survives a restart and a
redeploy). The page mirrors the Couchbase Relational Database Service's
console:

- **Prometheus set-up** - pick the targets (`host:port` as Prometheus
  reaches the operations manager), generate a scrape token, copy or
  download a `scrape_configs` job (token in a `credentials_file` or inline),
  then **Check targets**: the operations manager asks Prometheus's own API
  (`/api/v1/targets`) whether every AOM target is being scraped and is up,
  and shows the last scrape error per target if not.
- **Grafana set-up** - Grafana URL plus a service-account token (or user and
  password), and the Prometheus URL *as Grafana reaches it* (inside Compose
  that is `http://prometheus:9090`). The operations manager creates or
  updates the `AOM Prometheus` data source (uid `aom-prometheus`), checks its
  health, and installs the *Couchbase Agent Operations Manager* folder and
  the `aom-overview` dashboard through Grafana's HTTP API. Repeatable; the
  Grafana credential is used for that one call and never stored.
  **Dashboard JSON** downloads the same file for a manual import. The file
  the wizard installs is `operations-manager/app/resources/grafana-dashboard.json`
  (a copy of `monitoring/grafana/dashboards/aom-overview.json` - see below).
- **Push exporters** - Datadog (Metrics API v2 series), Dynatrace (Metrics
  API v2 line protocol) and New Relic (Metric API). Credentials, a **Test
  push**, then turn it on. One background thread collects the same samples
  a scrape sees once per push interval (default 60 s) and sends one
  gzip-compressed request to each vendor that is on; counters go as deltas,
  names are dotted (`aom.http_requests_total`), `service` and `host` ride
  along as tags/dimensions. Nothing runs while every exporter is off.
  Secrets are encrypted at rest and never returned to the browser (they
  read as `********`; sending that back keeps the stored value).
- **Push status by service** - pushes, failures, last success and last
  error per exporter.

The Compose and Helm set-ups below still work unchanged; the page is for
pointing an existing Prometheus/Grafana (or a SaaS vendor) at the appliance
without editing files.

## Settings -> Log Forwarding

The sibling page ships the operations manager's **application log** (not the
audit log - that is SIEM forwarding on the Audit Log page) to Splunk (HEC),
Elastic (Bulk API, ECS fields), Datadog (logs intake v2), Sumo Logic (HTTP
source), Graylog (GELF 1.1 over TCP/TLS/UDP) or any RFC 5424 syslog server
(UDP/TCP/TLS, octet-counting or newline framing). Same design as CRDS: the
logging handler does one level comparison and a non-blocking put into a
bounded queue; one daemon thread sends batches (batch size or flush
interval, whichever first) over kept-alive connections; a failing
destination backs off exponentially (max 60 s, 4xx not retried) and its
batches are dropped and counted; a full queue drops and counts rather than
blocking. Settings: master switch, minimum level, batch size, flush
interval, queue capacity, stack traces, maximum message length; every
destination has **Send test**. Delivery counts are also exposed as
`aom_log_forwarding_destination_records_total{destination,result}` (see
the metrics table below). Stored at `settings::log_forwarding`; the code is
`operations-manager/app/log_forwarding.py`.

## What is measured

| Metric | Labels | Recorded from |
|---|---|---|
| `aom_http_requests_total`, `aom_http_request_duration_seconds`, `aom_http_requests_in_flight` | `method`, `route` (template, e.g. `/v1/servers/{server_id}`), `status` | every request, via middleware |
| `aom_gateway_decisions_total`, `aom_gateway_latency_seconds` | `action` (discover/invoke), `decision` (ALLOW/DENY), `role`, `server_id` | `CouchbaseStore.log_access()` — the same choke point as the Audit Log and SIEM forwarding |
| `aom_hijack_flags_total` | `severity`, `role` | tool responses flagged by hijack detection |
| `aom_llm_cache_events_total`, `aom_llm_tokens_saved_total`, `aom_llm_cost_saved_usd_total`, `aom_llm_cost_usd_total`, `aom_llm_latency_seconds` | `outcome` (hit_exact/hit_semantic/miss/bypass/error), `provider`, `model`, `role` | every `/v1/llm/complete` event |
| `aom_context_cache_events_total`, `aom_context_latency_saved_seconds_total` | `outcome`, `namespace` | every context-cache get/set |
| `aom_trace_spans_total` | `kind`, `status` | every span written |
| `aom_siem_forward_total` | `vendor`, `status` | each audit-log delivery attempt per SIEM destination |
| `aom_log_forwarding_destination_records_total` | `destination`, `result` (sent/failed) | application-log records per log-forwarding destination |
| `aom_couchbase_connected` | — | 1 while the SDK connection is up |
| `aom_build_info` | `version`, `appliance` | startup |

Label values are all bounded (code-defined roles, the provider catalog,
registered server IDs, the six SIEM vendors, route templates) — nothing
user-typed becomes a label.

## Docker Compose

```bash
# .env
COMPOSE_PROFILES=local-couchbase,monitoring
GRAFANA_ADMIN_PASSWORD=<something>
```

`docker compose up -d` then adds **Prometheus** (http://localhost:9090,
scraping `operations-manager:8090` over its self-signed TLS) and
**Grafana** (http://localhost:3000) with the Prometheus datasource and the
*Couchbase Agent Operations Manager* dashboard already provisioned.
Config lives in `monitoring/`:

```
monitoring/
├── prometheus/prometheus.yml            # scrape config
├── prometheus/aom-alerts.yml            # alerting rules (also the chart's PrometheusRule)
└── grafana/
    ├── provisioning/                    # datasource + dashboard provider
    └── dashboards/aom-overview.json     # the dashboard (also the chart's ConfigMap)
```

Already running Prometheus somewhere else? Just add a scrape job:

```yaml
- job_name: aom
  scheme: https
  tls_config: { insecure_skip_verify: true }   # or ca_file once a real cert is installed
  static_configs: [{ targets: ["<host>:8090"] }]
```

## Kubernetes (Helm chart)

`metrics.*` in `values.yaml`:

- `metrics.serviceAnnotations: true` (default) — `prometheus.io/scrape|path|port|scheme`
  on the operations-manager Service for annotation-based scrapers.
- `metrics.serviceMonitor.enabled` — a `ServiceMonitor` for the Prometheus
  Operator / kube-prometheus-stack. Set `metrics.serviceMonitor.labels` to
  what its `serviceMonitorSelector` expects (`release: <stack release name>`
  by default). Requires the `monitoring.coreos.com` CRDs, so it is off by
  default.
- `metrics.prometheusRule.enabled` — the alert rules as a `PrometheusRule`.
- `metrics.grafanaDashboard.enabled` — the dashboard as a ConfigMap labelled
  `grafana_dashboard: "1"` for Grafana's dashboard sidecar.
- `metrics.token` — stored in the app Secret; the ServiceMonitor reads it
  from there.

```bash
helm upgrade --install aom ./helm/couchbase-agent-operations-manager \
  --set metrics.serviceMonitor.enabled=true \
  --set metrics.serviceMonitor.labels.release=kube-prometheus-stack \
  --set metrics.prometheusRule.enabled=true \
  --set metrics.prometheusRule.labels.release=kube-prometheus-stack \
  --set metrics.grafanaDashboard.enabled=true
```

The chart embeds copies of `aom-alerts.yml` and `aom-overview.json` under
`helm/couchbase-agent-operations-manager/monitoring/` (Helm can only read
files inside the chart), and the operations-manager image carries a third
copy of the dashboard at `operations-manager/app/resources/grafana-dashboard.json`
for the Settings -> Monitoring Grafana wizard. After editing either source
file run `python3 scripts/sync-monitoring-assets.py` to refresh all three.

### Terraform

`terraform/<cloud>` can install kube-prometheus-stack alongside AOM and turn
all three chart switches on:

```hcl
monitoring = {
  install_stack          = true
  grafana_admin_password = "..."
}
```

`terraform output grafana_port_forward` prints the `kubectl port-forward`
for Grafana. See `terraform/README.md`.

## Alerts shipped

`AOMCouchbaseDisconnected` (critical), `AOMHighErrorRate`, `AOMSlowRequests`,
`AOMHijackFlags`, `AOMDenialSpike`, `AOMSiemForwardingFailing`,
`AOMLlmCacheHitRateLow` — thresholds in `monitoring/prometheus/aom-alerts.yml`
are conservative starting points; tune per deployment.

## Not covered (yet)

- Couchbase Server's own metrics. The bundled server exposes them on
  `:8091/metrics` (Prometheus format, needs cluster credentials); for a
  self-managed cluster use Couchbase's own Prometheus integration, for
  Capella its built-in metrics. The AOM dashboard deliberately stays
  appliance-scoped.
- `ui`'s nginx has no exporter; its request volume is visible through the
  `/v1`/`/api` routes it proxies.
