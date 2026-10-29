import { useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import type { AgentIdentity, LLMCacheConfig, LLMCachePolicy, LLMProvider, Role } from "../../api/types";

/**
 * Per-role / per-agent overrides of the global LLM cache policy.
 *
 * Each override names a role or an agent identity and sets only the fields
 * it cares about - a different provider/model, caching off, a shorter TTL.
 * Everything it leaves blank keeps following the global policy above it on
 * the page. The gateway resolves an agent-identity match before a role
 * match, then falls back to the global config (llm_cache.resolve_policy).
 */

const TTL_PRESETS = [
  { label: "5 minutes", value: 300 },
  { label: "1 hour", value: 3600 },
  { label: "12 hours", value: 43200 },
  { label: "24 hours", value: 86400 },
  { label: "7 days", value: 604800 },
  { label: "Never expire", value: 0 },
];

type Draft = {
  policy_id?: string;
  name: string;
  matchType: "role" | "agent";
  matchValue: string;
  enabled: boolean;
  // "" / undefined = inherit
  provider: string;
  model: string;
  caching: "" | "on" | "off";
  semantic: "" | "on" | "off";
  similarity_threshold: string;
  ttl_seconds: string;
  cache_scope: string;
  namespace: string;
  max_output_tokens: string;
  temperature: string;
};

const EMPTY: Draft = {
  name: "",
  matchType: "role",
  matchValue: "",
  enabled: true,
  provider: "",
  model: "",
  caching: "",
  semantic: "",
  similarity_threshold: "",
  ttl_seconds: "",
  cache_scope: "",
  namespace: "",
  max_output_tokens: "",
  temperature: "",
};

function toDraft(p: LLMCachePolicy): Draft {
  const c = p.config || {};
  return {
    policy_id: p.policy_id,
    name: p.name,
    matchType: p.match.type,
    matchValue: p.match.value,
    enabled: p.enabled,
    provider: c.provider ?? "",
    model: c.model ?? "",
    caching: c.enabled === undefined ? "" : c.enabled ? "on" : "off",
    semantic: c.semantic_enabled === undefined ? "" : c.semantic_enabled ? "on" : "off",
    similarity_threshold: c.similarity_threshold === undefined ? "" : String(c.similarity_threshold),
    ttl_seconds: c.ttl_seconds === undefined ? "" : String(c.ttl_seconds),
    cache_scope: c.cache_scope ?? "",
    namespace: c.namespace ?? "",
    max_output_tokens: c.max_output_tokens === undefined ? "" : String(c.max_output_tokens),
    temperature: c.temperature === undefined ? "" : String(c.temperature),
  };
}

function fromDraft(d: Draft): LLMCachePolicy {
  const config: LLMCachePolicy["config"] = {};
  if (d.provider) {
    config.provider = d.provider;
    if (d.model) config.model = d.model;
  }
  if (d.caching) config.enabled = d.caching === "on";
  if (d.semantic) config.semantic_enabled = d.semantic === "on";
  if (d.similarity_threshold !== "") config.similarity_threshold = Number(d.similarity_threshold);
  if (d.ttl_seconds !== "") config.ttl_seconds = Number(d.ttl_seconds);
  if (d.cache_scope) config.cache_scope = d.cache_scope;
  if (d.namespace.trim()) config.namespace = d.namespace.trim();
  if (d.max_output_tokens !== "") config.max_output_tokens = Number(d.max_output_tokens);
  if (d.temperature !== "") config.temperature = Number(d.temperature);
  return {
    policy_id: d.policy_id || "",
    name: d.name.trim() || `${d.matchType} ${d.matchValue}`,
    match: { type: d.matchType, value: d.matchValue },
    enabled: d.enabled,
    config,
  };
}

function describeOverrides(p: LLMCachePolicy, providers: LLMProvider[]): string {
  const c = p.config || {};
  const parts: string[] = [];
  if (c.provider) {
    const label = providers.find((x) => x.id === c.provider)?.label || c.provider;
    parts.push(c.model ? `${label} / ${c.model}` : label);
  }
  if (c.enabled !== undefined) parts.push(c.enabled ? "caching on" : "caching off");
  if (c.semantic_enabled !== undefined) parts.push(c.semantic_enabled ? "semantic on" : "exact-match only");
  if (c.similarity_threshold !== undefined) parts.push(`threshold ${c.similarity_threshold}`);
  if (c.ttl_seconds !== undefined) {
    const preset = TTL_PRESETS.find((t) => t.value === c.ttl_seconds);
    parts.push(`TTL ${preset ? preset.label.toLowerCase() : `${c.ttl_seconds}s`}`);
  }
  if (c.cache_scope) parts.push(`scope ${c.cache_scope}`);
  if (c.namespace) parts.push(`namespace ${c.namespace}`);
  if (c.max_output_tokens !== undefined) parts.push(`max ${c.max_output_tokens} tokens`);
  if (c.temperature !== undefined) parts.push(`temp ${c.temperature}`);
  return parts.length ? parts.join(" · ") : "inherits everything (no-op)";
}

export function PolicyOverridesPanel({
  globalConfig,
  providers,
  roles,
  cacheScopes,
  onChanged,
}: {
  globalConfig: LLMCacheConfig;
  providers: LLMProvider[];
  roles: Role[];
  cacheScopes: string[];
  onChanged?: () => void;
}) {
  const [policies, setPolicies] = useState<LLMCachePolicy[]>([]);
  const [agents, setAgents] = useState<AgentIdentity[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function load() {
    try {
      const [p, a] = await Promise.all([api.llmPolicies(), api.agents().catch(() => null)]);
      setPolicies(p.policies);
      if (a) setAgents(a.agents);
    } catch (e: any) {
      setError(e.message || "Failed to load policy overrides");
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function persist(next: LLMCachePolicy[], message: string) {
    setSaving(true);
    setError(null);
    setNote(null);
    try {
      const res = await api.saveLlmPolicies(next);
      setPolicies(res.policies);
      setNote(
        res.entries_invalidated > 0
          ? `${message} ${res.entries_invalidated} cache entr(ies) written under the old model were invalidated.`
          : message
      );
      setDraft(null);
      onChanged?.();
    } catch (e: any) {
      setError(e.message || "Failed to save policy overrides");
    } finally {
      setSaving(false);
    }
  }

  function submitDraft() {
    if (!draft) return;
    if (!draft.matchValue) {
      setError(`Pick the ${draft.matchType === "role" ? "role" : "agent identity"} this override applies to.`);
      return;
    }
    const pol = fromDraft(draft);
    const next = draft.policy_id
      ? policies.map((p) => (p.policy_id === draft.policy_id ? pol : p))
      : [...policies, pol];
    persist(next, draft.policy_id ? "Override updated." : "Override added.");
  }

  function remove(id: string) {
    persist(
      policies.filter((p) => p.policy_id !== id),
      "Override removed - those callers follow the global policy again."
    );
  }

  function toggle(id: string) {
    persist(
      policies.map((p) => (p.policy_id === id ? { ...p, enabled: !p.enabled } : p)),
      "Override updated."
    );
  }

  function move(id: string, dir: -1 | 1) {
    const i = policies.findIndex((p) => p.policy_id === id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= policies.length) return;
    const next = [...policies];
    [next[i], next[j]] = [next[j], next[i]];
    persist(next, "Order updated.");
  }

  const draftProvider = useMemo(
    () => providers.find((p) => p.id === (draft?.provider || "")),
    [providers, draft?.provider]
  );
  const globalProviderLabel = providers.find((p) => p.id === globalConfig.provider)?.label || globalConfig.provider;

  function patch(next: Partial<Draft>) {
    setDraft((d) => (d ? { ...d, ...next } : d));
  }

  return (
    <>
      <div className="flex-between" style={{ marginBottom: 14 }}>
        <h2 style={{ fontSize: 16, margin: 0 }}>4. Per-agent policies</h2>
        {!draft && (
          <button className="btn btn-secondary" onClick={() => setDraft({ ...EMPTY })} disabled={saving}>
            Add override
          </button>
        )}
      </div>

      <div className="panel section-gap" style={{ marginBottom: 24 }}>
        <p className="cell-muted" style={{ marginTop: 0 }}>
          Sections 1-3 are the <strong>global policy</strong> every agent gets by default (currently{" "}
          <span className="cell-mono">
            {globalProviderLabel} / {globalConfig.model}
          </span>
          ). An override here changes only the fields it sets for one role or one agent identity - a different
          provider or model, caching off, a shorter TTL - and inherits the rest. An agent-identity match wins over
          a role match; within the same type the first enabled override in this list applies.
        </p>

        {error && <div className="error-note">{error}</div>}
        {note && <div className="loading-note">{note}</div>}

        {policies.length === 0 && !draft ? (
          <div className="empty-state">No overrides - every agent follows the global policy.</div>
        ) : (
          <div className="table-wrap" style={{ marginBottom: draft ? 18 : 0 }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Applies to</th>
                  <th>Overrides</th>
                  <th>Status</th>
                  <th style={{ width: 230 }} />
                </tr>
              </thead>
              <tbody>
                {policies.map((p, i) => (
                  <tr key={p.policy_id} style={{ opacity: p.enabled ? 1 : 0.55 }}>
                    <td style={{ fontWeight: 600 }}>{p.name}</td>
                    <td>
                      <span className="badge badge-neutral" style={{ marginRight: 6 }}>
                        {p.match.type}
                      </span>
                      <span className="cell-mono">
                        {p.match.type === "agent"
                          ? agents.find((a) => a.agent_id === p.match.value)?.name || p.match.value
                          : p.match.value}
                      </span>
                    </td>
                    <td className="cell-muted">{describeOverrides(p, providers)}</td>
                    <td>
                      <span className={`badge ${p.enabled ? "badge-trusted" : "badge-neutral"}`}>
                        {p.enabled ? "active" : "disabled"}
                      </span>
                    </td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <button className="btn btn-secondary btn-sm" onClick={() => move(p.policy_id, -1)} disabled={saving || i === 0} title="Move up">
                        ↑
                      </button>{" "}
                      <button
                        className="btn btn-secondary btn-sm"
                        onClick={() => move(p.policy_id, 1)}
                        disabled={saving || i === policies.length - 1}
                        title="Move down"
                      >
                        ↓
                      </button>{" "}
                      <button className="btn btn-secondary btn-sm" onClick={() => setDraft(toDraft(p))} disabled={saving}>
                        Edit
                      </button>{" "}
                      <button className="btn btn-secondary btn-sm" onClick={() => toggle(p.policy_id)} disabled={saving}>
                        {p.enabled ? "Disable" : "Enable"}
                      </button>{" "}
                      <button className="btn btn-danger-outline btn-sm" onClick={() => remove(p.policy_id)} disabled={saving}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {draft && (
          <div className="panel" style={{ background: "var(--bg-elevated)" }}>
            <h3 style={{ fontSize: 14, margin: "0 0 12px 0" }}>{draft.policy_id ? "Edit override" : "New override"}</h3>
            <div className="two-col">
              <div className="field">
                <label>Name</label>
                <input
                  value={draft.name}
                  placeholder={draft.matchValue ? `${draft.matchType} ${draft.matchValue}` : "e.g. Finance on GPT"}
                  onChange={(e) => patch({ name: e.target.value })}
                />
              </div>
              <div className="field">
                <label>Applies to</label>
                <div className="flex-row" style={{ gap: 8 }}>
                  <select
                    value={draft.matchType}
                    onChange={(e) => patch({ matchType: e.target.value as "role" | "agent", matchValue: "" })}
                    style={{ flex: "0 0 auto" }}
                  >
                    <option value="role">Role</option>
                    <option value="agent">Agent identity</option>
                  </select>
                  <select value={draft.matchValue} onChange={(e) => patch({ matchValue: e.target.value })} style={{ flex: 1 }}>
                    <option value="">{draft.matchType === "role" ? "Choose a role..." : "Choose an agent..."}</option>
                    {draft.matchType === "role"
                      ? roles.map((r) => (
                          <option key={r.id} value={r.id}>
                            {r.id}
                          </option>
                        ))
                      : agents.map((a) => (
                          <option key={a.agent_id} value={a.agent_id}>
                            {a.name} ({a.role})
                          </option>
                        ))}
                  </select>
                </div>
                <div className="field-hint">
                  {draft.matchType === "agent"
                    ? "Agents are issued under Settings → Agent Identities. An agent match beats a role match."
                    : "Every key issued for this role, unless an agent-identity override claims it first."}
                </div>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Provider</label>
                <select
                  value={draft.provider}
                  onChange={(e) => {
                    const p = providers.find((x) => x.id === e.target.value);
                    patch({ provider: e.target.value, model: p ? p.default_model : "" });
                  }}
                >
                  <option value="">Inherit ({globalProviderLabel})</option>
                  {providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label}
                      {p.api_key_configured ? "" : " (stub mode - no key set)"}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>Model</label>
                <select value={draft.model} onChange={(e) => patch({ model: e.target.value })} disabled={!draft.provider}>
                  {!draft.provider && <option value="">Inherit ({globalConfig.model})</option>}
                  {(draftProvider?.models || []).map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.id} - ${m.input_usd_per_1m}/M in, ${m.output_usd_per_1m}/M out
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Caching</label>
                <select value={draft.caching} onChange={(e) => patch({ caching: e.target.value as Draft["caching"] })}>
                  <option value="">Inherit ({globalConfig.enabled ? "enabled" : "disabled"})</option>
                  <option value="on">Enabled</option>
                  <option value="off">Disabled - always call the provider</option>
                </select>
              </div>
              <div className="field">
                <label>Semantic matching</label>
                <select value={draft.semantic} onChange={(e) => patch({ semantic: e.target.value as Draft["semantic"] })}>
                  <option value="">Inherit ({globalConfig.semantic_enabled ? "on" : "exact-match only"})</option>
                  <option value="on">On</option>
                  <option value="off">Exact-match only</option>
                </select>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Similarity threshold</label>
                <input
                  type="number"
                  min={0.5}
                  max={0.9999}
                  step={0.01}
                  value={draft.similarity_threshold}
                  placeholder={`Inherit (${globalConfig.similarity_threshold})`}
                  onChange={(e) => patch({ similarity_threshold: e.target.value })}
                />
              </div>
              <div className="field">
                <label>TTL</label>
                <select value={draft.ttl_seconds} onChange={(e) => patch({ ttl_seconds: e.target.value })}>
                  <option value="">
                    Inherit ({TTL_PRESETS.find((t) => t.value === globalConfig.ttl_seconds)?.label || `${globalConfig.ttl_seconds}s`})
                  </option>
                  {TTL_PRESETS.map((t) => (
                    <option key={t.value} value={String(t.value)}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Cache scope</label>
                <select value={draft.cache_scope} onChange={(e) => patch({ cache_scope: e.target.value })}>
                  <option value="">Inherit ({globalConfig.cache_scope})</option>
                  {cacheScopes.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>Namespace</label>
                <input
                  value={draft.namespace}
                  placeholder={`Inherit (${globalConfig.namespace})`}
                  onChange={(e) => patch({ namespace: e.target.value })}
                />
                <div className="field-hint">A separate namespace keeps this caller's answers out of everyone else's cache.</div>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Max output tokens</label>
                <input
                  type="number"
                  min={16}
                  max={32000}
                  value={draft.max_output_tokens}
                  placeholder={`Inherit (${globalConfig.max_output_tokens})`}
                  onChange={(e) => patch({ max_output_tokens: e.target.value })}
                />
              </div>
              <div className="field">
                <label>Temperature</label>
                <input
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  value={draft.temperature}
                  placeholder={`Inherit (${globalConfig.temperature})`}
                  onChange={(e) => patch({ temperature: e.target.value })}
                />
              </div>
            </div>

            <div className="flex-row" style={{ gap: 8, justifyContent: "flex-end" }}>
              <button className="btn btn-secondary" onClick={() => setDraft(null)} disabled={saving}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={submitDraft} disabled={saving}>
                {saving ? "Saving..." : draft.policy_id ? "Save override" : "Add override"}
              </button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
