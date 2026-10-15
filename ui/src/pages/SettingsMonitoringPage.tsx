import { useCallback, useState } from "react";
import { api } from "../api/client";
import type { DestinationConfig, ExporterStatus, MonitoringConfig, MonitoringView, PrometheusTargetRow, ServiceStatus } from "../api/types";
import { DestinationCard, ago, fmt, type DeliveryPill } from "../components/observability/DestinationCard";
import { usePoll } from "../hooks/usePoll";

type Wizard = "prometheus" | "grafana" | null;

function exporterPill(id: string, services: Record<string, ServiceStatus<Record<string, ExporterStatus>>>, enabled: boolean): DeliveryPill | null {
  if (!enabled) return null;
  let pushes = 0;
  let lastOk: string | null = null;
  let error: string | null = null;
  let seen = false;
  for (const s of Object.values(services)) {
    const d = s.status?.[id];
    if (!d) continue;
    seen = true;
    pushes += d.pushes;
    if (d.lastOkAt && (!lastOk || d.lastOkAt > lastOk)) lastOk = d.lastOkAt;
    if (d.lastError) error = `${s.service}: ${d.lastError}`;
  }
  if (error) return { tone: "danger", text: "failing", title: error };
  if (!seen) return { tone: "neutral", text: "not saved yet" };
  if (pushes > 0) return { tone: "success", text: `pushing · ${ago(lastOk)}`, title: `${fmt(pushes)} pushes` };
  return { tone: "neutral", text: "first push pending" };
}

export function SettingsMonitoringPage() {
  const [view, setView] = useState<MonitoringView | null>(null);
  const [cfg, setCfg] = useState<MonitoringConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [wizard, setWizard] = useState<Wizard>(null);

  const load = useCallback(async (resetForm: boolean) => {
    try {
      const v = await api.getMonitoring();
      setView(v);
      setCfg((c) => (resetForm || c === null ? v.config : c));
      setError(null);
    } catch (e: any) {
      setError(e.message || "operations-manager is unreachable");
    }
  }, []);

  usePoll(() => load(false), 15000);

  const patch = (p: Partial<MonitoringConfig>) => {
    setCfg((c) => (c ? { ...c, ...p } : c));
    setDirty(true);
    setSaved(null);
  };
  const patchExporter = (id: string, d: DestinationConfig) => {
    setCfg((c) => (c ? { ...c, exporters: { ...c.exporters, [id]: d } } : c));
    setDirty(true);
    setSaved(null);
  };

  async function save(ev?: React.FormEvent) {
    ev?.preventDefault();
    if (!cfg) return;
    setSaving(true);
    setError(null);
    try {
      const v = await api.saveMonitoring(cfg);
      setView(v);
      setCfg(v.config);
      setDirty(false);
      setSaved("Saved. The operations manager applies these settings immediately.");
    } catch (e: any) {
      setError(e.message || "Save failed.");
    } finally {
      setSaving(false);
    }
  }

  /** After a wizard changed something server-side: reload, keeping unsaved
   *  form edits out of the way. */
  const afterWizard = (v?: MonitoringView) => {
    if (v) {
      setView(v);
      if (!dirty) setCfg(v.config);
    } else {
      load(!dirty);
    }
  };

  const c = view?.config;
  const promReady = !!c && c.prometheusEnabled && !!(view?.endpoint.scrapeTokenSet || view?.endpoint.staticTokenSet);
  const grafanaReady = !!c?.grafanaAppliedAt;

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Monitoring</h1>
          <p className="page-subtitle">
            Metrics from the operations manager for Prometheus and Grafana, Datadog, Dynatrace or New Relic. Metrics
            are computed only when scraped or pushed, so monitoring adds no work to the gateway&apos;s request paths.
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => save()} disabled={saving || !dirty || !cfg}>
          {saving ? "Saving..." : "Save settings"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}
      {view && !view.endpoint.exporterAvailable && (
        <div className="helper-banner helper-banner-neutral">
          The Prometheus exporter is disabled in this build (METRICS_ENABLED=false or prometheus_client missing), so
          /metrics answers 404 and there is nothing to push.
        </div>
      )}
      {view && !view.durable && (
        <div className="helper-banner helper-banner-neutral">
          Couchbase is not connected: these settings apply to this process only and are lost on restart until it is.
        </div>
      )}
      {saved && <div className="helper-banner helper-banner-neutral">{saved}</div>}

      {!cfg && !error && <div className="loading-note">Loading...</div>}

      {cfg && view && (
        <form onSubmit={save}>
          <div className="three-col-fields" style={{ marginBottom: 18 }}>
            <ProductTile
              name="Prometheus"
              tag="pull"
              text="Scrapes /metrics on the operations manager."
              state={promReady ? "ready" : "not set up"}
              detail={c?.prometheusUrl || undefined}
              ok={promReady}
              onSetUp={() => setWizard(wizard === "prometheus" ? null : "prometheus")}
            />
            <ProductTile
              name="Grafana"
              tag="dashboards"
              text="Data source and the AOM overview dashboard, installed for you."
              state={grafanaReady ? "installed" : "not set up"}
              detail={grafanaReady ? `${c?.grafanaUrl} · ${ago(c?.grafanaAppliedAt)}` : undefined}
              ok={grafanaReady}
              link={c?.grafanaDashboardUrl || undefined}
              onSetUp={() => setWizard(wizard === "grafana" ? null : "grafana")}
            />
            <div className="card">
              <div style={{ fontWeight: 600, marginBottom: 4 }}>Push exporters</div>
              <div className="cell-muted" style={{ fontSize: 12.5 }}>
                {view.catalog.map((v) => {
                  const on = !!cfg.exporters[v.id]?.enabled;
                  return (
                    <div key={v.id} className="flex-row" style={{ gap: 6, marginTop: 4 }}>
                      <span className={`badge ${on ? "badge-success" : "badge-neutral"}`}>{on ? "pushing" : "off"}</span>
                      {v.label}
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {wizard === "prometheus" && <PrometheusWizard view={view} onChanged={afterWizard} onClose={() => setWizard(null)} />}
          {wizard === "grafana" && <GrafanaWizard view={view} onChanged={afterWizard} onClose={() => setWizard(null)} />}

          <div className="card" style={{ marginBottom: 18 }}>
            <h3 className="card-title">Prometheus endpoint</h3>
            <div className="checkbox-row">
              <input
                type="checkbox"
                id="mon-prom"
                checked={cfg.prometheusEnabled}
                onChange={(e) => patch({ prometheusEnabled: e.target.checked })}
              />
              <label htmlFor="mon-prom" style={{ margin: 0, fontWeight: 500, color: "var(--text)" }}>
                Serve /metrics
              </label>
            </div>
            <div className="field-hint" style={{ marginBottom: 12 }}>
              Prometheus text format. Off: the endpoint answers 404. The counters are in-process - a scrape never
              touches Couchbase.
            </div>
            <div className="two-col">
              <div className="field">
                <label>Endpoint</label>
                <div className="cell-mono" style={{ fontSize: 12.5 }}>
                  {view.endpoint.defaultTargets.map((t) => (
                    <div key={t.address}>
                      {view.endpoint.scheme}://{t.address}
                      {view.endpoint.path}
                    </div>
                  ))}
                </div>
                <div className="field-hint">Inside Docker Compose / the cluster. From outside, use the published host and port.</div>
              </div>
              <div className="field">
                <label>Scrape token</label>
                <div>
                  {view.endpoint.scrapeTokenSet ? (
                    <>
                      <span className="cell-mono">…{c?.scrapeTokenHint}</span>{" "}
                      <span className="cell-muted">generated {ago(c?.scrapeTokenCreatedAt)}</span>
                    </>
                  ) : (
                    <span className="cell-muted">none yet: run the Prometheus set-up</span>
                  )}
                </div>
                <div className="field-hint">
                  METRICS_TOKEN (environment):{" "}
                  {view.endpoint.staticTokenSet ? <span className="badge badge-success">set</span> : <span className="cell-muted">not set</span>}
                  {!view.endpoint.scrapeTokenSet && !view.endpoint.staticTokenSet && " - /metrics is currently open to anyone who can reach the port."}
                </div>
              </div>
              <div className="field">
                <label>Scrape interval (seconds)</label>
                <input
                  type="number"
                  min={5}
                  max={300}
                  value={cfg.scrapeIntervalSeconds}
                  onChange={(e) => patch({ scrapeIntervalSeconds: Math.max(5, Number(e.target.value) || 15) })}
                />
                <div className="field-hint">Used in the generated scrape_configs job only.</div>
              </div>
            </div>
          </div>

          <div className="card" style={{ marginBottom: 18 }}>
            <h3 className="card-title">Push exporters</h3>
            <p className="cell-muted" style={{ fontSize: 12.5, marginTop: -6, marginBottom: 12 }}>
              One background thread collects once per interval and sends one gzip-compressed request to each vendor
              that is on. Counters go as deltas. Nothing runs while every exporter is off.
            </p>
            <div className="field" style={{ maxWidth: 320 }}>
              <label>Push interval (seconds)</label>
              <input
                type="number"
                min={10}
                max={3600}
                value={cfg.pushIntervalSeconds}
                onChange={(e) => patch({ pushIntervalSeconds: Math.max(10, Number(e.target.value) || 60) })}
              />
            </div>
            {view.catalog.map((v) => {
              const d = cfg.exporters[v.id] ?? { type: `metrics:${v.id}`, enabled: false, fields: {} };
              return (
                <DestinationCard
                  key={v.id}
                  vendor={v}
                  value={d}
                  onChange={(nd) => patchExporter(v.id, nd)}
                  onTest={(nd) => api.testExporter(v.id, nd)}
                  pill={exporterPill(v.id, view.services, d.enabled)}
                  testLabel="Test push"
                />
              );
            })}
          </div>

          <div className="flex-row" style={{ gap: 10, alignItems: "center", marginBottom: 18 }}>
            <button type="submit" className="btn btn-primary" disabled={saving || !dirty}>
              {saving ? "Saving..." : "Save settings"}
            </button>
            {dirty && <span className="cell-muted">Unsaved changes.</span>}
          </div>

          <div className="card">
            <div className="flex-between">
              <h3 className="card-title">Push status by service</h3>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => load(false)}>
                Refresh
              </button>
            </div>
            <ExporterTable services={view.services} />
          </div>
        </form>
      )}
    </div>
  );
}

function ProductTile({
  name,
  tag,
  text,
  state,
  detail,
  ok,
  link,
  onSetUp,
}: {
  name: string;
  tag: string;
  text: string;
  state: string;
  detail?: string;
  ok: boolean;
  link?: string;
  onSetUp: () => void;
}) {
  return (
    <div className="card">
      <div className="flex-between">
        <span style={{ fontWeight: 600 }}>{name}</span>
        <span className="badge badge-neutral">{tag}</span>
      </div>
      <div className="cell-muted" style={{ fontSize: 12.5, margin: "4px 0" }}>
        {text}
      </div>
      {detail && (
        <div className="cell-mono cell-muted" style={{ fontSize: 11.5, wordBreak: "break-all" }} title={detail}>
          {detail}
        </div>
      )}
      <div className="flex-between" style={{ marginTop: 8 }}>
        <span className={`badge ${ok ? "badge-success" : "badge-neutral"}`}>{state}</span>
        <span className="flex-row" style={{ gap: 6 }}>
          {link && (
            <a className="btn btn-secondary btn-sm" href={link} target="_blank" rel="noreferrer" title="Open the dashboard">
              Open ↗
            </a>
          )}
          <button type="button" className="btn btn-secondary btn-sm" onClick={onSetUp}>
            Set up
          </button>
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- wizards

function PrometheusWizard({ view, onChanged, onClose }: { view: MonitoringView; onChanged: (v?: MonitoringView) => void; onClose: () => void }) {
  const [targets, setTargets] = useState(view.endpoint.defaultTargets.map((t) => t.address).join("\n"));
  const [token, setToken] = useState<string | null>(null);
  const [tokenInFile, setTokenInFile] = useState(true);
  const [snippet, setSnippet] = useState<string | null>(null);
  const [promUrl, setPromUrl] = useState(view.config.prometheusUrl || "http://prometheus:9090");
  const [check, setCheck] = useState<{ ok: boolean; detail: string; targets: PrometheusTargetRow[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const targetList = targets
    .split(/[\n,]+/)
    .map((t) => t.trim())
    .filter(Boolean);

  async function generate() {
    if (view.endpoint.scrapeTokenSet && !confirm("Generate a new scrape token? The previous one stops working immediately.")) return;
    setBusy("token");
    setErr(null);
    try {
      const res = await api.newScrapeToken();
      setToken(res.token);
      onChanged(res.view);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  async function makeSnippet() {
    setBusy("snippet");
    setErr(null);
    try {
      const res = await api.prometheusSnippet(targetList, tokenInFile);
      setSnippet(res.snippet);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  async function checkTargets() {
    setBusy("check");
    setErr(null);
    setCheck(null);
    try {
      setCheck(await api.prometheusCheck(promUrl, targetList));
      onChanged();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  function download(name: string, text: string) {
    const blob = new Blob([text], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  return (
    <div className="panel section-gap" style={{ marginBottom: 18 }}>
      <div className="flex-between">
        <h3 className="card-title">Prometheus set-up</h3>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
          Close
        </button>
      </div>
      {err && <div className="error-note">{err}</div>}

      <div className="field">
        <label>1. Targets (host:port, one per line)</label>
        <textarea rows={2} value={targets} onChange={(e) => setTargets(e.target.value)} />
        <div className="field-hint">As Prometheus reaches the operations manager - the Compose service name, a cluster Service, or a published host.</div>
      </div>

      <div className="field">
        <label>2. Scrape token</label>
        <div className="flex-row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy === "token"} onClick={generate}>
            {busy === "token" ? "Generating..." : view.endpoint.scrapeTokenSet ? "Generate a new token" : "Generate token"}
          </button>
          {view.endpoint.scrapeTokenSet && !token && (
            <span className="cell-muted">
              one is set (…{view.config.scrapeTokenHint}, {ago(view.config.scrapeTokenCreatedAt)}) - generate again only to rotate it
            </span>
          )}
        </div>
        {token && (
          <div className="helper-banner" style={{ marginTop: 10 }}>
            <div className="helper-banner-heading">Copy this now - it is shown once</div>
            <div className="cell-mono" style={{ wordBreak: "break-all", userSelect: "all" }}>
              {token}
            </div>
            <div className="flex-row" style={{ gap: 8, marginTop: 8 }}>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigator.clipboard?.writeText(token)}>
                Copy
              </button>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => download("aom-scrape-token", token)}>
                Download aom-scrape-token
              </button>
            </div>
            <div className="field-hint">Only its SHA-256 hash is stored. It grants nothing but /metrics.</div>
          </div>
        )}
      </div>

      <div className="field">
        <label>3. scrape_configs job</label>
        <div className="flex-row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <label className="checkbox-row" style={{ margin: 0, fontWeight: 400 }}>
            <input type="checkbox" checked={tokenInFile} onChange={(e) => setTokenInFile(e.target.checked)} /> token in a file (credentials_file)
          </label>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy === "snippet" || targetList.length === 0} onClick={makeSnippet}>
            {busy === "snippet" ? "..." : "Generate job"}
          </button>
          {snippet && (
            <>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigator.clipboard?.writeText(snippet)}>
                Copy
              </button>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => download("aom-scrape-job.yml", snippet)}>
                Download
              </button>
            </>
          )}
        </div>
        {snippet && (
          <pre className="json-block" style={{ marginTop: 10 }}>
            {snippet}
          </pre>
        )}
        <div className="field-hint">Paste it into prometheus.yml (or the bundled monitoring/prometheus/prometheus.yml) and reload Prometheus.</div>
      </div>

      <div className="field">
        <label>4. Check targets</label>
        <div className="flex-row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <input type="url" style={{ maxWidth: 360 }} value={promUrl} placeholder="http://prometheus:9090" onChange={(e) => setPromUrl(e.target.value)} />
          <button type="button" className="btn btn-primary btn-sm" disabled={busy === "check" || !promUrl} onClick={checkTargets}>
            {busy === "check" ? "Checking..." : "Check targets"}
          </button>
        </div>
        <div className="field-hint">Asks Prometheus&apos;s own API (/api/v1/targets) whether every target above is being scraped and is up.</div>
        {check && (
          <div className={check.ok ? "helper-banner helper-banner-neutral" : "error-note"} style={{ marginTop: 10 }}>
            <div>{check.detail}</div>
            {check.targets.length > 0 && (
              <div className="table-wrap" style={{ marginTop: 8 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Job</th>
                      <th>Instance</th>
                      <th>Health</th>
                      <th>Last scrape</th>
                      <th>Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {check.targets.map((t, i) => (
                      <tr key={i}>
                        <td className="cell-mono">{t.job}</td>
                        <td className="cell-mono">{t.instance}</td>
                        <td>
                          <span className={`badge ${t.health === "up" ? "badge-success" : "badge-danger"}`}>{t.health}</span>
                        </td>
                        <td className="cell-muted">{ago(t.lastScrape)}</td>
                        <td className="cell-muted" style={{ fontSize: 12 }}>
                          {t.lastError}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function GrafanaWizard({ view, onChanged, onClose }: { view: MonitoringView; onChanged: (v?: MonitoringView) => void; onClose: () => void }) {
  const [grafanaUrl, setGrafanaUrl] = useState(view.config.grafanaUrl || "http://grafana:3000");
  const [promUrl, setPromUrl] = useState(view.config.grafanaPrometheusUrl || view.config.prometheusUrl || "http://prometheus:9090");
  const [mode, setMode] = useState<"token" | "basic">("token");
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<{ dashboardUrl: string; health: string } | null>(null);

  async function run() {
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      const res = await api.grafanaSetup({
        grafana_url: grafanaUrl,
        prometheus_url: promUrl,
        token: mode === "token" ? token : undefined,
        username: mode === "basic" ? username : undefined,
        password: mode === "basic" ? password : undefined,
      });
      setResult(res);
      onChanged(res.view);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel section-gap" style={{ marginBottom: 18 }}>
      <div className="flex-between">
        <h3 className="card-title">Grafana set-up</h3>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="cell-muted" style={{ fontSize: 12.5, marginTop: -4 }}>
        Creates or updates the <span className="cell-mono">AOM Prometheus</span> data source (uid aom-prometheus), checks its
        health, and installs the <em>Couchbase Agent Operations Manager</em> folder and overview dashboard through
        Grafana&apos;s HTTP API. Repeatable. The credential is used for this one call and never stored.
      </p>
      {err && <div className="error-note">{err}</div>}
      <div className="two-col">
        <div className="field">
          <label>Grafana URL</label>
          <input type="url" value={grafanaUrl} onChange={(e) => setGrafanaUrl(e.target.value)} />
        </div>
        <div className="field">
          <label>Prometheus URL as Grafana reaches it</label>
          <input type="url" value={promUrl} onChange={(e) => setPromUrl(e.target.value)} />
          <div className="field-hint">Inside Compose that is http://prometheus:9090, not localhost.</div>
        </div>
        <div className="field">
          <label>Credential</label>
          <select value={mode} onChange={(e) => setMode(e.target.value as "token" | "basic")}>
            <option value="token">service-account token (recommended)</option>
            <option value="basic">user name and password</option>
          </select>
        </div>
        {mode === "token" ? (
          <div className="field">
            <label>Service-account token</label>
            <input type="password" autoComplete="new-password" value={token} onChange={(e) => setToken(e.target.value)} />
            <div className="field-hint">Administration → Service accounts → Add token. Needs Editor (dashboards) and Admin (data sources).</div>
          </div>
        ) : (
          <>
            <div className="field">
              <label>User name</label>
              <input type="text" value={username} onChange={(e) => setUsername(e.target.value)} />
            </div>
            <div className="field">
              <label>Password</label>
              <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
          </>
        )}
      </div>
      <div className="flex-row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy || !grafanaUrl || !promUrl || (mode === "token" ? !token : !username)}
          onClick={run}
        >
          {busy ? "Installing..." : "Install data source + dashboard"}
        </button>
        <a className="btn btn-secondary btn-sm" href="/v1/monitoring/grafana/dashboard.json" target="_blank" rel="noreferrer">
          Dashboard JSON (manual import)
        </a>
      </div>
      {result && (
        <div className="helper-banner helper-banner-neutral" style={{ marginTop: 10 }}>
          Installed. Data source health: {result.health}.{" "}
          <a href={result.dashboardUrl} target="_blank" rel="noreferrer">
            Open the dashboard ↗
          </a>
        </div>
      )}
    </div>
  );
}

function ExporterTable({ services }: { services: Record<string, ServiceStatus<Record<string, ExporterStatus>>> }) {
  const rows = Object.entries(services).sort(([a], [b]) => a.localeCompare(b));
  if (rows.length === 0) return <div className="empty-state">No service has reported yet.</div>;
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Service</th>
            <th>Reported</th>
            <th>Exporters</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([key, s]) => (
            <tr key={key}>
              <td>
                <div className="cell-mono">{s.service}</div>
                <div className="cell-muted">{s.node}</div>
              </td>
              <td className="cell-muted">{ago(s.at)}</td>
              <td>
                {s.status && Object.keys(s.status).length > 0 ? (
                  <div className="flex-row" style={{ flexWrap: "wrap", gap: 6 }}>
                    {Object.entries(s.status).map(([id, d]) => (
                      <span
                        key={id}
                        className={`badge ${d.lastError ? "badge-danger" : d.pushes > 0 ? "badge-success" : "badge-neutral"}`}
                        title={d.lastError ?? `last push ${ago(d.lastOkAt)}`}
                      >
                        {id}: {fmt(d.pushes)} pushes{d.failures ? ` · ${fmt(d.failures)} failed` : ""}
                      </span>
                    ))}
                  </div>
                ) : (
                  <span className="cell-muted">none on</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
