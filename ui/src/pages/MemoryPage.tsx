import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { MemoryConfig, MemoryEntry, MemoryUsersResponse } from "../api/types";

const TYPE_BADGE: Record<string, string> = {
  profile: "badge-trusted",
  semantic: "badge-info",
  conversational: "badge-neutral",
};

const BAND_BADGE: Record<string, string> = {
  high: "badge-success",
  medium: "badge-medium",
  low: "badge-neutral",
};

function shortTime(ts?: string | null) {
  return ts ? ts.replace("T", " ").replace("Z", "") : "-";
}

function EntryCard({
  entry,
  onEdit,
  onDelete,
  busy,
}: {
  entry: MemoryEntry;
  onEdit: (id: string, content: string) => void;
  onDelete: (id: string) => void;
  busy: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(entry.content);
  const superseded = entry.status === "superseded";

  return (
    <div className="panel section-gap" style={{ marginBottom: 12, opacity: superseded ? 0.62 : 1 }}>
      <div className="flex-between" style={{ marginBottom: 8 }}>
        <div className="flex-row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <span className={`badge ${TYPE_BADGE[entry.memory_type] || "badge-neutral"}`}>{entry.memory_type}</span>
          {entry.importance_band && (
            <span className={`badge ${BAND_BADGE[entry.importance_band]}`}>
              {entry.importance_band} · {(entry.importance ?? 0).toFixed(2)}
            </span>
          )}
          {superseded && <span className="badge badge-untrusted">superseded</span>}
          {entry.consolidation_kind && (
            <span className="badge badge-info">{entry.consolidation_kind}</span>
          )}
          {typeof entry.similarity === "number" && (
            <span className="cell-muted cell-mono" style={{ fontSize: 12 }}>
              similarity {entry.similarity.toFixed(4)}
            </span>
          )}
        </div>
        <div className="flex-row">
          {!superseded && (
            <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => setEditing((v) => !v)}>
              {editing ? "Cancel" : "Edit"}
            </button>
          )}
          <button className="btn btn-danger-outline btn-sm" disabled={busy} onClick={() => onDelete(entry.memory_id)}>
            Delete
          </button>
        </div>
      </div>

      {editing ? (
        <div className="field">
          <textarea rows={4} value={draft} onChange={(e) => setDraft(e.target.value)} />
          <div className="flex-row" style={{ marginTop: 8 }}>
            <button
              className="btn btn-primary btn-sm"
              disabled={busy || !draft.trim()}
              onClick={() => {
                onEdit(entry.memory_id, draft);
                setEditing(false);
              }}
            >
              Save
            </button>
          </div>
          <div className="field-hint">
            Saving re-embeds the entry, so what it means and what it retrieves for stay in step.
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 14, lineHeight: 1.55, marginBottom: 8 }}>{entry.content}</div>
      )}

      <div className="cell-muted cell-mono" style={{ fontSize: 12 }}>
        {shortTime(entry.created_at)}
        {entry.session_id && <> · session {entry.session_id}</>}
        {typeof entry.recall_count === "number" && <> · recalled {entry.recall_count}×</>}
        {!!entry.reinforcement_count && <> · reinforced {entry.reinforcement_count}×</>}
      </div>

      {superseded && entry.superseded_by && (
        <div className="field-hint" style={{ marginTop: 8 }}>
          Replaced {shortTime(entry.superseded_at)} by{" "}
          <span className="cell-mono">{entry.superseded_by}</span>. Kept so the summary that replaced it can be
          checked against what it was built from.
        </div>
      )}
      {!!entry.consolidated_from?.length && (
        <div className="field-hint" style={{ marginTop: 8 }}>
          Consolidated from {entry.consolidated_from.length} earlier entr
          {entry.consolidated_from.length === 1 ? "y" : "ies"}.
        </div>
      )}
      {Object.keys(entry.metadata || {}).length > 0 && (
        <div className="json-block" style={{ fontSize: 12, marginTop: 8 }}>
          {JSON.stringify(entry.metadata)}
        </div>
      )}
    </div>
  );
}

export function MemoryPage() {
  const [data, setData] = useState<MemoryUsersResponse | null>(null);
  const [config, setConfig] = useState<MemoryConfig | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [entries, setEntries] = useState<MemoryEntry[] | null>(null);
  const [typeFilter, setTypeFilter] = useState("");
  const [showSuperseded, setShowSuperseded] = useState(false);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.memoryUsers();
      setData(res);
      setConfig(res.config);
    } catch (e: any) {
      setError(e.message || "Failed to load agent memory");
    }
  }, []);

  const loadEntries = useCallback(
    async (userId: string) => {
      setError(null);
      try {
        const res = await api.memoryEntries({
          user_id: userId,
          memory_type: typeFilter || undefined,
          status: showSuperseded ? undefined : "active",
        });
        setEntries(res.entries);
      } catch (e: any) {
        setError(e.message || "Failed to load entries");
      }
    },
    [typeFilter, showSuperseded]
  );

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (selected) loadEntries(selected);
  }, [selected, loadEntries]);

  async function handleSearch() {
    if (!selected || !query.trim()) return;
    setSearching(true);
    try {
      const res = await api.memorySearch({ user_id: selected, query, memory_type: typeFilter || undefined });
      setEntries(res.entries);
    } catch (e: any) {
      setError(e.message || "Search failed");
    } finally {
      setSearching(false);
    }
  }

  async function handleEdit(memoryId: string, content: string) {
    setBusy(true);
    try {
      await api.editMemory(memoryId, { content });
      if (selected) await loadEntries(selected);
    } catch (e: any) {
      setError(e.message || "Edit failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(memoryId: string) {
    if (!confirm("Delete this memory entry? This cannot be undone.")) return;
    setBusy(true);
    try {
      await api.deleteMemory(memoryId);
      if (selected) await loadEntries(selected);
      await load();
    } catch (e: any) {
      setError(e.message || "Delete failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleForget(userId: string) {
    if (
      !confirm(
        `Erase everything remembered about "${userId}", including superseded entries?\n\n` +
          `This is what a data-subject erasure request needs. It is audited and cannot be undone.`
      )
    )
      return;
    setBusy(true);
    try {
      const res = await api.forgetMemoryUser(userId);
      setNotice(`Erased ${res.entries_removed} entr${res.entries_removed === 1 ? "y" : "ies"} for "${userId}".`);
      setSelected(null);
      setEntries(null);
      await load();
    } catch (e: any) {
      setError(e.message || "Erasure failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleConsolidate() {
    setBusy(true);
    setNotice(null);
    try {
      const res = await api.consolidateMemory(selected || undefined);
      const r = res.report;
      setNotice(
        `Consolidation: merged ${r.duplicates_merged} duplicate(s) across ${r.groups_merged} group(s), ` +
          `rolled up ${r.sessions_rolled_up} session(s), superseded ${r.entries_superseded} entr(ies), ` +
          `rescored ${r.importance_updated}. ${r.llm_calls} model call(s), ${r.cache_hits} served from cache.` +
          (r.errors.length ? ` ${r.errors.length} error(s).` : "")
      );
      await load();
      if (selected) await loadEntries(selected);
    } catch (e: any) {
      setError(e.message || "Consolidation failed");
    } finally {
      setBusy(false);
    }
  }

  function patch(update: Partial<MemoryConfig>) {
    setConfig((c) => (c ? { ...c, ...update } : c));
  }

  async function handleSaveConfig() {
    if (!config) return;
    setBusy(true);
    try {
      const res = await api.saveMemoryConfig(config);
      setConfig(res.config);
      setNotice("Consolidation policy saved.");
    } catch (e: any) {
      setError(e.message || "Could not save the policy");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Agent Memory</h1>
          <div className="page-subtitle page-subtitle-features">
            <div className="feature-item">
              <strong>User &amp; Session Management</strong> &mdash; create users and sessions to organize memory
            </div>
            <div className="feature-item">
              <strong>Memory Storage</strong> &mdash; store prompts and context for improved cost and
              latency
            </div>
            <div className="feature-item">
              <strong>Semantic Search</strong> &mdash; vector-based memory retrieval using FTS indexes
            </div>
            <div className="feature-item">
              <strong>Context Extraction</strong> &mdash; LLM-powered summarization and context generation
            </div>
          </div>
        </div>
        <div className="flex-row">
          <button className="btn btn-secondary" onClick={() => setShowSettings((v) => !v)}>
            {showSettings ? "Hide policy" : "Policy"}
          </button>
          <button className="btn btn-primary" onClick={handleConsolidate} disabled={busy}>
            {busy ? "Working..." : selected ? `Consolidate ${selected}` : "Consolidate now"}
          </button>
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}
      {notice && <div className="helper-banner helper-banner-neutral">{notice}</div>}

      {showSettings && config && (
        <div className="panel section-gap" style={{ marginBottom: 24 }}>
          <div className="two-col">
            <div className="field">
              <label className="checkbox-row">
                <input type="checkbox" checked={config.enabled} onChange={(e) => patch({ enabled: e.target.checked })} />
                <span>Run consolidation</span>
              </label>
              <div className="field-hint">Off means memory accumulates untouched, as it did before.</div>
            </div>
            <div className="field">
              <label>Run every (minutes)</label>
              <input
                type="number"
                min={5}
                value={config.interval_minutes}
                onChange={(e) => patch({ interval_minutes: Number(e.target.value) })}
              />
            </div>
          </div>

          <div className="two-col">
            <div className="field">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={config.dedup_enabled}
                  onChange={(e) => patch({ dedup_enabled: e.target.checked })}
                />
                <span>Merge near-duplicates</span>
              </label>
            </div>
            <div className="field">
              <label>Similarity threshold</label>
              <input
                type="number"
                min={0.9}
                max={0.999}
                step={0.005}
                value={config.dedup_similarity_threshold}
                onChange={(e) => patch({ dedup_similarity_threshold: Number(e.target.value) })}
              />
              <div className="field-hint">
                Deliberately high. A false merge destroys a distinct fact and, unlike a missed merge, the next
                pass cannot undo it.
              </div>
            </div>
          </div>

          <div className="two-col">
            <div className="field">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={config.rollup_enabled}
                  onChange={(e) => patch({ rollup_enabled: e.target.checked })}
                />
                <span>Roll up finished sessions</span>
              </label>
              <div className="field-hint">
                The only part that calls a model &mdash; and it goes through this appliance's own cached, budgeted
                completion gateway, so the cost is visible on the LLM Caching dashboard like any other call.
              </div>
            </div>
            <div className="field">
              <label>Session must be idle for (hours)</label>
              <input
                type="number"
                min={1}
                value={config.rollup_min_idle_hours}
                onChange={(e) => patch({ rollup_min_idle_hours: Number(e.target.value) })}
              />
              <div className="field-hint">
                A session still being written to is never rolled up &mdash; the summary would be wrong immediately.
              </div>
            </div>
          </div>

          <div className="two-col">
            <div className="field">
              <label>Minimum entries to roll up</label>
              <input
                type="number"
                min={2}
                value={config.rollup_min_entries}
                onChange={(e) => patch({ rollup_min_entries: Number(e.target.value) })}
              />
            </div>
            <div className="field">
              <label>Keep superseded entries for (hours)</label>
              <input
                type="number"
                min={1}
                value={config.retain_superseded_hours}
                onChange={(e) => patch({ retain_superseded_hours: Number(e.target.value) })}
              />
              <div className="field-hint">A Couchbase document TTL; they age out on their own afterwards.</div>
            </div>
          </div>

          <div className="two-col">
            <div className="field">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={config.importance_enabled}
                  onChange={(e) => patch({ importance_enabled: e.target.checked })}
                />
                <span>Score importance</span>
              </label>
            </div>
            <div className="field">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={config.track_recall}
                  onChange={(e) => patch({ track_recall: e.target.checked })}
                />
                <span>Count recalls</span>
              </label>
              <div className="field-hint">
                The strongest importance signal there is: what an agent actually reaches for. One extra write per
                recall, off the response path.
              </div>
            </div>
          </div>

          <button className="btn btn-primary" onClick={handleSaveConfig} disabled={busy}>
            Save policy
          </button>
        </div>
      )}

      {data && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">Users remembered</div>
              <div className="stat-value">{data.stats.users ?? data.users.length}</div>
              <div className="stat-hint">{data.stats.total ?? 0} entr(ies) total</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Consolidated</div>
              <div className="stat-value">{data.stats.consolidated ?? 0}</div>
              <div className="stat-hint">Merged or rolled-up entries</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Superseded</div>
              <div className="stat-value">{data.stats.superseded ?? 0}</div>
              <div className="stat-hint">Retained, excluded from recall</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Last pass</div>
              <div className="stat-value" style={{ fontSize: 17 }}>
                {data.last_consolidation_at ? shortTime(data.last_consolidation_at) : "not yet run"}
              </div>
              <div className="stat-hint">
                {data.config.enabled ? `Every ${data.config.interval_minutes}m` : "Consolidation off"}
              </div>
            </div>
          </div>

          <div className="two-col" style={{ marginTop: 22, alignItems: "start" }}>
            <div>
              <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>Users</h2>
              {data.users.length === 0 ? (
                <div className="card empty-state">
                  Nothing remembered yet. Anything an agent writes through /v1/memory appears here.
                </div>
              ) : (
                <div className="card">
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>User</th>
                          <th style={{ textAlign: "right" }}>Entries</th>
                          <th>Last write</th>
                          <th></th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.users.map((u) => (
                          <tr
                            key={u.user_id}
                            style={{ background: selected === u.user_id ? "var(--surface-2, rgba(128,128,128,0.10))" : undefined }}
                          >
                            <td className="cell-mono" style={{ fontWeight: 600 }}>{u.user_id}</td>
                            <td className="cell-mono" style={{ textAlign: "right" }}>
                              {u.total}
                              {!!u.superseded && (
                                <span className="cell-muted" style={{ fontSize: 12 }}> ({u.superseded} sup.)</span>
                              )}
                            </td>
                            <td className="cell-muted cell-mono">{shortTime(u.last_updated_at)}</td>
                            <td>
                              <div className="flex-row">
                                <button className="btn btn-secondary btn-sm" onClick={() => setSelected(u.user_id)}>
                                  View
                                </button>
                                <button
                                  className="btn btn-danger-outline btn-sm"
                                  disabled={busy}
                                  onClick={() => handleForget(u.user_id)}
                                >
                                  Forget
                                </button>
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>

            <div>
              <h2 style={{ fontSize: 16, margin: "0 0 14px 0" }}>
                {selected ? `What the agent remembers about ${selected}` : "Pick a user"}
              </h2>

              {!selected ? (
                <div className="card empty-state">
                  Select a user to see their memories, search them semantically, and correct or remove any of them.
                </div>
              ) : (
                <>
                  <div className="panel section-gap" style={{ marginBottom: 14 }}>
                    <div className="two-col">
                      <div className="field">
                        <label>Type</label>
                        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
                          <option value="">All types</option>
                          {data.memory_types.map((t) => (
                            <option key={t} value={t}>{t}</option>
                          ))}
                        </select>
                      </div>
                      <div className="field">
                        <label className="checkbox-row" style={{ marginTop: 26 }}>
                          <input
                            type="checkbox"
                            checked={showSuperseded}
                            onChange={(e) => setShowSuperseded(e.target.checked)}
                          />
                          <span>Show superseded</span>
                        </label>
                      </div>
                    </div>
                    <div className="field">
                      <label>Semantic search</label>
                      <div className="flex-row">
                        <input
                          type="text"
                          value={query}
                          placeholder="what does this user prefer?"
                          onChange={(e) => setQuery(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
                        />
                        <button className="btn btn-secondary btn-sm" onClick={handleSearch} disabled={searching}>
                          {searching ? "..." : "Search"}
                        </button>
                        <button
                          className="btn btn-secondary btn-sm"
                          onClick={() => {
                            setQuery("");
                            if (selected) loadEntries(selected);
                          }}
                        >
                          Reset
                        </button>
                      </div>
                      <div className="field-hint">The same vector recall an agent gets, without needing its key.</div>
                    </div>
                  </div>

                  {entries === null ? (
                    <div className="card empty-state">Loading...</div>
                  ) : entries.length === 0 ? (
                    <div className="card empty-state">Nothing matches.</div>
                  ) : (
                    entries.map((e) => (
                      <EntryCard
                        key={e.memory_id}
                        entry={e}
                        busy={busy}
                        onEdit={handleEdit}
                        onDelete={handleDelete}
                      />
                    ))
                  )}
                </>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
