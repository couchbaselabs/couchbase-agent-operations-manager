import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { SpanKind, TraceDetailResponse, TraceRun, TracesResponse } from "../api/types";

// One colour per span kind, so a timeline reads as a shape before it reads
// as text. Deliberately reuses the existing badge palette rather than
// introducing a second colour vocabulary to the dashboard.
const KIND_BADGE: Record<SpanKind, string> = {
  user: "badge-info",
  llm: "badge-medium",
  tool_call: "badge-trusted",
  tool_result: "badge-low",
  hand_off: "badge-neutral",
  internal: "badge-neutral",
  assistant: "badge-info",
};

const KIND_LABEL: Record<SpanKind, string> = {
  user: "user",
  llm: "llm",
  tool_call: "tool call",
  tool_result: "tool result",
  hand_off: "hand-off",
  internal: "internal",
  assistant: "assistant",
};

function shortTime(ts?: string | null) {
  return ts ? ts.replace("T", " ").replace("Z", "") : "-";
}

function SpanRow({ span, widest }: { span: TraceDetailResponse["spans"][number]; widest: number }) {
  const [open, setOpen] = useState(false);
  const attrs = Object.entries(span.attributes || {}).filter(([, v]) => v !== null && v !== "" && v !== undefined);
  // Bar width is relative to the slowest span in the run, so the timeline
  // shows where the time actually went rather than an absolute scale
  // nobody can read across runs of wildly different length.
  const width = widest > 0 ? Math.max(2, Math.round((span.latency_ms / widest) * 100)) : 2;

  return (
    <>
      <tr>
        <td>
          <span className={`badge ${KIND_BADGE[span.kind] || "badge-neutral"}`}>{KIND_LABEL[span.kind] || span.kind}</span>
        </td>
        <td className="cell-mono" style={{ fontWeight: 600 }}>
          {span.name}
          {span.status === "error" && <span className="badge badge-error" style={{ marginLeft: 8 }}>error</span>}
        </td>
        <td style={{ minWidth: 160 }}>
          <div
            title={`${span.latency_ms} ms`}
            style={{
              height: 8,
              width: `${width}%`,
              borderRadius: 4,
              background: span.status === "error" ? "var(--danger, #c0392b)" : "var(--accent, #4a9d8e)",
            }}
          />
        </td>
        <td className="cell-mono cell-muted" style={{ textAlign: "right" }}>
          {span.latency_ms} ms
        </td>
        <td>
          {attrs.length > 0 && (
            <button className="btn btn-secondary btn-sm" onClick={() => setOpen((v) => !v)}>
              {open ? "Hide" : "Attributes"}
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5}>
            <div className="json-block">
              {attrs.map(([k, v]) => (
                <div key={k}>
                  <span className="cell-muted">{k}</span>: {Array.isArray(v) ? v.join(", ") : String(v)}
                </div>
              ))}
              {span.error && (
                <div style={{ marginTop: 8 }}>
                  <span className="cell-muted">error</span>: {span.error}
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function TraceDetail({ traceId, onClose }: { traceId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<TraceDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .trace(traceId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e: any) => !cancelled && setError(e.message || "Failed to load trace"));
    return () => {
      cancelled = true;
    };
  }, [traceId]);

  if (error) return <div className="error-note">{error}</div>;
  if (!detail) return <div className="card empty-state">Loading trace...</div>;

  const widest = Math.max(1, ...detail.spans.map((s) => s.latency_ms));
  const run = detail.run;

  return (
    <div className="card" style={{ marginBottom: 24 }}>
      <div className="flex-between" style={{ marginBottom: 12 }}>
        <div>
          <div className="card-title">Run {traceId.slice(0, 12)}</div>
          <div className="cell-muted" style={{ fontSize: 13 }}>
            {run?.first_query || "no query recorded"}
          </div>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          Close
        </button>
      </div>

      {run && (
        <div className="stat-grid" style={{ marginBottom: 16 }}>
          <div className="stat-card">
            <div className="stat-label">Spans</div>
            <div className="stat-value">{run.span_count}</div>
            <div className="stat-hint">{run.error_count} error(s)</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Tokens</div>
            <div className="stat-value">{run.total_tokens.toLocaleString()}</div>
            <div className="stat-hint">
              {run.cache_hits} cache hit(s), {run.cache_misses} miss(es)
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Cost</div>
            <div className="stat-value">${run.cost_usd.toFixed(4)}</div>
            <div className="stat-hint">List-price estimate</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Memory recalls</div>
            <div className="stat-value">{run.memory_recalls}</div>
            <div className="stat-hint">Vector searches over agent memory</div>
          </div>
        </div>
      )}

      <h2 style={{ fontSize: 15, margin: "8px 0 12px 0" }}>Timeline</h2>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Kind</th>
              <th>Operation</th>
              <th>Duration</th>
              <th style={{ textAlign: "right" }}>ms</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {detail.spans.map((s) => (
              <SpanRow key={s.span_id} span={s} widest={widest} />
            ))}
          </tbody>
        </table>
      </div>

      {detail.tools.length > 0 && (
        <>
          <h2 style={{ fontSize: 15, margin: "20px 0 12px 0" }}>Tools this run touched, as the catalog has them now</h2>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Tool</th>
                  <th>Server</th>
                  <th>Risk</th>
                  <th>Trust</th>
                  <th>Allowed roles</th>
                  <th>Definition</th>
                </tr>
              </thead>
              <tbody>
                {detail.tools.map((t) => (
                  <tr key={t.tool_id}>
                    <td className="cell-mono">{t.tool_id}</td>
                    <td className="cell-muted">{t.server_id}</td>
                    <td className="cell-muted">{t.risk_level}</td>
                    <td>
                      <span className={`badge ${t.trust_status === "trusted" ? "badge-trusted" : "badge-untrusted"}`}>
                        {t.trust_status}
                      </span>
                      {t.drift_status === "drifted" && (
                        <span className="badge badge-critical" style={{ marginLeft: 6 }}>
                          drifted
                        </span>
                      )}
                    </td>
                    <td className="cell-muted">{(t.allowed_roles || []).join(", ") || "-"}</td>
                    <td className="cell-muted cell-mono">v{t.definition_version ?? 1}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

export function TracesPage() {
  const [data, setData] = useState<TracesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [windowHours, setWindowHours] = useState(24);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.traces({ status: statusFilter || undefined, windowHours }));
    } catch (e: any) {
      setError(e.message || "Failed to load traces");
    } finally {
      setLoading(false);
    }
  }, [statusFilter, windowHours]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Traces</h1>
          <p className="page-subtitle">
            Every discover, invoke, completion and memory recall, grouped into the run it belonged to. Spans are
            stored in the same Couchbase cluster as the catalog, the cache and agent memory, so a run can be read
            against the catalog it actually ran against.
          </p>
        </div>
        <button className="btn btn-primary" onClick={load} disabled={loading}>
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}

      {data && !data.tracing_enabled && (
        <div className="helper-banner helper-banner-neutral">
          Tracing is turned off (<span className="mono">TRACING_ENABLED=false</span>). Runs already recorded are
          still shown; nothing new is being written.
        </div>
      )}

      {data && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">Runs</div>
              <div className="stat-value">{data.totals.runs}</div>
              <div className="stat-hint">Last {data.window_hours}h</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Errored spans</div>
              <div className="stat-value">{data.totals.errors}</div>
              <div className="stat-hint">Denied, failed or timed out</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Tokens</div>
              <div className="stat-value">{data.totals.tokens.toLocaleString()}</div>
              <div className="stat-hint">${data.totals.cost_usd.toFixed(4)} estimated</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Stopped by a limit</div>
              <div className="stat-value">{data.totals.limit_blocks}</div>
              <div className="stat-hint">{data.totals.hijack_flags} flagged response(s)</div>
            </div>
          </div>

          <div className="flex-row" style={{ margin: "18px 0 14px 0", gap: 12 }}>
            <div className="field" style={{ margin: 0 }}>
              <label>Status</label>
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="">All runs</option>
                <option value="error">Errors only</option>
                <option value="ok">Clean only</option>
              </select>
            </div>
            <div className="field" style={{ margin: 0 }}>
              <label>Window</label>
              <select value={String(windowHours)} onChange={(e) => setWindowHours(Number(e.target.value))}>
                <option value="1">Last hour</option>
                <option value="24">Last 24 hours</option>
                <option value="168">Last 7 days</option>
                <option value="336">Last 14 days</option>
              </select>
            </div>
          </div>

          {selected && <TraceDetail traceId={selected} onClose={() => setSelected(null)} />}

          {data.runs.length === 0 ? (
            <div className="card empty-state">
              No runs recorded yet. Any call through the gateway writes one - try the Agent Tool Audit page, or
              point an agent at the SDK.
            </div>
          ) : (
            <div className="card">
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Started (UTC)</th>
                      <th>Run</th>
                      <th>Role</th>
                      <th>What it did</th>
                      <th style={{ textAlign: "right" }}>Spans</th>
                      <th style={{ textAlign: "right" }}>Tokens</th>
                      <th style={{ textAlign: "right" }}>Cost</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.runs.map((r: TraceRun) => (
                      <tr key={r.trace_id}>
                        <td className="cell-mono cell-muted">{shortTime(r.started_at)}</td>
                        <td className="cell-mono">
                          {r.trace_id.slice(0, 10)}
                          {r.status === "error" && <span className="badge badge-error" style={{ marginLeft: 8 }}>error</span>}
                          {r.limit_blocks > 0 && <span className="badge badge-medium" style={{ marginLeft: 6 }}>limited</span>}
                          {r.hijack_flags > 0 && <span className="badge badge-critical" style={{ marginLeft: 6 }}>flagged</span>}
                        </td>
                        <td className="cell-muted">{r.role || "-"}</td>
                        <td className="cell-muted" style={{ maxWidth: 380 }}>
                          {r.first_query || (r.tools_called.length ? r.tools_called.join(", ") : "-")}
                        </td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>{r.span_count}</td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>{r.total_tokens.toLocaleString()}</td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>${r.cost_usd.toFixed(4)}</td>
                        <td>
                          <button className="btn btn-secondary btn-sm" onClick={() => setSelected(r.trace_id)}>
                            Open
                          </button>
                        </td>
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
