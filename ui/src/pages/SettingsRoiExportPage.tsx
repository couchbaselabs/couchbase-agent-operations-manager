import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { RoiExportOptions, RoiReport } from "../api/types";

/**
 * Settings -> Export ROI Metrics.
 *
 * One report of what the LLM Cache and the Context Cache saved: a preview
 * here, and the same figures as a Couchbase-branded Excel workbook (the one
 * to forward), a CSV (one flat table to pivot) or JSON. See app/roi_export.py for how each number is derived.
 */

function usd(value: number | null | undefined): string {
  if (value == null) return "-";
  return value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 2,
  });
}

function num(value: number): string {
  return value.toLocaleString();
}

function save(filename: string, blob: Blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

export function SettingsRoiExportPage() {
  const [windowDays, setWindowDays] = useState(7);
  const [contextCost, setContextCost] = useState("");
  const [platformCost, setPlatformCost] = useState("");
  const [report, setReport] = useState<RoiReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"xlsx" | "csv" | "json" | null>(null);

  const options = useCallback(
    (): RoiExportOptions => ({
      window_days: windowDays,
      context_cost_per_fetch_usd: Number(contextCost) > 0 ? Number(contextCost) : undefined,
      platform_cost_usd: Number(platformCost) > 0 ? Number(platformCost) : undefined,
    }),
    [windowDays, contextCost, platformCost],
  );

  // The preview follows the form; typing a cost is debounced so each
  // keystroke does not rerun the aggregate query.
  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const res = await api.roiReport(options());
        if (!cancelled) {
          setReport(res);
          setError(null);
        }
      } catch (e: any) {
        if (!cancelled) setError(e.message || "Failed to load ROI metrics");
      }
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [options]);

  async function handleExport(format: "xlsx" | "csv" | "json") {
    setBusy(format);
    setError(null);
    try {
      if (format !== "json") {
        const { filename, blob } = await api.roiFile(format, options());
        save(filename, blob);
      } else {
        const res = await api.roiReport(options());
        setReport(res);
        save(
          `aom-roi-metrics-${res.generated_at.slice(0, 10)}-${res.window.days}d.json`,
          new Blob([JSON.stringify(res, null, 2)], { type: "application/json" }),
        );
      }
    } catch (e: any) {
      setError(e.message || "Export failed");
    } finally {
      setBusy(null);
    }
  }

  const retention = report?.window.retention_days ?? 7;
  const windowChoices = [1, 7, 14, 30, 90].filter((d) => d <= retention);
  if (!windowChoices.includes(retention)) windowChoices.push(retention);
  const llm = report?.llm;
  const ctx = report?.context;
  const combined = report?.combined;
  const estimate = report?.twelve_month_estimate;
  const hasPlatformCost = estimate?.total.platform_cost_usd != null;
  const monthLabel = (iso: string) =>
    new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Export ROI Metrics</h1>
          <p className="page-subtitle">
            What the LLM Cache and the Context Cache saved - tokens and dollars not spent on model calls, and source
            fetches and time not spent re-fetching context - as a Couchbase-branded Excel report, or as CSV or JSON
            data.
          </p>
        </div>
        <div className="flex-row">
          <button className="btn btn-secondary" onClick={() => handleExport("json")} disabled={busy !== null}>
            {busy === "json" ? "Exporting..." : "Export JSON"}
          </button>
          <button className="btn btn-secondary" onClick={() => handleExport("csv")} disabled={busy !== null}>
            {busy === "csv" ? "Exporting..." : "Export CSV"}
          </button>
          <button className="btn btn-primary" onClick={() => handleExport("xlsx")} disabled={busy !== null}>
            {busy === "xlsx" ? "Exporting..." : "Export Excel"}
          </button>
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}

      <div className="panel section-gap" style={{ marginBottom: 24 }}>
        <div className="three-col-fields">
          <div className="field">
            <label>Reporting window</label>
            <select value={windowDays} onChange={(e) => setWindowDays(Number(e.target.value))}>
              {windowChoices.map((d) => (
                <option key={d} value={d}>
                  {d === 1 ? "Last 24 hours" : `Last ${d} days`}
                </option>
              ))}
            </select>
            <div className="field-hint">
              Limited by how long cache events are kept ({retention} days). All-time totals are always included.
            </div>
          </div>
          <div className="field">
            <label>Cost of one context fetch (USD, optional)</label>
            <input
              type="number"
              min={0}
              step="any"
              value={contextCost}
              onChange={(e) => setContextCost(e.target.value)}
              placeholder="e.g. 0.01"
            />
            <div className="field-hint">
              A context fetch has no token price. Enter what one avoided source fetch costs you - a warehouse query,
              a metered API call - to put Context Cache hits in dollars. Blank reports them as fetches and time only.
            </div>
          </div>
          <div className="field">
            <label>Platform cost for the window (USD, optional)</label>
            <input
              type="number"
              min={0}
              step="any"
              value={platformCost}
              onChange={(e) => setPlatformCost(e.target.value)}
              placeholder="e.g. 250"
            />
            <div className="field-hint">
              What running this appliance cost over the window. Adds net savings and ROI = (savings - cost) / cost.
            </div>
          </div>
        </div>
      </div>

      {!report && !error && <div className="loading-note">Loading ROI metrics...</div>}

      {report && llm && ctx && combined && estimate && (
        <>
          <div className="stat-grid" style={{ marginBottom: 24 }}>
            <div className="stat-card">
              <div className="stat-label">Savings in the window</div>
              <div className="stat-value">{usd(combined.cost_saved_usd)}</div>
              <div className="stat-hint">
                {usd(llm.window.cost_saved_usd)} LLM
                {ctx.window.cost_saved_usd != null ? ` + ${usd(ctx.window.cost_saved_usd)} context` : ""}
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-label">All-time savings</div>
              <div className="stat-value">{usd(combined.lifetime_cost_saved_usd)}</div>
              <div className="stat-hint">{num(llm.lifetime.tokens_saved)} tokens never sent to a provider</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Time saved in the window</div>
              <div className="stat-value">{combined.latency_saved_hours.toLocaleString()} h</div>
              <div className="stat-hint">
                {llm.window.latency_saved_hours} h of model latency, {ctx.window.latency_saved_hours} h of source fetches
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-label">
                {estimate.total.roi_pct != null ? "Twelve-month ROI estimate" : "Twelve-month savings estimate"}
              </div>
              <div className="stat-value">
                {estimate.total.roi_pct != null
                  ? `${estimate.total.roi_pct.toLocaleString()}%`
                  : usd(estimate.total.cost_saved_usd)}
              </div>
              <div className="stat-hint">
                {estimate.total.roi_pct != null
                  ? `${usd(estimate.total.net_savings_usd)} net: ${usd(estimate.total.cost_saved_usd)} saved less ${usd(estimate.total.platform_cost_usd)} platform cost`
                  : "At the current run rate - enter a platform cost for ROI"}
              </div>
            </div>
          </div>

          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>Twelve-month estimate at current usage</h2>
          <div className="helper-banner helper-banner-neutral">
            Based on the last {estimate.basis.window_days === 1 ? "24 hours" : `${estimate.basis.window_days} days`}:{" "}
            {usd(estimate.basis.daily_cost_saved_usd)} saved per day ({num(estimate.basis.daily_tokens_saved)} tokens,{" "}
            {num(estimate.basis.daily_context_fetches_avoided)} context fetches avoided), applied to the calendar days
            of the next twelve months. It assumes usage, hit rate, model prices and platform cost stay where they are
            - no growth or seasonality.
            {estimate.basis.days_with_activity < estimate.basis.window_days &&
              ` Only ${estimate.basis.days_with_activity} of the window's ${estimate.basis.window_days} days had cache activity, so this run rate is likely lower than a normal day.`}
            {!hasPlatformCost && " Enter a platform cost above to add net savings and ROI."}
            {ctx.window.cost_saved_usd == null && " Context Cache savings count as $0 until a cost per fetch is entered."}
          </div>
          <div className="card" style={{ marginBottom: 24 }}>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Month</th>
                    <th>LLM savings</th>
                    <th>Context savings</th>
                    <th>Total savings</th>
                    {hasPlatformCost && <th>Platform cost</th>}
                    {hasPlatformCost && <th>Net savings</th>}
                    <th>Cumulative {hasPlatformCost ? "net" : "savings"}</th>
                    {hasPlatformCost && <th>ROI</th>}
                  </tr>
                </thead>
                <tbody>
                  {estimate.months.map((m) => (
                    <tr key={m.month}>
                      <td>
                        <div style={{ fontWeight: 600 }}>Month {m.month}</div>
                        <div className="cell-muted">
                          from {monthLabel(m.period_start)} ({m.days} days)
                        </div>
                      </td>
                      <td>{usd(m.llm_cost_saved_usd)}</td>
                      <td>{usd(m.context_cost_saved_usd)}</td>
                      <td>{usd(m.cost_saved_usd)}</td>
                      {hasPlatformCost && <td>{usd(m.platform_cost_usd)}</td>}
                      {hasPlatformCost && <td>{usd(m.net_savings_usd)}</td>}
                      <td>{usd(hasPlatformCost ? m.cumulative_net_savings_usd : m.cumulative_savings_usd)}</td>
                      {hasPlatformCost && <td>{m.roi_pct != null ? `${m.roi_pct.toLocaleString()}%` : "-"}</td>}
                    </tr>
                  ))}
                  <tr>
                    <td style={{ fontWeight: 600 }}>Twelve months ({estimate.total.days} days)</td>
                    <td>{usd(estimate.total.llm_cost_saved_usd)}</td>
                    <td>{usd(estimate.total.context_cost_saved_usd)}</td>
                    <td style={{ fontWeight: 600 }}>{usd(estimate.total.cost_saved_usd)}</td>
                    {hasPlatformCost && <td>{usd(estimate.total.platform_cost_usd)}</td>}
                    {hasPlatformCost && <td style={{ fontWeight: 600 }}>{usd(estimate.total.net_savings_usd)}</td>}
                    <td></td>
                    {hasPlatformCost && (
                      <td style={{ fontWeight: 600 }}>
                        {estimate.total.roi_pct != null ? `${estimate.total.roi_pct.toLocaleString()}%` : "-"}
                      </td>
                    )}
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>LLM Cache</h2>
          <div className="card" style={{ marginBottom: 24 }}>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Provider &amp; model</th>
                    <th>Requests</th>
                    <th>Hit rate</th>
                    <th>Tokens saved</th>
                    <th>Cost saved</th>
                    <th>Cost spent</th>
                    <th>Spend avoided</th>
                  </tr>
                </thead>
                <tbody>
                  {llm.by_model.map((m) => (
                    <tr key={`${m.provider}::${m.model}`}>
                      <td>
                        <div style={{ fontWeight: 600 }}>{m.provider_label}</div>
                        <div className="cell-mono">{m.model}</div>
                      </td>
                      <td>{num(m.requests)}</td>
                      <td>{m.hit_rate_pct}%</td>
                      <td>{num(m.tokens_saved)}</td>
                      <td>{usd(m.cost_saved_usd)}</td>
                      <td>{usd(m.cost_spent_usd)}</td>
                      <td>{m.savings_rate_pct}%</td>
                    </tr>
                  ))}
                  <tr>
                    <td style={{ fontWeight: 600 }}>Window total</td>
                    <td>{num(llm.window.requests)}</td>
                    <td>{llm.window.hit_rate_pct}%</td>
                    <td>{num(llm.window.tokens_saved)}</td>
                    <td>{usd(llm.window.cost_saved_usd)}</td>
                    <td>{usd(llm.window.cost_spent_usd)}</td>
                    <td>{llm.window.savings_rate_pct}%</td>
                  </tr>
                </tbody>
              </table>
              {llm.by_model.length === 0 && (
                <div className="empty-state">No LLM calls went through the gateway in this window.</div>
              )}
            </div>
          </div>

          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>Context Cache</h2>
          <div className="card" style={{ marginBottom: 24 }}>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>Namespace</th>
                    <th>Lookups</th>
                    <th>Hit rate</th>
                    <th>Fetches avoided</th>
                    <th>Time saved</th>
                    <th>Est. cost saved</th>
                  </tr>
                </thead>
                <tbody>
                  {ctx.by_agent.slice(0, 25).map((a) => (
                    <tr key={`${a.agent}::${a.namespace}`}>
                      <td style={{ fontWeight: 600 }}>{a.agent}</td>
                      <td className="cell-mono">{a.namespace}</td>
                      <td>{num(a.lookups)}</td>
                      <td>{a.hit_rate_pct}%</td>
                      <td>{num(a.source_fetches_avoided)}</td>
                      <td>{a.latency_saved_hours} h</td>
                      <td>{usd(a.cost_saved_usd)}</td>
                    </tr>
                  ))}
                  <tr>
                    <td style={{ fontWeight: 600 }}>Window total</td>
                    <td></td>
                    <td>{num(ctx.window.lookups)}</td>
                    <td>{ctx.window.hit_rate_pct}%</td>
                    <td>{num(ctx.window.source_fetches_avoided)}</td>
                    <td>{ctx.window.latency_saved_hours} h</td>
                    <td>{usd(ctx.window.cost_saved_usd)}</td>
                  </tr>
                </tbody>
              </table>
              {ctx.by_agent.length === 0 && (
                <div className="empty-state">No context lookups went through the gateway in this window.</div>
              )}
              {ctx.by_agent.length > 25 && (
                <div className="field-hint" style={{ padding: "8px 14px" }}>
                  Showing the top 25 of {ctx.by_agent.length} agent/namespace rows - the export has all of them.
                </div>
              )}
            </div>
          </div>

          <div className="helper-banner helper-banner-neutral">
            <div className="helper-banner-heading">What is in the export</div>
            Window totals, all-time totals, the twelve-month estimate (total and month by month), the per-model and
            per-agent breakdowns above and a day-by-day series for both caches. The Excel export is a Couchbase-branded workbook (Summary, 12-Month Estimate, LLM Cache,
            Context Cache, Assumptions) for sharing. The CSV is one flat table - filter or pivot on its <code>section</code> and{" "}
            <code>scope</code> columns. {report.assumptions.llm_pricing} Bypassed calls (
            {usd(llm.window.bypass_cost_usd)} in this window) are reported separately from cacheable spend.
          </div>
        </>
      )}
    </div>
  );
}
