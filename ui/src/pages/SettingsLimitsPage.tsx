import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { LimitOverridesPanel } from "../components/limits/LimitOverridesPanel";
import type {
  GovernanceConfig,
  GovernanceConfigResponse,
  LimitOverride,
  LimitVerdict,
  SharedLimitFields,
} from "../api/types";

const FAMILIES = ["requests", "tool_calls", "tokens", "spend"];

function usageLabel(v: LimitVerdict) {
  if (v.family === "spend") return `$${Number(v.used).toFixed(4)} / $${Number(v.limit).toFixed(2)}`;
  return `${Math.round(Number(v.used)).toLocaleString()} / ${Math.round(Number(v.limit)).toLocaleString()}`;
}

function UsageBar({ verdict }: { verdict: LimitVerdict }) {
  const pct = verdict.limit ? Math.min(100, Math.round((Number(verdict.used) / Number(verdict.limit)) * 100)) : 0;
  return (
    <div>
      <div
        style={{
          height: 6,
          borderRadius: 3,
          background: "var(--surface-2, rgba(128,128,128,0.18))",
          overflow: "hidden",
          marginBottom: 4,
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${pct}%`,
            background: verdict.exceeded ? "var(--danger, #c0392b)" : "var(--accent, #4a9d8e)",
          }}
        />
      </div>
      <span className="cell-muted cell-mono" style={{ fontSize: 12 }}>
        {usageLabel(verdict)}
      </span>
    </div>
  );
}

export function SettingsLimitsPage() {
  const [data, setData] = useState<GovernanceConfigResponse | null>(null);
  const [config, setConfig] = useState<GovernanceConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);

  // Current usage filters
  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [limitFilter, setLimitFilter] = useState<"" | "over" | "custom" | "active">("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.governanceConfig();
      setData(res);
      setConfig(res.config);
    } catch (e: any) {
      setError(e.message || "Failed to load the limits policy");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function patch(update: Partial<GovernanceConfig>) {
    setConfig((c) => (c ? { ...c, ...update } : c));
  }

  async function handleSave() {
    if (!config) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.saveGovernanceConfig(config);
      setConfig(res.config);
      setNotice("Saved. New limits apply to the next call - counters already in flight keep their current window.");
      await load();
    } catch (e: any) {
      setError(e.message || "Could not save the limits policy");
    } finally {
      setSaving(false);
    }
  }

  function patchGlobal(update: Partial<SharedLimitFields>) {
    setConfig((c) => (c ? { ...c, global_limits: { ...c.global_limits, ...update } } : c));
  }

  // Adding, editing or removing a role/agent entry saves straight away -
  // along with anything else changed on the page, since it is one policy.
  async function handleSaveOverrides(overrides: LimitOverride[], note: string): Promise<boolean> {
    if (!config) return false;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.saveGovernanceConfig({ ...config, overrides });
      setConfig(res.config);
      setNotice(`${note} It applies to the next call.`);
      await load();
      return true;
    } catch (e: any) {
      setError(e.message || "Could not save the limits");
      return false;
    } finally {
      setSaving(false);
    }
  }

  const filteredUsage = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (data?.usage || []).filter((row) => {
      if (needle && ![row.agent, row.subject, row.agent_id, row.role].some((v) => (v || "").toLowerCase().includes(needle)))
        return false;
      if (roleFilter && row.role !== roleFilter) return false;
      if (limitFilter === "over" && !row.limits.some((l) => l.exceeded)) return false;
      if (limitFilter === "custom" && !row.limits.some((l) => l.source === "role" || l.source === "agent")) return false;
      if (limitFilter === "active" && !row.limits.some((l) => Number(l.used) > 0)) return false;
      return true;
    });
  }, [data, search, roleFilter, limitFilter]);
  const filtering = !!(search.trim() || roleFilter || limitFilter);

  function toggleExemptRole(role: string) {
    if (!config) return;
    const current = config.exempt_roles || [];
    patch({
      exempt_roles: current.includes(role) ? current.filter((r) => r !== role) : [...current, role],
    });
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Limits &amp; Budgets</h1>
          <p className="page-subtitle">
            Ceilings on what may be consumed through this gateway - request and tool-call rates, a per-run tool
            ceiling, a rolling token budget and a daily spend budget - set globally for all callers combined, as
            defaults for each caller, and per role or per agent. Plus the timeouts on downstream and provider calls.
            Counted with Couchbase KV atomic counters, so a window rolls itself and the count survives a restart.
          </p>
        </div>
        <button className="btn btn-primary" onClick={handleSave} disabled={saving || !config}>
          {saving ? "Saving..." : "Save policy"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}
      {notice && <div className="helper-banner helper-banner-neutral">{notice}</div>}

      {config && data && (
        <>
          {config.enabled && !config.enforce && (
            <div className="helper-banner helper-banner-neutral" style={{ marginBottom: 18 }}>
              <div className="helper-banner-heading">Measuring, not enforcing</div>
              Every ceiling below is being evaluated and recorded, and nothing is being blocked. Leave it this way
              until the usage table shows what normal traffic actually consumes - turning limits on in front of live
              agents without knowing that is how a control like this gets switched off permanently.
            </div>
          )}

          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>1. Policy</h2>
          <div className="panel section-gap" style={{ marginBottom: 24 }}>
            <div className="two-col">
              <div className="field">
                <label className="checkbox-row">
                  <input type="checkbox" checked={config.enabled} onChange={(e) => patch({ enabled: e.target.checked })} />
                  <span>Apply limits</span>
                </label>
                <div className="field-hint">
                  Off means no counting at all. The gateway behaves exactly as it did before this page existed.
                </div>
              </div>
              <div className="field">
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={config.enforce}
                    disabled={!config.enabled}
                    onChange={(e) => patch({ enforce: e.target.checked })}
                  />
                  <span>Enforce (block calls over a limit)</span>
                </label>
                <div className="field-hint">
                  Off measures and records without blocking. A refused call returns 429 with a Retry-After header
                  and is written to the audit log as a DENY.
                </div>
              </div>
            </div>
          </div>

          <div className="helper-banner helper-banner-neutral" style={{ marginBottom: 18 }}>
            <div className="helper-banner-heading">What these cover</div>
            The request rate and the request timeout are applied in one place to every agent-reachable route -
            discover, invoke, completions, all four memory routes, and the SDK and skill downloads - so a route
            cannot be unbounded by having forgotten to ask. The tool-call, token and spend ceilings are applied by
            the routes that can know about them. Downloads carry no API key by design, so they are counted against
            the caller's address instead.
          </div>

          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>2. Global limits and budgets</h2>
          <div className="panel section-gap" style={{ marginBottom: 24 }}>
            <div className="field-hint" style={{ marginTop: 0, marginBottom: 14 }}>
              One ceiling for the whole gateway - every caller, role and agent combined. It holds even when each
              caller is inside its own limits, and when it is reached every caller is refused until the window rolls.
              0 disables each one; roles exempt from every limit are not counted.
            </div>
            <div className="two-col">
              <div className="field">
                <label>Requests per minute, all callers</label>
                <input
                  type="number"
                  min={0}
                  value={config.global_limits.requests_per_minute}
                  onChange={(e) => patchGlobal({ requests_per_minute: Number(e.target.value) })}
                />
              </div>
              <div className="field">
                <label>Tool calls per minute, all callers</label>
                <input
                  type="number"
                  min={0}
                  value={config.global_limits.tool_calls_per_minute}
                  onChange={(e) => patchGlobal({ tool_calls_per_minute: Number(e.target.value) })}
                />
              </div>
            </div>
            <div className="two-col">
              <div className="field">
                <label>Tokens per hour, all callers</label>
                <input
                  type="number"
                  min={0}
                  value={config.global_limits.tokens_per_hour}
                  onChange={(e) => patchGlobal({ tokens_per_hour: Number(e.target.value) })}
                />
              </div>
              <div className="field">
                <label>Spend per day, all callers (USD)</label>
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  value={config.global_limits.spend_per_day_usd}
                  onChange={(e) => patchGlobal({ spend_per_day_usd: Number(e.target.value) })}
                />
                <div className="field-hint">The total LLM budget for the appliance, at the dashboard&rsquo;s price estimates.</div>
              </div>
            </div>
          </div>

          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>3. Default limits for each caller</h2>
          <div className="panel section-gap" style={{ marginBottom: 24 }}>
            <div className="two-col">
              <div className="field">
                <label>Requests per minute, per caller</label>
                <input
                  type="number"
                  min={0}
                  value={config.requests_per_minute}
                  onChange={(e) => patch({ requests_per_minute: Number(e.target.value) })}
                />
                <div className="field-hint">Every authenticated call. 0 disables this limit.</div>
              </div>
              <div className="field">
                <label>Tool calls per minute, per caller</label>
                <input
                  type="number"
                  min={0}
                  value={config.tool_calls_per_minute}
                  onChange={(e) => patch({ tool_calls_per_minute: Number(e.target.value) })}
                />
                <div className="field-hint">
                  Invocation gets its own ceiling because it is the only thing here that reaches a downstream
                  server.
                </div>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Tool calls in one run</label>
                <input
                  type="number"
                  min={0}
                  value={config.tool_calls_per_run}
                  onChange={(e) => patch({ tool_calls_per_run: Number(e.target.value) })}
                />
                <div className="field-hint">
                  The limit that actually catches a loop: a runaway agent stays comfortably inside a per-minute
                  rate while making its fortieth tool call of the same task.
                </div>
              </div>
              <div className="field">
                <label>Tokens per hour, per caller</label>
                <input
                  type="number"
                  min={0}
                  value={config.tokens_per_hour}
                  onChange={(e) => patch({ tokens_per_hour: Number(e.target.value) })}
                />
                <div className="field-hint">
                  Counted on completions the gateway actually paid for. A cache hit adds nothing, because those
                  tokens were never spent.
                </div>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Spend per day, per caller (USD)</label>
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  value={config.spend_per_day_usd}
                  onChange={(e) => patch({ spend_per_day_usd: Number(e.target.value) })}
                />
                <div className="field-hint">
                  List-price estimates from the same table the LLM Caching dashboard uses - not billing data.
                </div>
              </div>
              <div className="field">
                <label>Roles exempt from every limit</label>
                <div className="flex-row" style={{ flexWrap: "wrap", gap: 10, marginTop: 6 }}>
                  {data.roles.map((role) => (
                    <label key={role} className="checkbox-row" style={{ margin: 0 }}>
                      <input
                        type="checkbox"
                        checked={(config.exempt_roles || []).includes(role)}
                        onChange={() => toggleExemptRole(role)}
                      />
                      <span className="cell-mono" style={{ fontSize: 13 }}>{role}</span>
                    </label>
                  ))}
                </div>
                <div className="field-hint">
                  Empty by default. An exemption should be a decision someone made, not one they inherited.
                </div>
              </div>
            </div>
          </div>

          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>4. Per-role and per-agent limits and budgets</h2>
          <LimitOverridesPanel
            config={config}
            roles={data.roles}
            agents={data.agents}
            busy={saving}
            onSave={handleSaveOverrides}
          />

          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>5. Timeouts</h2>
          <div className="panel section-gap" style={{ marginBottom: 24 }}>
            <div className="two-col">
              <div className="field">
                <label>Downstream MCP call (seconds)</label>
                <input
                  type="number"
                  min={1}
                  max={600}
                  value={config.downstream_timeout_seconds}
                  onChange={(e) => patch({ downstream_timeout_seconds: Number(e.target.value) })}
                />
                <div className="field-hint">
                  Applies to tool invocation and catalog ingestion. Without a bound, one unhealthy MCP server holds
                  connections here open indefinitely.
                </div>
              </div>
              <div className="field">
                <label>LLM provider call (seconds)</label>
                <input
                  type="number"
                  min={1}
                  max={900}
                  value={config.llm_timeout_seconds}
                  onChange={(e) => patch({ llm_timeout_seconds: Number(e.target.value) })}
                />
                <div className="field-hint">Applies to a cache miss on /v1/llm/complete.</div>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Whole request (seconds)</label>
                <input
                  type="number"
                  min={5}
                  max={1800}
                  value={config.request_timeout_seconds}
                  onChange={(e) => patch({ request_timeout_seconds: Number(e.target.value) })}
                />
                <div className="field-hint">
                  The two timeouts above bound the calls this gateway makes. This one bounds the call made to it,
                  so a request that stalls somewhere neither of those covers still ends - with a 504 rather than a
                  held-open connection. Set it above the other two.
                </div>
              </div>
              <div className="field" />
            </div>
          </div>

          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>6. Current usage</h2>

          {(data.global_usage.length > 0 || data.role_usage.length > 0) && (
            <div className="card" style={{ marginBottom: 16 }}>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Shared budget</th>
                      {FAMILIES.map((f) => (
                        <th key={f}>{f.replace("_", " ")}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ...(data.global_usage.length ? [{ key: "global", label: "Global - all callers", limits: data.global_usage }] : []),
                      ...data.role_usage.map((r) => ({ key: `role-${r.role}`, label: `Role total - ${r.role}`, limits: r.limits })),
                    ].map((row) => (
                      <tr key={row.key}>
                        <td style={{ fontWeight: 600 }}>{row.label}</td>
                        {FAMILIES.map((family) => {
                          const verdict = row.limits.find((l) => l.family === family);
                          return (
                            <td key={family} style={{ minWidth: 130 }}>
                              {verdict ? <UsageBar verdict={verdict} /> : <span className="cell-muted">no limit</span>}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {data.usage.length === 0 ? (
            <div className="card empty-state">No callers yet - usage appears here once an agent has an API key.</div>
          ) : (
            <div className="card">
              <div className="flex-row" style={{ flexWrap: "wrap", gap: 10, marginBottom: 12, alignItems: "center" }}>
                <input
                  type="search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search callers by agent name, key or role..."
                  aria-label="Search callers"
                  style={{ flex: "1 1 260px", minWidth: 220 }}
                />
                <select value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)} aria-label="Filter by role">
                  <option value="">All roles</option>
                  {data.roles.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                <select
                  value={limitFilter}
                  onChange={(e) => setLimitFilter(e.target.value as typeof limitFilter)}
                  aria-label="Filter by usage"
                >
                  <option value="">All callers</option>
                  <option value="active">Active in the current window</option>
                  <option value="over">Over a limit</option>
                  <option value="custom">With role or agent limits</option>
                </select>
                {filtering && (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    onClick={() => {
                      setSearch("");
                      setRoleFilter("");
                      setLimitFilter("");
                    }}
                  >
                    Clear
                  </button>
                )}
                <span className="cell-muted" style={{ fontSize: 12 }}>
                  {filtering ? `${filteredUsage.length} of ${data.usage.length} callers` : `${data.usage.length} callers`}
                </span>
              </div>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Caller</th>
                      <th>Role</th>
                      {FAMILIES.map((f) => (
                        <th key={f}>{f.replace("_", " ")}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {filteredUsage.map((row) => (
                      <tr key={row.subject}>
                        <td>
                          {row.agent ? (
                            <>
                              <div style={{ fontWeight: 600 }}>{row.agent}</div>
                              <div className="cell-mono cell-muted" style={{ fontSize: 11 }}>{row.subject}</div>
                            </>
                          ) : (
                            <span className="cell-mono">{row.subject}</span>
                          )}
                        </td>
                        <td className="cell-muted">
                          {row.role}
                          {row.exempt && (
                            <div>
                              <span className="badge badge-neutral">exempt</span>
                            </div>
                          )}
                        </td>
                        {FAMILIES.map((family) => {
                          const verdict = row.limits.find((l) => l.family === family);
                          return (
                            <td key={family} style={{ minWidth: 130 }}>
                              {verdict ? (
                                <>
                                  <UsageBar verdict={verdict} />
                                  {(verdict.source === "role" || verdict.source === "agent") && (
                                    <span className="badge badge-info" style={{ marginLeft: 6 }}>
                                      {verdict.source} limit
                                    </span>
                                  )}
                                </>
                              ) : (
                                <span className="cell-muted">no limit</span>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {filteredUsage.length === 0 && <div className="empty-state">No callers match these filters.</div>}
              </div>
              <div className="field-hint" style={{ marginTop: 10 }}>
                Usage is per rolling window: {data.window_seconds.requests}s for requests and tool calls,{" "}
                {data.window_seconds.tokens / 3600}h for tokens, {data.window_seconds.spend / 86400}d for spend.
                A window that has just rolled reads as zero because its counter has expired, not because usage was
                reset. Each caller is shown against its own ceiling - the default, or its role&rsquo;s or agent&rsquo;s
                where one is set.
              </div>
            </div>
          )}

          <div style={{ marginTop: 20 }}>
            <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
              {saving ? "Saving..." : "Save policy"}
            </button>
            <button className="btn btn-secondary" style={{ marginLeft: 10 }} onClick={load} disabled={loading}>
              {loading ? "Refreshing..." : "Refresh usage"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
