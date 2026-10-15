import { useCallback, useMemo, useState } from "react";
import { api } from "../api/client";
import type { DestinationConfig, ForwarderStatus, LogForwardingConfig, LogForwardingView, ServiceStatus } from "../api/types";
import { DestinationCard, ago, fmt, type DeliveryPill } from "../components/observability/DestinationCard";
import { usePoll } from "../hooks/usePoll";

/** Worst state of one destination across every service that reports it
 *  (one service today - the operations manager - but the shape matches the
 *  CRDS console so a second replica slots in). */
function pillFor(type: string, services: Record<string, ServiceStatus<ForwarderStatus>>, enabled: boolean): DeliveryPill | null {
  if (!enabled) return null;
  let sent = 0;
  let lastOk: string | null = null;
  let error: string | null = null;
  let seen = false;
  for (const s of Object.values(services)) {
    const d = s.status?.destinations?.[type];
    if (!d) continue;
    seen = true;
    sent += d.sent;
    if (d.lastOkAt && (!lastOk || d.lastOkAt > lastOk)) lastOk = d.lastOkAt;
    if (d.lastError) error = `${s.service}: ${d.lastError}`;
  }
  if (error) return { tone: "danger", text: "failing", title: error };
  if (!seen) return { tone: "neutral", text: "not saved yet" };
  if (sent > 0) return { tone: "success", text: `delivering · ${ago(lastOk)}`, title: `${fmt(sent)} records sent` };
  return { tone: "neutral", text: "waiting for logs" };
}

export function SettingsLogForwardingPage() {
  const [view, setView] = useState<LogForwardingView | null>(null);
  const [cfg, setCfg] = useState<LogForwardingConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async (resetForm: boolean) => {
    try {
      const v = await api.getLogForwarding();
      setView(v);
      setCfg((c) => (resetForm || c === null ? v.config : c));
      setError(null);
    } catch (e: any) {
      setError(e.message || "operations-manager is unreachable");
    }
  }, []);

  // Delivery status refreshes on its own; the form is left alone once it
  // has been filled in.
  usePoll(() => load(false), 15000);

  const patch = (p: Partial<LogForwardingConfig>) => {
    setCfg((c) => (c ? { ...c, ...p } : c));
    setDirty(true);
    setSaved(null);
  };
  const patchDestination = (id: string, d: DestinationConfig) => {
    setCfg((c) => (c ? { ...c, destinations: { ...c.destinations, [id]: d } } : c));
    setDirty(true);
    setSaved(null);
  };

  async function save(ev: React.FormEvent) {
    ev.preventDefault();
    if (!cfg) return;
    setSaving(true);
    setError(null);
    try {
      const v = await api.saveLogForwarding(cfg);
      setView(v);
      setCfg(v.config);
      setDirty(false);
      const on = Object.values(v.config.destinations).filter((d) => d.enabled).length;
      setSaved(
        v.config.enabled && on > 0
          ? `Saved. The operations manager forwards ${v.config.minLevel} and above to ${on} destination${on === 1 ? "" : "s"}.`
          : "Saved. Log forwarding is off."
      );
    } catch (e: any) {
      setError(e.message || "Save failed.");
    } finally {
      setSaving(false);
    }
  }

  const services = useMemo(() => view?.services ?? {}, [view]);
  const enabledCount = cfg ? Object.values(cfg.destinations).filter((d) => d.enabled).length : 0;

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Log Forwarding</h1>
          <p className="page-subtitle">
            Ship the operations manager's own application log to your log platform. Records are batched and
            compressed on a background thread, so forwarding never slows down discover, invoke or the caches. The
            Audit Log has its own SIEM forwarding - this is everything else the service logs.
          </p>
        </div>
        <button className="btn btn-primary" onClick={save} disabled={saving || !dirty || !cfg}>
          {saving ? "Saving..." : "Save settings"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}
      {view && !view.durable && (
        <div className="helper-banner helper-banner-neutral">
          Couchbase is not connected: these settings apply to this process only and are lost on restart until it is.
        </div>
      )}
      {saved && <div className="helper-banner helper-banner-neutral">{saved}</div>}

      {!cfg && !error && <div className="loading-note">Loading...</div>}

      {cfg && view && (
        <form onSubmit={save}>
          <div className="card" style={{ marginBottom: 18 }}>
            <h3 className="card-title">Forwarding</h3>
            <div className="checkbox-row" style={{ marginBottom: 6 }}>
              <input type="checkbox" id="lf-enabled" checked={cfg.enabled} onChange={(e) => patch({ enabled: e.target.checked })} />
              <label htmlFor="lf-enabled" style={{ margin: 0, fontWeight: 500, color: "var(--text)" }}>
                Forward logs
              </label>
            </div>
            <div className="field-hint" style={{ marginBottom: 12 }}>
              Master switch. Off: nothing is queued or sent, and the handler filters every record with one comparison.
            </div>
            <div className="two-col">
              <div className="field">
                <label>Minimum level</label>
                <select value={cfg.minLevel} onChange={(e) => patch({ minLevel: e.target.value as LogForwardingConfig["minLevel"] })}>
                  {view.levels.map((l) => (
                    <option key={l} value={l}>
                      {l}
                    </option>
                  ))}
                </select>
                <div className="field-hint">
                  Records below this level are dropped inside the handler before any work is done. INFO is the usual
                  choice; DEBUG is noisy.
                </div>
              </div>
              <div className="field">
                <div className="checkbox-row" style={{ marginTop: 22 }}>
                  <input
                    type="checkbox"
                    id="lf-stack"
                    checked={cfg.includeStackTraces}
                    onChange={(e) => patch({ includeStackTraces: e.target.checked })}
                  />
                  <label htmlFor="lf-stack" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                    Include stack traces
                  </label>
                </div>
                <div className="field-hint">Off: only the exception class and message are sent.</div>
              </div>
            </div>
          </div>

          <div className="card" style={{ marginBottom: 18 }}>
            <div className="flex-between">
              <h3 className="card-title">Destinations</h3>
              <span className="cell-muted">{enabledCount} on</span>
            </div>
            <p className="cell-muted" style={{ fontSize: 12.5, marginTop: -6, marginBottom: 12 }}>
              Turn on one or more. Each gets the same records; a destination that fails backs off on its own without
              affecting the others. &ldquo;Send test&rdquo; uses the form as it is now, saved or not.
            </p>
            {view.catalog.map((v) => {
              const d = cfg.destinations[v.id] ?? { type: v.id, enabled: false, fields: {} };
              return (
                <DestinationCard
                  key={v.id}
                  vendor={v}
                  value={d}
                  onChange={(nd) => patchDestination(v.id, nd)}
                  onTest={(nd) => api.testLogDestination(v.id, nd)}
                  pill={pillFor(v.id, services, d.enabled && cfg.enabled)}
                />
              );
            })}
          </div>

          <div className="card" style={{ marginBottom: 18 }}>
            <h3 className="card-title">Load on the operations manager</h3>
            <p className="cell-muted" style={{ fontSize: 12.5, marginTop: -6, marginBottom: 12 }}>
              Logging threads only copy the record into a bounded queue; one background thread sends batches. If a
              destination can&apos;t keep up, the queue fills and new records are dropped and counted rather than
              slowing the gateway down.
            </p>
            <div className="two-col">
              <div className="field">
                <label>Batch size (records)</label>
                <input type="number" min={1} max={5000} value={cfg.batchSize} onChange={(e) => patch({ batchSize: Number(e.target.value) || 1 })} />
                <div className="field-hint">Sent when this many are waiting... (1-5000)</div>
              </div>
              <div className="field">
                <label>Flush interval (ms)</label>
                <input
                  type="number"
                  min={200}
                  max={60000}
                  value={cfg.flushIntervalMs}
                  onChange={(e) => patch({ flushIntervalMs: Number(e.target.value) || 200 })}
                />
                <div className="field-hint">...or when the oldest has waited this long (200-60000).</div>
              </div>
              <div className="field">
                <label>Queue capacity (records)</label>
                <input
                  type="number"
                  min={100}
                  max={1000000}
                  value={cfg.queueCapacity}
                  onChange={(e) => patch({ queueCapacity: Number(e.target.value) || 100 })}
                />
                <div className="field-hint">Memory bound. Records beyond it are dropped and counted.</div>
              </div>
              <div className="field">
                <label>Maximum message length (chars)</label>
                <input
                  type="number"
                  min={256}
                  max={100000}
                  value={cfg.maxMessageChars}
                  onChange={(e) => patch({ maxMessageChars: Number(e.target.value) || 256 })}
                />
                <div className="field-hint">Longer messages and stack traces are cut.</div>
              </div>
            </div>
          </div>

          <div className="flex-row" style={{ gap: 10, alignItems: "center", marginBottom: 18 }}>
            <button type="submit" className="btn btn-primary" disabled={saving || !dirty}>
              {saving ? "Saving..." : "Save settings"}
            </button>
            {dirty && <span className="cell-muted">Unsaved changes.</span>}
          </div>

          <div className="card">
            <div className="flex-between">
              <h3 className="card-title">Delivery by service</h3>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => load(false)}>
                Refresh
              </button>
            </div>
            <ServicesTable services={services} />
          </div>
        </form>
      )}
    </div>
  );
}

function ServicesTable({ services }: { services: Record<string, ServiceStatus<ForwarderStatus>> }) {
  const rows = Object.entries(services).sort(([a], [b]) => a.localeCompare(b));
  if (rows.length === 0) return <div className="empty-state">No service has reported yet.</div>;
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Service</th>
            <th>Reported</th>
            <th style={{ textAlign: "right" }}>Records</th>
            <th style={{ textAlign: "right" }}>Queued</th>
            <th style={{ textAlign: "right" }}>Dropped (queue full)</th>
            <th>Destinations</th>
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
              <td className="cell-mono" style={{ textAlign: "right" }}>
                {fmt(s.status?.received)}
              </td>
              <td className="cell-mono" style={{ textAlign: "right" }}>
                {fmt(s.status?.queued)}
              </td>
              <td className="cell-mono" style={{ textAlign: "right" }}>
                {s.status?.droppedQueueFull ? <span className="badge badge-danger">{fmt(s.status.droppedQueueFull)}</span> : fmt(s.status?.droppedQueueFull)}
              </td>
              <td>
                {s.status && Object.keys(s.status.destinations).length > 0 ? (
                  <div className="flex-row" style={{ flexWrap: "wrap", gap: 6 }}>
                    {Object.entries(s.status.destinations).map(([id, d]) => (
                      <span
                        key={id}
                        className={`badge ${d.lastError ? "badge-danger" : d.sent > 0 ? "badge-success" : "badge-neutral"}`}
                        title={d.lastError ?? `last delivery ${ago(d.lastOkAt)}`}
                      >
                        {id}: {fmt(d.sent)} sent{d.failed ? ` · ${fmt(d.failed)} failed` : ""}
                        {d.backingOff ? " · backing off" : ""}
                      </span>
                    ))}
                  </div>
                ) : (
                  <span className="cell-muted">{s.status?.active ? "--" : "off"}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
