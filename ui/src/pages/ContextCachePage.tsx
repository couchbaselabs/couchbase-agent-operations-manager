import { useCallback, useState } from "react";
import { api } from "../api/client";
import { usePoll } from "../hooks/usePoll";
import type { ContextCacheEntry, ContextDashboardResponse } from "../api/types";
import { StatCard } from "../components/dashboard/StatCard";
import { DonutChart, StackedBarChart } from "../components/dashboard/Charts";

const HIT_COLOR = "#3ecf8e";
const MISS_COLOR = "#2dd4c8";
const WRITE_COLOR = "#e8a33d";

function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function bytesLabel(n: number): string {
  if (n >= 1_048_576) return `${(n / 1_048_576).toFixed(1)} MB`;
  if (n >= 1_024) return `${(n / 1_024).toFixed(1)} KB`;
  return `${n} B`;
}

function duration(ms: number): string {
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} s`;
  return `${ms} ms`;
}

// Same reasoning as LLMCachePage's formatLatencyAvoided - sustained
// traffic runs the all-time "Latency Avoided" figure into the weeks'-worth
// of milliseconds range fast, so switch units as it grows.
function formatLatencyAvoided(ms: number): string {
  const minutes = ms / 60_000;
  if (minutes < 1440) return `${minutes.toFixed(1)} min`;
  const hours = ms / 3_600_000;
  if (hours >= 1000) return `${(hours / 1000).toFixed(1)}k hrs`;
  return `${hours.toFixed(1)} hrs`;
}

function OutcomeBadge({ outcome }: { outcome: string }) {
  const cls = outcome === "hit" ? "badge-allow" : outcome === "write" ? "badge-info" : "badge-neutral";
  return <span className={`badge ${cls}`}>{outcome}</span>;
}

function StateBadge({ state }: { state: ContextCacheEntry["state"] }) {
  const cls = state === "fresh" ? "badge-trusted" : "badge-untrusted";
  return <span className={`badge ${cls}`}>{state}</span>;
}

export function ContextCachePage() {
  const [data, setData] = useState<ContextDashboardResponse | null>(null);
  const [entries, setEntries] = useState<ContextCacheEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [d, e] = await Promise.all([api.contextDashboard(), api.contextCacheEntries(100)]);
      setData(d);
      setEntries(e.entries);
    } catch (e: any) {
      setError(e.message || "Failed to load the context cache dashboard");
    } finally {
      setLoading(false);
    }
  }, []);

  usePoll(load, 30000);

  async function handleSweep() {
    setNote(null);
    try {
      const res = await api.sweepContextCache();
      setNote(`Invalidation sweep complete - ${res.removed} entr(ies) removed.`);
      await load();
    } catch (e: any) {
      setError(e.message || "Sweep failed");
    }
  }

  async function handlePurgeAll() {
    if (!confirm("Purge every cached context entry? Agents will re-fetch from their own data source on the next lookup.")) return;
    setNote(null);
    try {
      const res = await api.purgeContextCache({});
      setNote(`Purged ${res.purged} cache entr(ies).`);
      await load();
    } catch (e: any) {
      setError(e.message || "Purge failed");
    }
  }

  async function handleDelete(entryId: string) {
    setBusyId(entryId);
    try {
      await api.deleteContextCacheEntry(entryId);
      await load();
    } catch (e: any) {
      setError(`Delete failed: ${e.message}`);
    } finally {
      setBusyId(null);
    }
  }

  const s = data?.summary;

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Context Cache</h1>
          <p className="page-subtitle">
            {data
              ? `${s?.lookups ?? 0} lookup(s) in the last 24h across ${data.agent_breakdown.length} agent/namespace pair(s)`
              : "Loading..."}
          </p>
        </div>
        <div className="flex-row">
          <button className="btn btn-primary" onClick={load} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>

      <p className="cell-muted" style={{ marginTop: -8, marginBottom: 18, fontSize: 13, maxWidth: 720 }}>
        What every agent using the AOM SDK's <span className="cell-mono">context_get</span> /{" "}
        <span className="cell-mono">context_set</span> (or <span className="cell-mono">cached_context</span>) methods
        has cached from its own data sources - product catalogs, warehouse lookups, API responses, anything an agent
        fetches to build context rather than asks an LLM to generate.
      </p>

      {error && <div className="error-note">{error}</div>}
      {note && <div className="loading-note">{note}</div>}

      {data && !data.enabled && (
        <div className="helper-banner">
          Context caching is currently <strong>disabled</strong> in the active policy - every{" "}
          <span className="cell-mono">context_get</span> call is reporting a miss. Re-enable it via{" "}
          <span className="cell-mono">PUT /v1/context/config</span>.
        </div>
      )}

      {data && s && (
        <>
          <div className="stat-grid">
            <StatCard
              label="Total Lookups"
              value={compactNumber(data.lookups_total)}
              hint={`${compactNumber(s.lookups)} lookup(s) in the last 24h`}
            />
            <StatCard
              label="Cache Hit Rate"
              value={`${s.hit_rate_pct}%`}
              hint={`${s.hits} hit(s) of ${s.lookups} lookup(s) (24h)`}
            />
            <StatCard
              label="Latency Avoided"
              value={formatLatencyAvoided(data.latency_saved_ms_total)}
              hint={`${s.avg_hit_latency_ms}ms per hit vs ${s.avg_miss_latency_ms}ms per miss`}
            />
            <StatCard
              label="Cached Entries"
              value={compactNumber(data.cached_entries)}
              hint={`${bytesLabel(s.bytes_cached)} written in the last 24h - ${data.max_entries.toLocaleString()} max`}
            />
          </div>

          <div className="chart-grid">
            <div className="card">
              <h3 className="card-title">Cache hits vs source fetches (last 12h)</h3>
              <StackedBarChart
                height={200}
                data={data.hourly.map((h) => ({
                  label: h.hour,
                  segments: [
                    { value: h.hits, color: HIT_COLOR },
                    { value: h.misses, color: MISS_COLOR },
                  ],
                }))}
                legend={[
                  { label: "Served from cache", color: HIT_COLOR },
                  { label: "Fetched from source", color: MISS_COLOR },
                ]}
              />
            </div>
            <div className="card">
              <h3 className="card-title">How lookups were resolved (last 24hrs)</h3>
              <DonutChart
                segments={[
                  { label: "Cache hit", value: s.hits, color: HIT_COLOR },
                  { label: "Miss (fetched)", value: s.misses, color: MISS_COLOR },
                  { label: "New writes", value: s.writes, color: WRITE_COLOR },
                ]}
              />
            </div>
          </div>

          <div className="flex-between" style={{ marginBottom: 14 }}>
            <h2 style={{ fontSize: 16, margin: 0 }}>Traffic by agent &amp; namespace (last 24hrs)</h2>
            <span className="cell-muted" style={{ fontSize: 12 }}>
              Any identity calling the SDK's context_get/context_set shows up here automatically
            </span>
          </div>
          {data.agent_breakdown.length === 0 ? (
            <div className="card empty-state">
              No agent has called <span className="cell-mono">context_get</span> or{" "}
              <span className="cell-mono">context_set</span> yet. Any agent using the AOM SDK's context-caching
              methods will appear here the moment it does.
            </div>
          ) : (
            <div className="card" style={{ marginBottom: 24 }}>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Agent</th>
                      <th>Namespace</th>
                      <th>Lookups</th>
                      <th>Hit rate</th>
                      <th>Writes</th>
                      <th>Latency avoided</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.agent_breakdown.map((r) => (
                      <tr key={`${r.agent}:${r.namespace}`}>
                        <td style={{ fontWeight: 600 }}>{r.agent}</td>
                        <td className="cell-mono cell-muted">{r.namespace}</td>
                        <td>{r.lookups}</td>
                        <td>{r.hit_rate_pct}%</td>
                        <td className="cell-muted">{r.writes}</td>
                        <td style={{ color: "var(--green)", fontWeight: 600 }}>
                          {formatLatencyAvoided(r.latency_saved_ms)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="flex-between" style={{ marginBottom: 14 }}>
            <h2 style={{ fontSize: 16, margin: 0 }}>
              Cached entries{" "}
              <span className="cell-muted" style={{ fontWeight: 400, fontSize: 13 }}>
                ({data.cached_entries})
              </span>
            </h2>
            <div className="flex-row">
              <button className="btn btn-secondary btn-sm" onClick={handleSweep}>
                Run invalidation sweep
              </button>
              <button className="btn btn-danger-outline btn-sm" onClick={handlePurgeAll}>
                Purge all
              </button>
            </div>
          </div>

          {entries && entries.length === 0 ? (
            <div className="card empty-state">The context cache is empty.</div>
          ) : (
            <div className="card" style={{ marginBottom: 24 }}>
              <div className="table-wrap scroll" style={{ maxHeight: 620 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Key</th>
                      <th>Value preview</th>
                      <th>Scope</th>
                      <th>Hits</th>
                      <th>Size</th>
                      <th>Age</th>
                      <th>State</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries?.map((e) => (
                      <tr key={e.entry_id}>
                        <td style={{ maxWidth: 260 }} className="cell-mono">
                          {e.key_preview}
                          <div className="cell-muted" style={{ fontSize: 11.5, fontFamily: "inherit" }}>
                            {e.subject || "unknown"} {e.role ? `(${e.role})` : ""}
                          </div>
                        </td>
                        <td style={{ maxWidth: 280 }} className="cell-muted">
                          {e.value_preview}
                        </td>
                        <td className="cell-mono cell-muted">{e.scope_key}</td>
                        <td>{e.hit_count}</td>
                        <td className="cell-muted">{bytesLabel(e.value_bytes)}</td>
                        <td className="cell-muted">{duration(e.age_seconds * 1000)}</td>
                        <td>
                          <StateBadge state={e.state} />
                          {e.state_reason && (
                            <div className="cell-muted" style={{ fontSize: 11.5, marginTop: 4 }}>
                              {e.state_reason}
                            </div>
                          )}
                        </td>
                        <td>
                          <button
                            className="btn btn-danger-outline btn-sm"
                            disabled={busyId === e.entry_id}
                            onClick={() => handleDelete(e.entry_id)}
                          >
                            Invalidate
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Recent cache events</h2>
          {data.recent_events.length === 0 ? (
            <div className="card empty-state">No cache events in the current retention window.</div>
          ) : (
            <div className="card">
              <div className="table-wrap scroll" style={{ maxHeight: 460 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Time (UTC)</th>
                      <th>Outcome</th>
                      <th>Agent</th>
                      <th>Namespace</th>
                      <th>Key</th>
                      <th>Latency</th>
                      <th>Latency avoided</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent_events.map((e, i) => (
                      <tr key={i}>
                        <td className="cell-mono cell-muted">{e.timestamp?.replace("T", " ").replace("Z", "")}</td>
                        <td>
                          <OutcomeBadge outcome={e.outcome} />
                        </td>
                        <td className="cell-mono cell-muted">{e.subject || "-"}</td>
                        <td className="cell-mono cell-muted">{e.namespace}</td>
                        <td style={{ maxWidth: 260 }} className="cell-mono cell-muted">
                          {e.key_preview}
                        </td>
                        <td className="cell-muted">{e.latency_ms}ms</td>
                        <td className="cell-muted">{e.latency_saved_ms ? `${e.latency_saved_ms}ms` : "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
