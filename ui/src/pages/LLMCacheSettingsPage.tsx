import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { LLMCacheConfig, LLMCompleteResponse, LLMProvider, Role } from "../api/types";

const TTL_PRESETS = [
  { label: "5 minutes", value: 300 },
  { label: "1 hour", value: 3600 },
  { label: "12 hours", value: 43200 },
  { label: "24 hours", value: 86400 },
  { label: "7 days", value: 604800 },
  { label: "Never expire", value: 0 },
];

const SCOPE_HELP: Record<string, string> = {
  global: "One shared cache. Highest hit rate - use when prompts carry no caller-specific data.",
  per_role: "A separate cache per RBAC role. A finance_analyst answer is never replayed to a support_agent.",
  per_subject: "A separate cache per API key. Maximum isolation, lowest hit rate.",
};

const EVICTION_HELP: Record<string, string> = {
  lru: "Evict the entries that have gone longest without a hit.",
  lfu: "Evict the entries with the fewest hits.",
  fifo: "Evict the oldest entries by creation time.",
};

export function LLMCacheSettingsPage() {
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [config, setConfig] = useState<LLMCacheConfig | null>(null);
  const [cachedEntries, setCachedEntries] = useState(0);
  const [lastSweep, setLastSweep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Test console
  const [apiKey, setApiKey] = useState("demo-admin-4c56");
  const [prompt, setPrompt] = useState("Summarise what the Couchbase Agent Operations Manager does in three sentences.");
  const [bypass, setBypass] = useState(false);
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<LLMCompleteResponse | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [p, c, r] = await Promise.all([api.llmProviders(), api.llmConfig(), api.roles()]);
      setProviders(p.providers);
      setConfig(c.config);
      setCachedEntries(c.cached_entries);
      setLastSweep(c.last_sweep_at);
      setRoles(r.roles);
    } catch (e: any) {
      setError(e.message || "Failed to load LLM caching configuration");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function patch(next: Partial<LLMCacheConfig>) {
    setConfig((c) => (c ? { ...c, ...next } : c));
  }

  function selectProvider(p: LLMProvider) {
    patch({ provider: p.id, model: p.default_model });
  }

  async function handleSave() {
    if (!config) return;
    setSaving(true);
    setNote(null);
    setError(null);
    try {
      const res = await api.saveLlmConfig(config);
      setConfig(res.config);
      setNote(
        res.entries_invalidated > 0
          ? `Policy saved - ${res.entries_invalidated} cache entr(ies) invalidated by the change.`
          : "Policy saved."
      );
      await load();
    } catch (e: any) {
      setError(e.message || "Failed to save configuration");
    } finally {
      setSaving(false);
    }
  }

  async function handlePurge(filter: { provider?: string; model?: string }) {
    setNote(null);
    try {
      const res = await api.purgeLlmCache(filter);
      setNote(`Purged ${res.purged} cache entr(ies).`);
      await load();
    } catch (e: any) {
      setError(e.message || "Purge failed");
    }
  }

  async function handleSend(e: React.FormEvent) {
    e.preventDefault();
    setSending(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.llmComplete(apiKey, { prompt, bypass_cache: bypass });
      setResult(res);
      await load();
    } catch (e: any) {
      setError(e.message || "Completion failed");
    } finally {
      setSending(false);
    }
  }

  const activeProvider = providers.find((p) => p.id === config?.provider);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">LLM Providers &amp; Cache Policy</h1>
          <p className="page-subtitle">
            Pick the model agents call through this gateway, and decide when a cached answer stops counting as an
            answer. {cachedEntries} entr(ies) cached
            {lastSweep ? ` - last invalidation sweep ${lastSweep.replace("T", " ").replace("Z", "")}` : ""}.
          </p>
        </div>
        <button className="btn btn-primary" onClick={handleSave} disabled={saving || !config}>
          {saving ? "Saving..." : "Save policy"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}
      {note && <div className="loading-note">{note}</div>}

      {config && (
        <>
          {/* ---------------- provider selection ---------------- */}
          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>1. Choose the LLM</h2>
          <div className="stat-grid" style={{ marginBottom: 18 }}>
            {providers.map((p) => {
              const selected = p.id === config.provider;
              return (
                <div
                  key={p.id}
                  className="stat-card"
                  onClick={() => selectProvider(p)}
                  style={{
                    cursor: "pointer",
                    borderColor: selected ? "var(--teal)" : undefined,
                    boxShadow: selected ? "0 0 0 1px var(--teal) inset" : undefined,
                  }}
                >
                  <div className="flex-between">
                    <div className="stat-label">{p.vendor}</div>
                    <span className={`badge ${p.api_key_configured ? "badge-trusted" : "badge-neutral"}`}>
                      {p.api_key_configured ? "key set" : "stub mode"}
                    </span>
                  </div>
                  <div className="stat-value" style={{ fontSize: 22 }}>
                    {p.label}
                  </div>
                  <div className="stat-hint">
                    {selected
                      ? "Selected - agents route here"
                      : !p.api_key_configured
                      ? `Set ${p.env_key} in .env to call it live`
                      : null}
                  </div>
                </div>
              );
            })}
          </div>

          <div className="panel section-gap" style={{ marginBottom: 24 }}>
            <div className="two-col">
              <div className="field">
                <label>Model</label>
                <select value={config.model} onChange={(e) => patch({ model: e.target.value })}>
                  {(activeProvider?.models || []).map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.id} - ${m.input_usd_per_1m}/M in, ${m.output_usd_per_1m}/M out
                    </option>
                  ))}
                </select>
                <div className="field-hint">
                  Pricing is a list-price estimate used to turn tokens saved into dollars saved on the dashboard.
                </div>
              </div>
              <div className="field">
                <label>Caching</label>
                <select
                  value={config.enabled ? "on" : "off"}
                  onChange={(e) => patch({ enabled: e.target.value === "on" })}
                >
                  <option value="on">Enabled - check the cache before calling the provider</option>
                  <option value="off">Disabled - always call the provider</option>
                </select>
              </div>
            </div>
            <div className="two-col">
              <div className="field">
                <label>Max output tokens</label>
                <input
                  type="number"
                  min={16}
                  max={32000}
                  value={config.max_output_tokens}
                  onChange={(e) => patch({ max_output_tokens: Number(e.target.value) })}
                />
              </div>
              <div className="field">
                <label>Temperature</label>
                <input
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  value={config.temperature}
                  onChange={(e) => patch({ temperature: Number(e.target.value) })}
                />
                <div className="field-hint">
                  Caching is most defensible at 0 - a deterministic prompt should have a deterministic answer.
                </div>
              </div>
            </div>
          </div>

          {/* ---------------- matching ---------------- */}
          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>2. How a prompt matches a cached answer</h2>
          <div className="panel section-gap" style={{ marginBottom: 24 }}>
            <div className="field">
              <div className="checkbox-row">
                <input
                  type="checkbox"
                  id="semantic-enabled"
                  checked={config.semantic_enabled}
                  onChange={(e) => patch({ semantic_enabled: e.target.checked })}
                />
                <label htmlFor="semantic-enabled" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  Semantic matching (Couchbase Vector Search over prompt embeddings)
                </label>
              </div>
              <div className="field-hint">
                Exact matching always runs first - a SHA-256 of the normalized prompt, resolved with a single KV
                get. Semantic matching is the fallback that catches paraphrases, using the same vector index
                pattern the tool catalog uses, pre-filtered to this provider, model, scope and namespace.
              </div>
            </div>
            <div className="two-col">
              <div className="field">
                <label>Similarity threshold ({config.similarity_threshold.toFixed(2)})</label>
                <input
                  type="range"
                  min={0.7}
                  max={0.999}
                  step={0.005}
                  value={config.similarity_threshold}
                  onChange={(e) => patch({ similarity_threshold: Number(e.target.value) })}
                  disabled={!config.semantic_enabled}
                />
                <div className="field-hint">
                  Cosine similarity a candidate must beat to be served. Higher is stricter; below ~0.90 you start
                  returning answers to questions nobody asked.
                </div>
              </div>
              <div className="field">
                <label>Candidates examined per lookup</label>
                <input
                  type="number"
                  min={1}
                  max={200}
                  value={config.semantic_candidates}
                  onChange={(e) => patch({ semantic_candidates: Number(e.target.value) })}
                  disabled={!config.semantic_enabled}
                />
              </div>
            </div>
          </div>

          {/* ---------------- invalidation ---------------- */}
          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>3. Cache invalidation</h2>
          <div className="panel section-gap" style={{ marginBottom: 24 }}>
            <div className="two-col">
              <div className="field">
                <label>Time to live</label>
                <select value={String(config.ttl_seconds)} onChange={(e) => patch({ ttl_seconds: Number(e.target.value) })}>
                  {TTL_PRESETS.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                  {!TTL_PRESETS.some((t) => t.value === config.ttl_seconds) && (
                    <option value={config.ttl_seconds}>{config.ttl_seconds}s (custom)</option>
                  )}
                </select>
                <div className="field-hint">
                  Written as a Couchbase document expiry as well as a policy check, so the cluster reclaims the
                  space even if the sweeper never runs.
                </div>
              </div>
              <div className="field">
                <label>Stale-while-revalidate window (seconds)</label>
                <input
                  type="number"
                  min={0}
                  value={config.stale_while_revalidate_seconds}
                  onChange={(e) => patch({ stale_while_revalidate_seconds: Number(e.target.value) })}
                />
                <div className="field-hint">
                  Grace period after the TTL during which a past-due answer is still served. 0 disables it.
                </div>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Maximum entries</label>
                <input
                  type="number"
                  min={0}
                  value={config.max_entries}
                  onChange={(e) => patch({ max_entries: Number(e.target.value) })}
                />
                <div className="field-hint">0 means unbounded.</div>
              </div>
              <div className="field">
                <label>Eviction policy (when the cache is full)</label>
                <select value={config.eviction_policy} onChange={(e) => patch({ eviction_policy: e.target.value })}>
                  <option value="lru">LRU - least recently used</option>
                  <option value="lfu">LFU - least frequently used</option>
                  <option value="fifo">FIFO - oldest first</option>
                </select>
                <div className="field-hint">{EVICTION_HELP[config.eviction_policy]}</div>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Maximum reuses per entry</label>
                <input
                  type="number"
                  min={0}
                  value={config.max_reuse_hits}
                  onChange={(e) => patch({ max_reuse_hits: Number(e.target.value) })}
                />
                <div className="field-hint">
                  Retire an answer after it has been served this many times, so a hot prompt is periodically
                  re-verified against the provider. 0 means unlimited.
                </div>
              </div>
              <div className="field">
                <label>Sweep interval (minutes)</label>
                <input
                  type="number"
                  min={1}
                  max={1440}
                  value={config.sweep_interval_minutes}
                  onChange={(e) => patch({ sweep_interval_minutes: Number(e.target.value) })}
                />
                <div className="field-hint">
                  How often the background sweeper applies these rules to entries nobody has read.
                </div>
              </div>
            </div>

            <div className="field">
              <label>Invalidate automatically when...</label>
              <div className="checkbox-row">
                <input
                  type="checkbox"
                  id="inv-model"
                  checked={config.invalidate_on_model_change}
                  onChange={(e) => patch({ invalidate_on_model_change: e.target.checked })}
                />
                <label htmlFor="inv-model" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  the selected model changes - answers from the previously selected model are dropped (entries created by an explicit per-request model override are kept)
                </label>
              </div>
              <div className="checkbox-row">
                <input
                  type="checkbox"
                  id="inv-config"
                  checked={config.invalidate_on_config_change}
                  onChange={(e) => patch({ invalidate_on_config_change: e.target.checked })}
                />
                <label htmlFor="inv-config" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  this policy changes in a way that alters what an answer means - namespace, cache scope,
                  similarity threshold or generation settings
                </label>
              </div>
              <div className="checkbox-row">
                <input
                  type="checkbox"
                  id="inv-catalog"
                  checked={config.invalidate_on_catalog_change}
                  onChange={(e) => patch({ invalidate_on_catalog_change: e.target.checked })}
                />
                <label htmlFor="inv-catalog" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  the vetted tool catalog changes - an agent's answer can depend on which tools it was allowed to
                  see
                </label>
              </div>
            </div>

            <div className="two-col">
              <div className="field">
                <label>Cache scope</label>
                <select value={config.cache_scope} onChange={(e) => patch({ cache_scope: e.target.value })}>
                  <option value="global">Global - shared by every caller</option>
                  <option value="per_role">Per RBAC role</option>
                  <option value="per_subject">Per API key</option>
                </select>
                <div className="field-hint">{SCOPE_HELP[config.cache_scope]}</div>
              </div>
              <div className="field">
                <label>Namespace</label>
                <input
                  type="text"
                  value={config.namespace}
                  onChange={(e) => patch({ namespace: e.target.value })}
                  placeholder="default"
                />
                <div className="field-hint">
                  A manual invalidation lever: bump the namespace (e.g. after a prompt-template change) and every
                  older entry stops matching without deleting anything.
                </div>
              </div>
            </div>

            <div className="field">
              <label>Never cache prompts matching (one regular expression per line)</label>
              <textarea
                rows={4}
                value={config.bypass_patterns.join("\n")}
                onChange={(e) => patch({ bypass_patterns: e.target.value.split("\n").filter((l) => l.trim()) })}
                style={{ fontFamily: "var(--font-mono, monospace)" }}
              />
              <div className="field-hint">
                Time-sensitive or caller-specific prompts should never be answered from cache. Matched prompts go
                straight to the provider and are recorded as a bypass.
              </div>
            </div>

            <div className="field">
              <label>Roles that never use the cache</label>
              {roles.map((r) => (
                <div className="checkbox-row" key={r.id}>
                  <input
                    type="checkbox"
                    id={`nocache-${r.id}`}
                    checked={config.no_cache_roles.includes(r.id)}
                    onChange={() =>
                      patch({
                        no_cache_roles: config.no_cache_roles.includes(r.id)
                          ? config.no_cache_roles.filter((x) => x !== r.id)
                          : [...config.no_cache_roles, r.id],
                      })
                    }
                  />
                  <label htmlFor={`nocache-${r.id}`} style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                    {r.id}
                  </label>
                </div>
              ))}
            </div>

            <div className="field">
              <label>Manual invalidation</label>
              <div className="flex-row" style={{ flexWrap: "wrap" }}>
                <button className="btn btn-secondary btn-sm" onClick={() => handlePurge({ model: config.model })}>
                  Purge {config.model}
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => handlePurge({ provider: config.provider })}>
                  Purge all {activeProvider?.label || config.provider} entries
                </button>
                <button className="btn btn-danger-outline btn-sm" onClick={() => handlePurge({})}>
                  Purge everything
                </button>
              </div>
            </div>
          </div>

          {/* ---------------- test console ---------------- */}
          <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>Test Agent API Key</h2>
          <form className="panel section-gap" onSubmit={handleSend}>
            <div className="two-col">
              <div className="field">
                <label>Agent API key</label>
                <input type="text" value={apiKey} onChange={(e) => setApiKey(e.target.value)} required />
                <div className="field-hint">
                  The same key an agent authenticates with for tool discovery - the cache is scoped by whatever
                  role it resolves to.
                </div>
              </div>
              <div className="field">
                <label>Cache behaviour</label>
                <select value={bypass ? "bypass" : "use"} onChange={(e) => setBypass(e.target.value === "bypass")}>
                  <option value="use">Use the cache</option>
                  <option value="bypass">Bypass - force a provider call</option>
                </select>
              </div>
            </div>
            <div className="field">
              <label>Prompt</label>
              <textarea rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} required />
            </div>
            <button className="btn btn-primary" type="submit" disabled={sending}>
              {sending ? "Sending..." : "Send completion"}
            </button>

            {result && (
              <div style={{ marginTop: 18 }}>
                <div className="flex-row" style={{ marginBottom: 10, flexWrap: "wrap" }}>
                  <span
                    className={`badge ${
                      result.cache.status.startsWith("hit") ? "badge-allow" : "badge-info"
                    }`}
                  >
                    {result.cache.status}
                  </span>
                  {result.cache.similarity != null && (
                    <span className="badge badge-neutral">similarity {result.cache.similarity.toFixed(3)}</span>
                  )}
                  <span className="badge badge-neutral">{result.usage.total_tokens} tokens</span>
                  <span className="badge badge-neutral">{result.latency_ms}ms</span>
                  {result.tokens_saved > 0 && (
                    <span className="badge badge-trusted">{result.tokens_saved} tokens saved</span>
                  )}
                  {result.stub && <span className="badge badge-medium">offline stub</span>}
                </div>
                {result.cache.reason && <div className="field-hint">{result.cache.reason}</div>}
                <pre className="json-block">{result.response}</pre>
              </div>
            )}
          </form>
        </>
      )}
    </div>
  );
}
