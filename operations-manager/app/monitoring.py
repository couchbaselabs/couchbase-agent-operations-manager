"""Settings -> Monitoring: the same capability the Couchbase Relational
Database Service's Admin Console has, ported to this appliance.

Three products pull or receive the metrics app/metrics.py already keeps:

  Prometheus  pulls GET /metrics. This module owns the *scrape token*
              (generated in the console, only its SHA-256 hash is stored,
              grants nothing but /metrics), the "Serve /metrics" switch,
              the scrape_configs job to paste, and "Check targets" - which
              asks Prometheus's own API (/api/v1/targets) whether every job
              that scrapes this appliance is up.
  Grafana     the wizard creates or updates the `AOM Prometheus` data
              source (uid aom-prometheus), checks its health, and installs
              the `Couchbase Agent Operations Manager` folder + overview
              dashboard through Grafana's HTTP API. Repeatable; the Grafana
              credential is used once and never stored. "Dashboard JSON"
              serves the same file for a manual import.
  Datadog / Dynatrace / New Relic
              pushed. One daemon thread, one collection per interval
              (default 60 s) shared by every vendor, one gzip-compressed
              request each. Counters are sent as deltas. Nothing is
              scheduled while no exporter is on.

Collection reads the prometheus_client registry - the same samples a
scrape sees - so a pushed metric is byte-for-byte the scraped one with the
name dotted (aom.http_requests_total) and `service`/`host` as tags.

Config lives at settings::monitoring (config.MONITORING_SETTINGS_DOC). The
exporter catalog follows the same {name,label,input,...} field shape as
app/log_forwarding.VENDORS so the console renders both with one component.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from app import user_auth
from app.log_forwarding import DATADOG_SITES, MASK, _clamp, _f, _verify_tls  # shared field helpers

logger = logging.getLogger("operations-manager.monitoring")

SERVICE = "operations-manager"
NODE = os.getenv("HOSTNAME") or socket.gethostname()
_HTTP_TIMEOUT = 15

DATASOURCE_UID = "aom-prometheus"
DATASOURCE_NAME = "AOM Prometheus"
FOLDER_TITLE = "Couchbase Agent Operations Manager"
DASHBOARD_UID = "aom-overview"

EXPORTERS: list[dict] = [
    {
        "id": "datadog", "label": "Datadog", "product": "Datadog Infrastructure / Metrics", "openSource": False,
        "transport": "Metrics API v2 (series), pushed",
        "description": "Pushes every metric to Datadog on the export interval.",
        "fields": [
            _f("site", "Datadog site", "select", "datadoghq.com", True, "", None, DATADOG_SITES),
            _f("apiKey", "API key", "secret", "", True),
            _f("tags", "Tags", "text", "", False, "env:prod", "Comma-separated, added to every series."),
        ],
    },
    {
        "id": "dynatrace", "label": "Dynatrace", "product": "Dynatrace", "openSource": False,
        "transport": "Metrics API v2 line protocol, pushed",
        "description": "Pushes metrics to your environment's metrics ingest endpoint.",
        "fields": [
            _f("url", "Environment URL", "text", "", True, "https://abc12345.live.dynatrace.com", "Managed: https://<domain>/e/<environment-id>."),
            _f("apiToken", "API token", "secret", "", True, "", "Needs the metrics.ingest scope."),
            _verify_tls(),
        ],
    },
    {
        "id": "newrelic", "label": "New Relic", "product": "New Relic", "openSource": False,
        "transport": "Metric API, gzip, pushed",
        "description": "Pushes dimensional metrics to New Relic's Metric API.",
        "fields": [
            _f("region", "Data center", "select", "US", True, "", None, ["US", "EU"]),
            _f("licenseKey", "License key", "secret", "", True, "", "An INGEST - LICENSE key."),
        ],
    },
]
EXPORTER_BY_ID = {v["id"]: v for v in EXPORTERS}


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "prometheusEnabled": True,
    "scrapeTokenHash": "",
    "scrapeTokenHint": "",
    "scrapeTokenCreatedAt": "",
    "scrapeIntervalSeconds": 15,
    "prometheusUrl": "",
    "grafanaUrl": "",
    "grafanaPrometheusUrl": "",
    "grafanaDashboardUrl": "",
    "grafanaAppliedAt": "",
    "pushIntervalSeconds": 60,
    "exporters": {},
}


def normalize_config(raw: dict | None, *, previous: dict | None = None) -> dict:
    raw = raw or {}
    prev = previous or {}
    cfg = {
        "prometheusEnabled": bool(raw.get("prometheusEnabled", prev.get("prometheusEnabled", True))),
        # Token fields are only ever written by generate_scrape_token(); the
        # console cannot set them, so they carry over from `previous`.
        "scrapeTokenHash": str(prev.get("scrapeTokenHash") or raw.get("scrapeTokenHash") or ""),
        "scrapeTokenHint": str(prev.get("scrapeTokenHint") or raw.get("scrapeTokenHint") or ""),
        "scrapeTokenCreatedAt": str(prev.get("scrapeTokenCreatedAt") or raw.get("scrapeTokenCreatedAt") or ""),
        "scrapeIntervalSeconds": _clamp(raw.get("scrapeIntervalSeconds", prev.get("scrapeIntervalSeconds")), 5, 300, 15),
        "prometheusUrl": str(raw.get("prometheusUrl", prev.get("prometheusUrl")) or "").strip()[:500],
        "grafanaUrl": str(raw.get("grafanaUrl", prev.get("grafanaUrl")) or "").strip()[:500],
        "grafanaPrometheusUrl": str(raw.get("grafanaPrometheusUrl", prev.get("grafanaPrometheusUrl")) or "").strip()[:500],
        "grafanaDashboardUrl": str(raw.get("grafanaDashboardUrl", prev.get("grafanaDashboardUrl")) or "").strip()[:500],
        "grafanaAppliedAt": str(raw.get("grafanaAppliedAt", prev.get("grafanaAppliedAt")) or ""),
        "pushIntervalSeconds": _clamp(raw.get("pushIntervalSeconds", prev.get("pushIntervalSeconds")), 10, 3600, 60),
        "exporters": {},
    }
    prev_ex = prev.get("exporters") or {}
    for vid, vendor in EXPORTER_BY_ID.items():
        d = (raw.get("exporters") or {}).get(vid)
        pd = prev_ex.get(vid) or {}
        if d is None:
            d = pd
        fields_in = dict((d or {}).get("fields") or {})
        out: dict = {}
        for f in vendor["fields"]:
            name = f["name"]
            val = fields_in.get(name)
            if f["input"] == "secret":
                enc_key = f"{name}_encrypted"
                if val is None or val == "" or val == MASK:
                    if (pd.get("fields") or {}).get(enc_key):
                        out[enc_key] = pd["fields"][enc_key]
                    elif fields_in.get(enc_key):
                        out[enc_key] = fields_in[enc_key]
                else:
                    out[enc_key] = user_auth.encrypt_secret(str(val))
                continue
            if val is None or val == "":
                val = f["default"]
            if f["input"] == "bool":
                val = "true" if str(val).lower() in ("true", "1", "yes", "on") else "false"
            elif f["input"] == "select" and f["options"] and str(val) not in f["options"]:
                val = f["default"]
            out[name] = str(val)[:1000]
        cfg["exporters"][vid] = {"type": f"metrics:{vid}", "enabled": bool((d or {}).get("enabled", False)), "fields": out}
    return cfg


def public_config(cfg: dict) -> dict:
    out = {k: v for k, v in cfg.items() if k not in ("exporters", "scrapeTokenHash")}
    out["exporters"] = {}
    for vid, d in (cfg.get("exporters") or {}).items():
        fields = {}
        for f in EXPORTER_BY_ID[vid]["fields"]:
            if f["input"] == "secret":
                fields[f["name"]] = MASK if (d.get("fields") or {}).get(f"{f['name']}_encrypted") else ""
            else:
                fields[f["name"]] = (d.get("fields") or {}).get(f["name"], f["default"])
        out["exporters"][vid] = {"type": f"metrics:{vid}", "enabled": bool(d.get("enabled")), "fields": fields}
    return out


def _get(vid: str, d: dict, name: str) -> str:
    fields = d.get("fields") or {}
    f = next((x for x in EXPORTER_BY_ID[vid]["fields"] if x["name"] == name), None)
    if f and f["input"] == "secret":
        enc = fields.get(f"{name}_encrypted")
        if not enc:
            return ""
        try:
            return user_auth.decrypt_secret(enc)
        except Exception:  # noqa: BLE001
            return ""
    return str(fields.get(name) or "")


def missing_required(vid: str, d: dict) -> list[str]:
    out = []
    for f in EXPORTER_BY_ID[vid]["fields"]:
        if f["required"] and not (_get(vid, d, f["name"]).strip() or (f["input"] != "secret" and f["default"])):
            out.append(f["label"])
    return out


# ---------------------------------------------------------------------------
# Scrape token
# ---------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_scrape_token(cfg: dict) -> tuple[dict, str]:
    """Returns (new cfg, plaintext token). The plaintext is shown once."""
    token = "aom_scrape_" + secrets.token_urlsafe(32)
    new = dict(cfg)
    new["scrapeTokenHash"] = hash_token(token)
    new["scrapeTokenHint"] = token[-4:]
    new["scrapeTokenCreatedAt"] = _iso()
    return new, token


def token_accepted(cfg: dict, presented: str | None, env_token: str) -> bool:
    """The /metrics bearer check. Accepts the console-generated scrape token
    (by hash) or METRICS_TOKEN from the environment. With neither
    configured the endpoint stays open, as before (fine inside a Compose
    network or a ClusterIP service)."""
    presented = (presented or "").strip()
    stored_hash = cfg.get("scrapeTokenHash") or ""
    if not stored_hash and not env_token:
        return True
    if not presented:
        return False
    if env_token and secrets.compare_digest(presented, env_token):
        return True
    if stored_hash and secrets.compare_digest(hash_token(presented), stored_hash):
        return True
    return False


# ---------------------------------------------------------------------------
# Prometheus set-up
# ---------------------------------------------------------------------------
def default_targets(api_port: int, tls: bool) -> dict:
    return {
        "scheme": "https" if tls else "http",
        "path": "/metrics",
        "defaultTargets": [{"address": f"{SERVICE}:{api_port}", "service": SERVICE}],
    }


def scrape_config_snippet(targets: list[str], *, tls: bool, interval: int, token_in_file: bool) -> str:
    lines = [
        "scrape_configs:",
        "  - job_name: aom",
        f"    scrape_interval: {interval}s",
        "    metrics_path: /metrics",
        f"    scheme: {'https' if tls else 'http'}",
    ]
    if tls:
        lines += ["    tls_config:", "      insecure_skip_verify: true   # or ca_file: /etc/prometheus/aom-ca.pem"]
    lines += ["    authorization:", "      type: Bearer"]
    lines += ["      credentials_file: /etc/prometheus/aom-scrape-token"] if token_in_file else ["      credentials: <paste the scrape token>"]
    lines += ["    static_configs:", "      - targets:"]
    lines += [f"          - {t}" for t in targets]
    return "\n".join(lines) + "\n"


def check_prometheus_targets(prometheus_url: str, expected: list[str]) -> dict:
    """Ask Prometheus which of the expected targets it is scraping and
    whether they are up."""
    url = prometheus_url.rstrip("/") + "/api/v1/targets?state=active"
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "detail": f"could not reach Prometheus at {prometheus_url}: {exc.__class__.__name__}: {exc}", "targets": []}
    if resp.status_code >= 400:
        return {"ok": False, "detail": f"Prometheus answered HTTP {resp.status_code} - is this the Prometheus base URL?", "targets": []}
    try:
        active = resp.json().get("data", {}).get("activeTargets") or []
    except ValueError:
        return {"ok": False, "detail": "Prometheus did not return JSON", "targets": []}
    rows = []
    for t in active:
        labels = t.get("labels") or {}
        disc = t.get("discoveredLabels") or {}
        addr = labels.get("instance") or disc.get("__address__") or ""
        path = disc.get("__metrics_path__") or ""
        if path and path != "/metrics" and "aom" not in (labels.get("job") or ""):
            continue
        rows.append({
            "job": labels.get("job"), "instance": addr, "health": t.get("health"), "lastError": t.get("lastError") or "",
            "lastScrape": t.get("lastScrape"), "scrapeUrl": t.get("scrapeUrl"), "expected": any(e in (t.get("scrapeUrl") or "") or e == addr for e in expected),
        })
    matched = [r for r in rows if r["expected"]]
    up = [r for r in matched if r["health"] == "up"]
    if not matched:
        detail = "Prometheus is reachable but has no target for this appliance yet - add the job and reload Prometheus."
    elif len(up) == len(matched):
        detail = f"all {len(up)} AOM target(s) are up"
    else:
        detail = f"{len(up)} of {len(matched)} AOM target(s) up; " + "; ".join(f"{r['instance']}: {r['lastError']}" for r in matched if r["health"] != "up")
    return {"ok": bool(matched) and len(up) == len(matched), "detail": detail, "targets": rows}


# ---------------------------------------------------------------------------
# Grafana provisioning
# ---------------------------------------------------------------------------
_DASHBOARD_CANDIDATES = [
    Path(__file__).resolve().parent / "resources" / "grafana-dashboard.json",
    Path(__file__).resolve().parents[2] / "monitoring" / "grafana" / "dashboards" / "aom-overview.json",
]


def dashboard_json() -> dict:
    for p in _DASHBOARD_CANDIDATES:
        if p.exists():
            try:
                return json.loads(p.read_text("utf-8"))
            except ValueError as exc:
                logger.warning("monitoring: dashboard file %s is not valid JSON: %s", p, exc)
    # Minimal fallback so the wizard still installs something useful.
    return {
        "uid": DASHBOARD_UID, "title": FOLDER_TITLE, "tags": ["aom", "couchbase"], "schemaVersion": 39, "editable": True,
        "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "type": "datasource", "query": "prometheus", "hide": 0}]},
        "panels": [
            {"type": "stat", "title": "Couchbase connected", "gridPos": {"x": 0, "y": 0, "w": 6, "h": 4},
             "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
             "targets": [{"expr": "aom_couchbase_connected", "refId": "A"}]},
            {"type": "timeseries", "title": "Gateway decisions / s", "gridPos": {"x": 6, "y": 0, "w": 18, "h": 8},
             "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
             "targets": [{"expr": "sum by (action, decision) (rate(aom_gateway_decisions_total[5m]))", "refId": "A", "legendFormat": "{{action}} {{decision}}"}]},
            {"type": "timeseries", "title": "HTTP p95 latency", "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
             "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
             "targets": [{"expr": "histogram_quantile(0.95, sum by (le) (rate(aom_http_request_duration_seconds_bucket[5m])))", "refId": "A"}]},
            {"type": "timeseries", "title": "LLM cache events / s", "gridPos": {"x": 12, "y": 8, "w": 12, "h": 8},
             "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
             "targets": [{"expr": "sum by (outcome) (rate(aom_llm_cache_events_total[5m]))", "refId": "A", "legendFormat": "{{outcome}}"}]},
        ],
    }


class GrafanaError(RuntimeError):
    pass


def provision_grafana(*, grafana_url: str, token: str | None, username: str | None, password: str | None,
                      prometheus_url: str, scrape_token_set: bool) -> dict:
    """Create/update the data source, check its health, install the folder
    and dashboard. Returns {dashboardUrl, datasourceUid, folderUid, health}."""
    base = grafana_url.rstrip("/")
    s = requests.Session()
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    elif username:
        s.auth = (username, password or "")
    else:
        raise GrafanaError("a service-account token, or a user name and password, is required")
    s.headers["Content-Type"] = "application/json"

    def call(method, path, **kw):
        try:
            r = s.request(method, base + path, timeout=_HTTP_TIMEOUT, **kw)
        except requests.exceptions.RequestException as exc:
            raise GrafanaError(f"could not reach Grafana at {grafana_url}: {exc.__class__.__name__}: {exc}") from exc
        if r.status_code == 401:
            raise GrafanaError("Grafana rejected the credential (HTTP 401)")
        if r.status_code == 403:
            raise GrafanaError("the Grafana credential lacks permission (HTTP 403) - it needs Editor on dashboards and Admin on data sources")
        return r

    # 1. data source
    ds_body = {
        "name": DATASOURCE_NAME, "uid": DATASOURCE_UID, "type": "prometheus", "access": "proxy",
        "url": prometheus_url, "isDefault": False, "jsonData": {"httpMethod": "POST", "timeInterval": "15s"},
    }
    r = call("GET", f"/api/datasources/uid/{DATASOURCE_UID}")
    if r.status_code == 200:
        r = call("PUT", f"/api/datasources/uid/{DATASOURCE_UID}", json=ds_body)
    else:
        r = call("POST", "/api/datasources", json=ds_body)
    if r.status_code >= 400:
        raise GrafanaError(f"data source create/update failed: HTTP {r.status_code} {r.text[:200]}")

    # 2. health
    health = "unknown"
    r = call("GET", f"/api/datasources/uid/{DATASOURCE_UID}/health")
    if r.status_code == 200:
        try:
            health = r.json().get("status") or "ok"
        except ValueError:
            health = "ok"
    else:
        health = f"HTTP {r.status_code}"
    if health not in ("ok", "OK"):
        raise GrafanaError(f"Grafana cannot reach Prometheus at {prometheus_url} (data source health: {health}). "
                           "Use the URL as Grafana reaches it - inside Compose that is http://prometheus:9090.")

    # 3. folder
    folder_uid = None
    r = call("GET", "/api/folders")
    if r.status_code == 200:
        for f in r.json() if isinstance(r.json(), list) else []:
            if f.get("title") == FOLDER_TITLE:
                folder_uid = f.get("uid")
    if not folder_uid:
        r = call("POST", "/api/folders", json={"title": FOLDER_TITLE})
        if r.status_code >= 400:
            raise GrafanaError(f"folder create failed: HTTP {r.status_code} {r.text[:200]}")
        folder_uid = r.json().get("uid")

    # 4. dashboard
    dash = dashboard_json()
    dash = dict(dash, id=None, uid=DASHBOARD_UID)
    body = {"dashboard": dash, "folderUid": folder_uid, "overwrite": True, "message": "installed by Couchbase Agent Operations Manager"}
    r = call("POST", "/api/dashboards/db", json=body)
    if r.status_code >= 400:
        raise GrafanaError(f"dashboard install failed: HTTP {r.status_code} {r.text[:300]}")
    url = r.json().get("url") or f"/d/{DASHBOARD_UID}"
    return {"dashboardUrl": base + url if url.startswith("/") else url, "datasourceUid": DATASOURCE_UID,
            "folderUid": folder_uid, "health": health, "scrapeTokenSet": scrape_token_set}


# ---------------------------------------------------------------------------
# Push exporters
# ---------------------------------------------------------------------------
def collect_samples() -> list[dict]:
    """Every sample in the prometheus_client registry as {name, labels,
    value, kind}. Histogram buckets are skipped (vendors want _count/_sum)."""
    try:
        from prometheus_client import REGISTRY
    except Exception:  # noqa: BLE001
        return []
    out = []
    for metric in REGISTRY.collect():
        kind = metric.type
        for s in metric.samples:
            if s.name.endswith("_bucket") or s.name.endswith("_created"):
                continue
            if kind == "histogram" or kind == "summary":
                skind = "counter" if s.name.endswith(("_count", "_sum")) else "gauge"
            elif kind == "counter":
                skind = "counter"
            else:
                skind = "gauge"
            labels = {k: v for k, v in s.labels.items() if k != "quantile"}
            out.append({"name": s.name, "labels": labels, "value": float(s.value), "kind": skind})
    return out


class _ExporterState:
    __slots__ = ("pushes", "failures", "last_ok_at", "last_error")

    def __init__(self):
        self.pushes = 0
        self.failures = 0
        self.last_ok_at: str | None = None
        self.last_error: str | None = None


class MetricsPusher:
    def __init__(self):
        self.cfg: dict = dict(DEFAULT_CONFIG)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._last: dict[tuple, float] = {}
        self.states: dict[str, _ExporterState] = {}
        self._active = False

    def apply(self, cfg: dict) -> None:
        with self._lock:
            self.cfg = cfg
            enabled = [vid for vid, d in cfg.get("exporters", {}).items() if d.get("enabled") and not missing_required(vid, d)]
            for vid in enabled:
                self.states.setdefault(vid, _ExporterState())
            for vid in list(self.states):
                if vid not in enabled:
                    self.states.pop(vid, None)
            self._active = bool(enabled)
        if self._active and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, name="aom-metrics-pusher", daemon=True)
            self._thread.start()
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait(timeout=self.cfg.get("pushIntervalSeconds", 60))
            self._wake.clear()
            if not self._active:
                return
            self.push_once()

    def _deltas(self, samples: list[dict]) -> list[dict]:
        out = []
        for s in samples:
            if s["kind"] == "counter":
                key = (s["name"], tuple(sorted(s["labels"].items())))
                prev = self._last.get(key)
                self._last[key] = s["value"]
                if prev is None:
                    continue
                value = s["value"] - prev
                if value < 0:  # process restart
                    value = s["value"]
                out.append(dict(s, value=value))
            else:
                out.append(s)
        return out

    def push_once(self) -> None:
        samples = self._deltas(collect_samples())
        with self._lock:
            targets = [(vid, self.cfg["exporters"][vid]) for vid in self.states]
        for vid, d in targets:
            st = self.states.get(vid)
            if st is None:
                continue
            try:
                push(vid, d, samples)
                st.pushes += 1
                st.last_ok_at = _iso()
                st.last_error = None
            except Exception as exc:  # noqa: BLE001
                st.failures += 1
                st.last_error = f"{exc}"[:300]

    def status(self) -> dict:
        return {
            f"{SERVICE}@{NODE}": {
                "service": SERVICE, "node": NODE, "at": _iso(),
                "status": {vid: {"pushes": st.pushes, "failures": st.failures, "lastOkAt": st.last_ok_at, "lastError": st.last_error}
                           for vid, st in self.states.items()},
            }
        }


pusher = MetricsPusher()


def _dotted(name: str) -> str:
    return "aom." + name[4:] if name.startswith("aom_") else name.replace("_", ".", 1)


def _post(url: str, headers: dict, body: bytes, *, gz: bool = True, verify: bool = True) -> requests.Response:
    h = dict(headers)
    if gz:
        body = gzip.compress(body, compresslevel=1)
        h["Content-Encoding"] = "gzip"
    r = requests.post(url, data=body, headers=h, timeout=_HTTP_TIMEOUT, verify=verify)
    if r.status_code >= 400:
        snippet = (r.text or "")[:200].replace("\n", " ")
        hint = " - the API key/token was rejected" if r.status_code in (401, 403) else ""
        raise RuntimeError(f"HTTP {r.status_code}{': ' + snippet if snippet else ''}{hint}")
    return r


def push(vid: str, d: dict, samples: list[dict]) -> None:
    now = int(time.time())
    base_tags = {"service": SERVICE, "host": NODE}
    if vid == "datadog":
        extra = [t.strip() for t in _get(vid, d, "tags").split(",") if t.strip()]
        series = []
        for s in samples:
            tags = [f"{k}:{v}" for k, v in {**base_tags, **s["labels"]}.items()] + extra
            series.append({"metric": _dotted(s["name"]), "type": 1 if s["kind"] == "counter" else 3,
                           "points": [{"timestamp": now, "value": s["value"]}], "tags": tags})
        if not series:
            return
        _post(f"https://api.{_get(vid, d, 'site') or 'datadoghq.com'}/api/v2/series",
              {"DD-API-KEY": _get(vid, d, "apiKey"), "Content-Type": "application/json"},
              json.dumps({"series": series}, separators=(",", ":")).encode())
    elif vid == "dynatrace":
        lines = []
        for s in samples:
            dims = ",".join(f"{k}={json.dumps(str(v))}" for k, v in {**base_tags, **s["labels"]}.items())
            kind = "count,delta=" if s["kind"] == "counter" else "gauge,"
            lines.append(f"{_dotted(s['name'])},{dims} {kind}{s['value']} {now * 1000}")
        if not lines:
            return
        _post(_get(vid, d, "url").rstrip("/") + "/api/v2/metrics/ingest",
              {"Authorization": f"Api-Token {_get(vid, d, 'apiToken')}", "Content-Type": "text/plain; charset=utf-8"},
              "\n".join(lines).encode(), gz=False, verify=str((d.get("fields") or {}).get("verifyTls", "true")).lower() == "true")
    elif vid == "newrelic":
        metrics_ = []
        for s in samples:
            m = {"name": _dotted(s["name"]), "type": "count" if s["kind"] == "counter" else "gauge",
                 "value": s["value"], "timestamp": now, "attributes": {**base_tags, **s["labels"]}}
            if s["kind"] == "counter":
                m["interval.ms"] = int(pusher.cfg.get("pushIntervalSeconds", 60)) * 1000
            metrics_.append(m)
        if not metrics_:
            return
        host = "metric-api.eu.newrelic.com" if _get(vid, d, "region") == "EU" else "metric-api.newrelic.com"
        _post(f"https://{host}/metric/v1", {"Api-Key": _get(vid, d, "licenseKey"), "Content-Type": "application/json"},
              json.dumps([{"metrics": metrics_}], separators=(",", ":")).encode())
    else:
        raise RuntimeError(f"unknown exporter '{vid}'")


def test_exporter(vid: str, d: dict) -> dict:
    if vid not in EXPORTER_BY_ID:
        return {"success": False, "detail": f"unknown exporter '{vid}'"}
    missing = missing_required(vid, d)
    if missing:
        return {"success": False, "detail": "missing: " + ", ".join(missing)}
    started = time.time()
    try:
        samples = [{"name": "aom_monitoring_test", "labels": {}, "value": 1.0, "kind": "gauge"}]
        samples += [s for s in collect_samples() if s["kind"] == "gauge"][:50]
        push(vid, d, samples)
    except requests.exceptions.RequestException as exc:
        return {"success": False, "detail": f"could not reach {EXPORTER_BY_ID[vid]['label']}: {exc.__class__.__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "detail": str(exc)}
    return {"success": True, "detail": f"test push to {EXPORTER_BY_ID[vid]['label']} accepted in {int((time.time() - started) * 1000)} ms"}
