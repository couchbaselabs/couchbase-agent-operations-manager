import { useState } from "react";
import type { GovernanceAgentRef, GovernanceConfig, LimitFields, LimitOverride } from "../../api/types";

/**
 * Per-role and per-agent limits and budgets.
 *
 * An entry names a role or one agent identity and sets only the ceilings it
 * cares about; everything it leaves blank keeps following the defaults. The
 * gateway resolves each ceiling field by field - the agent's entry, else its
 * role's, else the default (governance.resolve). A role entry can also carry
 * a shared total: one budget every caller in the role draws from together.
 */

export const LIMIT_FIELD_LABELS: Record<keyof LimitFields, string> = {
  requests_per_minute: "Requests / minute",
  tool_calls_per_minute: "Tool calls / minute",
  tool_calls_per_run: "Tool calls in one run",
  tokens_per_hour: "Tokens / hour",
  spend_per_day_usd: "Spend / day (USD)",
};

const CALLER_FIELDS = Object.keys(LIMIT_FIELD_LABELS) as Array<keyof LimitFields>;
const SHARED_FIELDS = CALLER_FIELDS.filter((f) => f !== "tool_calls_per_run");

type Draft = {
  override_id?: string;
  matchType: "role" | "agent";
  matchValue: string;
  enabled: boolean;
  // "" = not set (inherit / no shared total)
  limits: Record<string, string>;
  shared: Record<string, string>;
};

const EMPTY: Draft = { matchType: "role", matchValue: "", enabled: true, limits: {}, shared: {} };

function toDraft(o: LimitOverride): Draft {
  const text = (obj: Partial<LimitFields>) =>
    Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, String(v)])) as Record<string, string>;
  return {
    override_id: o.override_id,
    matchType: o.match.type,
    matchValue: o.match.value,
    enabled: o.enabled,
    limits: text(o.limits),
    shared: text(o.shared_limits),
  };
}

function numbers(values: Record<string, string>, fields: string[]): Partial<LimitFields> {
  const out: Record<string, number> = {};
  for (const f of fields) {
    const raw = (values[f] ?? "").trim();
    if (raw !== "" && Number.isFinite(Number(raw)) && Number(raw) >= 0) out[f] = Number(raw);
  }
  return out as Partial<LimitFields>;
}

function formatLimit(field: string, value: number): string {
  if (field === "spend_per_day_usd") return value === 0 ? "no limit" : `$${value.toLocaleString()}`;
  return value === 0 ? "no limit" : value.toLocaleString();
}

function summary(limits: Partial<LimitFields>): string {
  const parts = Object.entries(limits).map(
    ([field, value]) => `${LIMIT_FIELD_LABELS[field as keyof LimitFields]}: ${formatLimit(field, Number(value))}`,
  );
  return parts.length ? parts.join(" · ") : "-";
}

export function LimitOverridesPanel({
  config,
  roles,
  agents,
  busy,
  onSave,
}: {
  config: GovernanceConfig;
  roles: string[];
  agents: GovernanceAgentRef[];
  busy: boolean;
  // Persists the whole policy with this override list; false when the
  // save failed (the page shows why), so the form stays open.
  onSave: (overrides: LimitOverride[], note: string) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const overrides = config.overrides || [];

  const agentName = (id: string) => agents.find((a) => a.agent_id === id)?.name || id;
  const targetLabel = (o: LimitOverride) => (o.match.type === "role" ? o.match.value : agentName(o.match.value));

  function patch(next: Partial<Draft>) {
    setDraft((d) => (d ? { ...d, ...next } : d));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!draft) return;
    const limits = numbers(draft.limits, CALLER_FIELDS);
    const shared_limits = draft.matchType === "role" ? numbers(draft.shared, SHARED_FIELDS) : {};
    // A shared total of 0 means "none", so it is simply not stored.
    for (const f of Object.keys(shared_limits) as Array<keyof LimitFields>) if (!shared_limits[f]) delete shared_limits[f];
    if (!draft.matchValue) {
      setFormError(draft.matchType === "role" ? "Choose a role." : "Choose an agent.");
      return;
    }
    if (!Object.keys(limits).length && !Object.keys(shared_limits).length) {
      setFormError("Set at least one limit - an entry with every field blank changes nothing.");
      return;
    }
    const duplicate = overrides.find(
      (o) => o.override_id !== draft.override_id && o.match.type === draft.matchType && o.match.value === draft.matchValue,
    );
    if (duplicate) {
      setFormError(`${targetLabel(duplicate)} already has an entry - edit that one instead.`);
      return;
    }
    const entry: LimitOverride = {
      override_id: draft.override_id || "",
      name: `${draft.matchType} ${draft.matchType === "role" ? draft.matchValue : agentName(draft.matchValue)}`,
      match: { type: draft.matchType, value: draft.matchValue },
      enabled: draft.enabled,
      limits,
      shared_limits,
    };
    const next = draft.override_id
      ? overrides.map((o) => (o.override_id === draft.override_id ? entry : o))
      : [...overrides, entry];
    setFormError(null);
    if (await onSave(next, `Limits for ${entry.name.replace(/^(role|agent) /, "")} saved.`)) setDraft(null);
  }

  async function handleRemove(o: LimitOverride) {
    if (!window.confirm(`Remove the limits set for ${targetLabel(o)}? It goes back to the defaults.`)) return;
    const removed = await onSave(
      overrides.filter((x) => x.override_id !== o.override_id),
      `Limits for ${targetLabel(o)} removed.`,
    );
    if (removed && draft?.override_id === o.override_id) setDraft(null);
  }

  async function handleToggle(o: LimitOverride) {
    await onSave(
      overrides.map((x) => (x.override_id === o.override_id ? { ...x, enabled: !x.enabled } : x)),
      `Limits for ${targetLabel(o)} ${o.enabled ? "paused" : "resumed"}.`,
    );
  }

  const numberInput = (
    values: Record<string, string>,
    field: keyof LimitFields,
    onChange: (v: string) => void,
    placeholder: string,
  ) => (
    <div className="field" key={field}>
      <label>{LIMIT_FIELD_LABELS[field]}</label>
      <input
        type="number"
        min={0}
        step={field === "spend_per_day_usd" ? "0.01" : 1}
        value={values[field] ?? ""}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );

  return (
    <div className="panel section-gap" style={{ marginBottom: 24 }}>
      <div className="flex-between" style={{ marginBottom: overrides.length || draft ? 14 : 0 }}>
        <div className="field-hint" style={{ margin: 0, maxWidth: 760 }}>
          Give a role or a single agent its own ceilings. Each one is resolved separately: an agent&rsquo;s entry wins
          over its role&rsquo;s, which wins over the defaults above, and anything left blank keeps following the
          level below it. A role can also have a shared total that all of its callers draw from together.
        </div>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => {
            setFormError(null);
            setDraft(draft ? null : { ...EMPTY });
          }}
        >
          {draft ? "Cancel" : "Add role or agent limits"}
        </button>
      </div>

      {draft && (
        <form onSubmit={handleSubmit} style={{ marginBottom: overrides.length ? 18 : 0 }}>
          {formError && <div className="error-note">{formError}</div>}
          <div className="two-col">
            <div className="field">
              <label>Applies to</label>
              <select
                value={draft.matchType}
                disabled={!!draft.override_id}
                onChange={(e) => patch({ matchType: e.target.value as "role" | "agent", matchValue: "", shared: {} })}
              >
                <option value="role">A role - every caller with that role</option>
                <option value="agent">One agent identity</option>
              </select>
            </div>
            <div className="field">
              <label>{draft.matchType === "role" ? "Role" : "Agent"}</label>
              <select
                value={draft.matchValue}
                disabled={!!draft.override_id}
                onChange={(e) => patch({ matchValue: e.target.value })}
              >
                <option value="">Choose...</option>
                {draft.matchType === "role"
                  ? roles.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))
                  : agents.map((a) => (
                      <option key={a.agent_id} value={a.agent_id}>
                        {a.name || a.agent_id} ({a.role})
                      </option>
                    ))}
                {draft.matchValue &&
                  draft.matchType === "agent" &&
                  !agents.some((a) => a.agent_id === draft.matchValue) && (
                    <option value={draft.matchValue}>{draft.matchValue} (no longer active)</option>
                  )}
              </select>
              {draft.matchType === "agent" && agents.length === 0 && (
                <div className="field-hint">No agent identities yet - create one under Settings, Agent Identities.</div>
              )}
            </div>
          </div>

          <div style={{ fontWeight: 600, margin: "4px 0 8px 0" }}>
            {draft.matchType === "role" ? "Limits for each caller in this role" : "Limits for this agent"}
          </div>
          <div className="three-col-fields">
            {CALLER_FIELDS.map((f) =>
              numberInput(
                draft.limits,
                f,
                (v) => patch({ limits: { ...draft.limits, [f]: v } }),
                `inherit (${formatLimit(f, Number(config[f]))})`,
              ),
            )}
          </div>
          <div className="field-hint" style={{ marginBottom: 14 }}>
            Blank inherits. 0 removes that ceiling for {draft.matchType === "role" ? "these callers" : "this agent"} -
            the global limits and any role total still apply.
          </div>

          {draft.matchType === "role" && (
            <>
              <div style={{ fontWeight: 600, margin: "4px 0 8px 0" }}>Shared total for the whole role</div>
              <div className="three-col-fields">
                {SHARED_FIELDS.map((f) =>
                  numberInput(draft.shared, f, (v) => patch({ shared: { ...draft.shared, [f]: v } }), "none"),
                )}
              </div>
              <div className="field-hint" style={{ marginBottom: 14 }}>
                One budget that every caller in the role counts against together - when it is used up, all of them
                are refused until the window rolls. Blank or 0 means no shared total.
              </div>
            </>
          )}

          <button className="btn btn-primary" type="submit" disabled={busy}>
            {busy ? "Saving..." : draft.override_id ? "Save changes" : "Add limits"}
          </button>
        </form>
      )}

      {overrides.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Applies to</th>
                <th>Per-caller limits</th>
                <th>Shared role total</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {overrides.map((o) => (
                <tr key={o.override_id}>
                  <td>
                    <span className={`badge ${o.match.type === "role" ? "badge-info" : "badge-neutral"}`}>
                      {o.match.type}
                    </span>{" "}
                    <span style={{ fontWeight: 600 }}>{targetLabel(o)}</span>
                    {o.match.type === "agent" && (
                      <div className="cell-mono cell-muted" style={{ fontSize: 11 }}>
                        {o.match.value}
                      </div>
                    )}
                  </td>
                  <td className="cell-muted">{summary(o.limits)}</td>
                  <td className="cell-muted">{o.match.type === "role" ? summary(o.shared_limits) : "-"}</td>
                  <td>
                    <span className={`badge ${o.enabled ? "badge-success" : "badge-neutral"}`}>
                      {o.enabled ? "active" : "paused"}
                    </span>
                  </td>
                  <td>
                    <div className="flex-row">
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={() => {
                          setFormError(null);
                          setDraft(toDraft(o));
                        }}
                      >
                        Edit
                      </button>
                      <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => handleToggle(o)}>
                        {o.enabled ? "Pause" : "Resume"}
                      </button>
                      <button type="button" className="btn btn-danger-outline btn-sm" disabled={busy} onClick={() => handleRemove(o)}>
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
