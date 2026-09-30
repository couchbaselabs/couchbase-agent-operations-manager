import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { usePoll } from "../hooks/usePoll";
import type { LLMCacheEntry, LLMDashboardResponse } from "../api/types";
import { StatCard } from "../components/dashboard/StatCard";
import { DonutChart, StackedBarChart } from "../components/dashboard/Charts";

const HIT_COLOR = "#3ecf8e";
const MISS_COLOR = "#2dd4c8";
const SEMANTIC_COLOR = "#e8a33d";

function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function usd(n: number): string {
  if (n === 0) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function duration(ms: number): string {
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} s`;
  return `${ms} ms`;
}

// "Latency Avoided" can run into weeks' worth of milliseconds under
// sustained traffic - minutes alone gets unreadable fast, so switch to
// hours past the 24h mark, then a compact "###.#k hrs" once that would
// otherwise be a 4+ digit hour count.
function formatLatencyAvoided(ms: number): string {
  const minutes = ms / 60_000;
  if (minutes < 1440) return `${minutes.toFixed(1)} min`;
  const hours = ms / 3_600_000;
  if (hours >= 1000) return `${(hours / 1000).toFixed(1)}k hrs`;
  return `${hours.toFixed(1)} hrs`;
}

function OutcomeBadge({ outcome }: { outcome: string }) {
  const cls =
    outcome === "hit_exact" || outcome === "hit_semantic"
      ? "badge-allow"
      : outcome === "error"
      ? "badge-error"
      : outcome === "bypass"
      ? "badge-neutral"
      : "badge-info";
  const text =
    outcome === "hit_exact" ? "exact hit" : outcome === "hit_semantic" ? "semantic hit" : outcome;
  return <span className={`badge ${cls}`}>{text}</span>;
}

function StateBadge({ state }: { state: LLMCacheEntry["state"] }) {
  const cls = state === "fresh" ? "badge-trusted" : state === "stale" ? "badge-medium" : "badge-untrusted";
  return <span className={`badge ${cls}`}>{state}</span>;
}

export function LLMCachePage() {
  const [data, setData] = useState<LLMDashboardResponse | null>(null);
  const [entries, setEntries] = useState<LLMCacheEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [d, e] = await Promise.all([api.llmDashboard(), api.llmCacheEntries(100)]);
      setData(d);
      setEntries(e.entries);
    } catch (e: any) {
      setError(e.message || "Failed to load the LLM cache dashboard");
    } finally {
      setLoading(false);
    }
  }, []);

  usePoll(load, 30000);

  async function handleSweep() {
    setNote(null);
    try {
      const res = await api.sweepLlmCache();
      setNote(`Invalidation sweep complete - ${res.removed} entr(ies) removed.`);
      await load();
    } catch (e: any) {
      setError(e.message || "Sweep failed");
    }
  }

  async function handlePurgeAll() {
    if (!confirm("Purge every cached completion? Agents will pay full token cost again until the cache refills.")) return;
    setNote(null);
    try {
      const res = await api.purgeLlmCache({});
      setNote(`Purged ${res.purged} cache entr(ies).`);
      await load();
    } catch (e: any) {
      setError(e.message || "Purge failed");
    }
  }

  async function handleDelete(entryId: string) {
    setBusyId(entryId);
    try {
      await api.deleteLlmCacheEntry(entryId);
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
          <h1 className="page-title">LLM Cache Dashboard</h1>
          <p className="page-subtitle">
            {data
              ? `${data.events_examined} request(s) in the last 24h - serving ${data.provider_label} (${data.model})${
                  data.api_key_configured ? "" : " in offline stub mode"
                }`
              : "Loading..."}
          </p>
        </div>
        <div className="flex-row">
          <Link to="/llm-caching/settings" className="btn btn-secondary btn-sm">
            Providers &amp; policy
          </Link>
          <button className="btn btn-primary" onClick={load} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}
      {note && <div className="loading-note">{note}</div>}

      {data && !data.enabled && (
        <div className="helper-banner">
          Caching is currently <strong>disabled</strong> in the active policy - every agent completion is going
          straight to {data.provider_label}. Re-enable it on the{" "}
          <Link to="/llm-caching/settings">Providers &amp; policy</Link> page.
        </div>
      )}

      {data && s && (
        <>
          <div className="stat-grid">
            <StatCard
              label="Total Tokens Saved"
              value={compactNumber(data.tokens_saved_total)}
              hint={`${compactNumber(s.tokens_spent)} tokens still billed on misses (last 24h)`}
            />
            <StatCard
              label="Total Estimated Cost Saved"
              value={usd(data.cost_saved_usd_total)}
              hint={`${usd(s.cost_spent_usd)} spent on provider calls (last 24h)`}
            />
            <StatCard
              label="Cache Hit Rate"
              value={`${s.hit_rate_pct}%`}
              hint={`${s.hits} hit(s) of ${s.cacheable_requests} cacheable request(s)`}
            />
            <StatCard
              label="Latency Avoided"
              value={formatLatencyAvoided(s.latency_saved_ms)}
              hint={`${s.avg_hit_latency_ms}ms per hit vs ${s.avg_miss_latency_ms}ms per miss`}
            />
          </div>

          <div className="chart-grid">
            <div className="card">
              <h3 className="card-title">Cache hits vs provider calls (last 12h)</h3>
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
                  { label: "Sent to provider", color: MISS_COLOR },
                ]}
              />
            </div>
            <div className="card">
              <h3 className="card-title">How requests were resolved (last 24hrs)</h3>
              <DonutChart
                segments={[
                  { label: "Exact hit", value: s.exact_hits, color: HIT_COLOR },
                  { label: "Semantic hit", value: s.semantic_hits, color: SEMANTIC_COLOR },
                  { label: "Provider call", value: s.misses, color: MISS_COLOR },
                  { label: "Bypassed", value: s.bypasses, color: "#8b93a7" },
                  { label: "Error", value: s.errors, color: "#ea2328" },
                ]}
              />
            </div>
          </div>

          <div className="flex-between" style={{ marginBottom: 14 }}>
            <h2 style={{ fontSize: 16, margin: 0 }}>Savings by provider &amp; model (last 24hrs)</h2>
            <span className="cell-muted" style={{ fontSize: 12 }}>
              Cost figures are list-price estimates, not billing data
            </span>
          </div>
          {data.model_breakdown.length === 0 ? (
            <div className="card empty-state">
              No completions have been routed through the caching gateway yet. Send one from the Providers &amp;
              policy page, or POST to <span className="cell-mono">/v1/llm/complete</span>.
            </div>
          ) : (
            <div className="card" style={{ marginBottom: 24 }}>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Provider</th>
                      <th>Model</th>
                      <th>Requests</th>
                      <th>Hit rate</th>
                      <th>Tokens saved</th>
                      <th>Total cost saved</th>
                      <th>Total cost spent</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.model_breakdown.map((r) => (
                      <tr key={`${r.provider}:${r.model}`}>
                        <td style={{ fontWeight: 600 }}>{r.provider_label}</td>
                        <td className="cell-mono cell-muted">{r.model}</td>
                        <td>{r.requests}</td>
                        <td>{r.hit_rate_pct}%</td>
                        <td>{compactNumber(r.tokens_saved)}</td>
                        <td style={{ color: "var(--green)", fontWeight: 600 }}>{usd(r.cost_saved_usd)}</td>
                        <td className="cell-muted">{usd(r.cost_spent_usd)}</td>
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
            <div className="card empty-state">The cache is empty.</div>
          ) : (
            <div className="card" style={{ marginBottom: 24 }}>
              <div className="table-wrap scroll" style={{ maxHeight: 620 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Prompt</th>
                      <th>Model</th>
                      <th>Scope</th>
                      <th>Hits</th>
                      <th>Tokens saved</th>
                      <th>Age</th>
                      <th>State</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries?.map((e) => (
                      <tr key={e.entry_id}>
                        <td style={{ maxWidth: 340 }}>
                          <div style={{ fontWeight: 600 }}>{e.prompt_preview}</div>
                          <div className="cell-muted" style={{ fontSize: 12 }}>
                            {e.response_preview}
                          </div>
                        </td>
                        <td className="cell-mono cell-muted">{e.model}</td>
                        <td className="cell-mono cell-muted">{e.scope_key}</td>
                        <td>
                          {e.hit_count}
                          <div className="cell-muted" style={{ fontSize: 11.5 }}>
                            {e.exact_hits} exact / {e.semantic_hits} semantic
                          </div>
                        </td>
                        <td>{compactNumber(e.tokens_saved)}</td>
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
                      <th>Model</th>
                      <th>Role</th>
                      <th>Prompt</th>
                      <th>Similarity</th>
                      <th>Tokens saved</th>
                      <th>Latency</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent_events.map((e, i) => (
                      <tr key={i}>
                        <td className="cell-mono cell-muted">{e.timestamp?.replace("T", " ").replace("Z", "")}</td>
                        <td>
                          <OutcomeBadge outcome={e.outcome} />
                        </td>
                        <td className="cell-mono cell-muted">{e.model}</td>
                        <td className="cell-mono cell-muted">{e.role || "-"}</td>
                        <td style={{ maxWidth: 320 }} className="cell-muted">
                          {e.prompt_preview}
                        </td>
                        <td className="cell-mono">{e.similarity != null ? e.similarity.toFixed(3) : "-"}</td>
                        <td>{compactNumber(e.tokens_saved || 0)}</td>
                        <td className="cell-muted">{e.latency_ms}ms</td>
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
