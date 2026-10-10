# Monitoring with Prometheus and Grafana

`operations-manager` exposes Prometheus metrics at **`GET /metrics`** on its
API listener (`:8090`, HTTPS unless `DISABLE_TLS=true`). The exporter is
in-process (`operations-manager/app/metrics.py`): a scrape never touches
Couchbase, so a 15s interval is safe on an appliance under load.

| Setting | Default | Meaning |
|---|---|---|
| `METRICS_ENABLED` | `true` | `false` → `/metrics` answers 404 and the counters are no-ops |
| `METRICS_TOKEN` | empty | When set, scrapers must send `Authorization: Bearer <token>` |

The dashboard session middleware never guards `/metrics`; the token is the
only auth on it. Leave it empty when `:8090` is only reachable inside the
Compose network or the cluster (the Helm chart's Service is ClusterIP), and
set it whenever the port is exposed further.

## What is measured

| Metric | Labels | Recorded from |
|---|---|---|
| `aom_http_requests_total`, `aom_http_request_duration_seconds`, `aom_http_requests_in_flight` | `method`, `route` (template, e.g. `/v1/servers/{server_id}`), `status` | every request, via middleware |
| `aom_gateway_decisions_total`, `aom_gateway_latency_seconds` | `action` (discover/invoke), `decision` (ALLOW/DENY), `role`, `server_id` | `CouchbaseStore.log_access()` — the same choke point as the Audit Log and SIEM forwarding |
| `aom_hijack_flags_total` | `severity`, `role` | tool responses flagged by hijack detection |
| `aom_llm_cache_events_total`, `aom_llm_tokens_saved_total`, `aom_llm_cost_saved_usd_total`, `aom_llm_cost_usd_total`, `aom_llm_latency_seconds` | `outcome` (hit_exact/hit_semantic/miss/bypass/error), `provider`, `model`, `role` | every `/v1/llm/complete` event |
| `aom_context_cache_events_total`, `aom_context_latency_saved_seconds_total` | `outcome`, `namespace` | every context-cache get/set |
| `aom_trace_spans_total` | `kind`, `status` | every span written |
| `aom_siem_forward_total` | `vendor`, `status` | each delivery attempt per destination |
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
files inside the chart). After editing either source file run
`python3 scripts/sync-monitoring-assets.py`.

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
