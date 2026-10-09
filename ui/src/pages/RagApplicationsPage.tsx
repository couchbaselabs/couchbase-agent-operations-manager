import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { KnowledgeDocument, KnowledgeSet, LLMProvider, RagApp, RagAppsResponse, RagQueryResponse } from "../api/types";

const API_KEY_STORAGE_KEY = "aom.rag.api-key";

const TTL_OPTIONS: Array<{ value: number; label: string }> = [
  { value: 0, label: "Off - always search" },
  { value: 300, label: "5 minutes" },
  { value: 1800, label: "30 minutes" },
  { value: 7200, label: "2 hours" },
  { value: 86400, label: "24 hours" },
];

const DEFAULT_SYSTEM_PROMPT =
  "You are a helpful assistant. Answer using only the numbered context passages. Cite the passages you used " +
  "as [1], [2], ... If the context does not contain the answer, say you don't know rather than guessing.";

type FormState = {
  name: string;
  app_id: string;
  appIdEdited: boolean;
  owner: string;
  description: string;
  allowed_roles: string[];
  set_id: string;
  scopeMode: "all" | "selected";
  document_ids: string[];
  top_k: number;
  min_score: number;
  retrieval_ttl_seconds: number;
  provider: string;
  model: string;
  semantic_cache: boolean;
  system_prompt: string;
  no_answer_text: string;
  issue_key: boolean;
};

const EMPTY_FORM: FormState = {
  name: "",
  app_id: "",
  appIdEdited: false,
  owner: "",
  description: "",
  allowed_roles: [],
  set_id: "default",
  scopeMode: "all",
  document_ids: [],
  top_k: 5,
  min_score: 0,
  retrieval_ttl_seconds: 1800,
  provider: "",
  model: "",
  semantic_cache: true,
  system_prompt: DEFAULT_SYSTEM_PROMPT,
  no_answer_text: "I couldn't find anything in the knowledge base that answers that.",
  issue_key: true,
};

function slugify(name: string) {
  return (
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40)
      .replace(/-+$/g, "") || ""
  );
}

function pct(part: number, whole: number) {
  return whole > 0 ? `${Math.round((part / whole) * 100)}%` : "-";
}

function money(v: number) {
  return v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`;
}

function ttlLabel(seconds: number) {
  return TTL_OPTIONS.find((o) => o.value === seconds)?.label || `${seconds}s`;
}

function cacheBadge(status: string | undefined) {
  if (!status) return "badge badge-neutral";
  if (status.startsWith("hit")) return "badge badge-success";
  if (status === "miss") return "badge badge-info";
  return "badge badge-neutral";
}

export function RagApplicationsPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [data, setData] = useState<RagAppsResponse | null>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [sets, setSets] = useState<KnowledgeSet[]>([]);
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [issued, setIssued] = useState<{ appId: string; key: string; note: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const [testAppId, setTestAppId] = useState("");
  const [apiKey, setApiKey] = useState(() => {
    try {
      return localStorage.getItem(API_KEY_STORAGE_KEY) || "";
    } catch {
      return "";
    }
  });
  const [question, setQuestion] = useState("");
  const [bypass, setBypass] = useState(false);
  const [asking, setAsking] = useState(false);
  const [result, setResult] = useState<RagQueryResponse | null>(null);
  const [askError, setAskError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [apps, kb, prov] = await Promise.all([
        api.ragApps(),
        api.knowledge().catch(() => null),
        api.llmProviders().catch(() => null),
      ]);
      setData(apps);
      setDocuments(kb?.documents || []);
      setSets(kb?.sets || []);
      setProviders(prov?.providers || []);
      setTestAppId((current) => current || apps.apps.find((a) => a.enabled)?.app_id || "");
    } catch (e: any) {
      setError(e.message || "Failed to load RAG applications");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    try {
      localStorage.setItem(API_KEY_STORAGE_KEY, apiKey);
    } catch {
      // A blocked localStorage just means the key is not remembered.
    }
  }, [apiKey]);

  const totals = useMemo(() => {
    const t = { queries: 0, llmHits: 0, retrievals: 0, retrievalHits: 0, saved: 0 };
    for (const a of data?.apps || []) {
      const s = a.activity;
      if (!s) continue;
      t.queries += s.queries;
      t.llmHits += s.llm_hits;
      t.retrievals += s.retrievals;
      t.retrievalHits += s.retrieval_hits;
      t.saved += s.cost_saved_usd;
    }
    return t;
  }, [data]);

  const providerModels = providers.find((p) => p.id === form.provider)?.models || [];
  const docsForRoles = documents.filter(
    (d) =>
      (d.set_id || "default") === form.set_id &&
      (form.allowed_roles.length === 0 || d.allowed_roles.some((r) => form.allowed_roles.includes(r)))
  );

  function update<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function toggleIn(key: "allowed_roles" | "document_ids", value: string) {
    setForm((f) => ({
      ...f,
      [key]: f[key].includes(value) ? f[key].filter((v) => v !== value) : [...f[key], value],
    }));
  }

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const res = await api.registerRagApp(
        {
          name: form.name,
          app_id: form.app_id || slugify(form.name),
          owner: form.owner,
          description: form.description,
          allowed_roles: form.allowed_roles,
          set_id: form.set_id,
          document_ids: form.scopeMode === "selected" ? form.document_ids : [],
          top_k: form.top_k,
          min_score: form.min_score,
          retrieval_ttl_seconds: form.retrieval_ttl_seconds,
          provider: form.provider || null,
          model: form.provider ? form.model || null : null,
          semantic_cache: form.semantic_cache,
          system_prompt: form.system_prompt,
          no_answer_text: form.no_answer_text,
        },
        form.issue_key
      );
      if (res.api_key) {
        setIssued({ appId: res.app.app_id, key: res.api_key, note: res.notice || "" });
        setApiKey(res.api_key);
      } else {
        setNotice(`Registered '${res.app.app_id}'. Query it with any key whose role is allowed.`);
      }
      setTestAppId(res.app.app_id);
      setForm(EMPTY_FORM);
      setShowForm(false);
      await load();
    } catch (e: any) {
      setError(e.message || "Registration failed");
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled(app: RagApp) {
    setBusyId(app.app_id);
    try {
      await api.updateRagApp(app.app_id, { enabled: !app.enabled });
      await load();
    } catch (e: any) {
      setError(e.message || "Update failed");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(app: RagApp) {
    if (confirmDelete !== app.app_id) {
      setConfirmDelete(app.app_id);
      return;
    }
    setBusyId(app.app_id);
    try {
      const res = await api.deleteRagApp(app.app_id);
      setNotice(
        `Deleted '${app.app_id}'.` + (res.agent_revoked ? " Its Agent Identity was revoked, so its key no longer works." : "")
      );
      setConfirmDelete(null);
      if (testAppId === app.app_id) setTestAppId("");
      await load();
    } catch (e: any) {
      setError(e.message || "Delete failed");
    } finally {
      setBusyId(null);
    }
  }

  async function handleAsk(e: React.FormEvent) {
    e.preventDefault();
    setAsking(true);
    setAskError(null);
    setResult(null);
    try {
      setResult(await api.ragQuery(apiKey, testAppId, question, bypass));
    } catch (e: any) {
      setAskError(e.message || "Query failed");
    } finally {
      setAsking(false);
    }
  }

  const sdkSnippet = `from aom_sdk import AOMClient

client = AOMClient("https://aom.example.com:8090", api_key="aom_...")  # this app's key

res = client.rag_query("${testAppId || "your-app-id"}", "What is our refund window?")
print(res["answer"])                                   # cites sources as [1], [2] ...
for s in res["sources"]:
    print(s["n"], s["document_title"], s["score"])
print(res["retrieval"]["cache"], res["llm"]["cache"]["status"])  # e.g. hit, hit_semantic`;

  const curlSnippet = `curl -X POST https://aom.example.com:8090/v1/agent/rag/${testAppId || "your-app-id"}/query \\
  -H "Authorization: Bearer aom_..." \\
  -H "Content-Type: application/json" \\
  -d '{"question": "What is our refund window?"}'`;

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">RAG Applications</h1>
          <p className="page-subtitle">
            Register retrieval-augmented applications and serve them straight from the Knowledge Base: role-filtered
            retrieval with Context Caching in front of it, and answers generated through the same governed, cached
            LLM path as every other model call.
          </p>
        </div>
        <div className="flex-row">
          <button className="btn btn-secondary" onClick={load}>
            Refresh
          </button>
          {isAdmin && (
            <button className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
              {showForm ? "Cancel" : "Register application"}
            </button>
          )}
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}
      {notice && <div className="helper-banner helper-banner-neutral">{notice}</div>}

      <div className="helper-banner helper-banner-neutral">
        <div className="helper-banner-heading">How a RAG query is served</div>
        <ol className="rag-flow">
          <li>
            <strong>Authorize</strong> - the caller's API key decides its role; the role must be allowed by the app.
          </li>
          <li>
            <strong>Retrieve</strong> - a <Link to="/context-cache">Context Cache</Link> hit returns the chunks this
            role already retrieved for the same question; a miss runs vector search over the{" "}
            <Link to="/knowledge">Knowledge Base</Link>, filtered by the caller's role and narrowed by the app's
            document scope. Adding or deleting a document invalidates every cached retrieval at once.
          </li>
          <li>
            <strong>Generate</strong> - the app's prompt plus the numbered passages go through the{" "}
            <Link to="/llm-caching">LLM Cache</Link>. Paraphrase (semantic) matching only compares questions that
            retrieved exactly the same passages, so a cached answer is never reused across different context.
          </li>
          <li>
            <strong>Govern</strong> - guardrails, rate limits and budgets apply, and the whole query is one trace on{" "}
            <Link to="/traces">Traces</Link>.
          </li>
        </ol>
      </div>

      {issued && (
        <div className="helper-banner" style={{ marginBottom: 18 }}>
          <div className="helper-banner-heading">API key for '{issued.appId}' - save it now</div>
          <div className="json-block" style={{ fontSize: 13, margin: "8px 0" }}>{issued.key}</div>
          <div style={{ marginBottom: 10 }}>
            {issued.note} It's also listed under <Link to="/settings/agents">Agent Identities</Link>, where it can be
            rotated or revoked.
          </div>
          <div className="flex-row">
            <button
              className="btn btn-secondary btn-sm"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(issued.key);
                  setCopied(true);
                } catch {
                  setCopied(false);
                }
              }}
            >
              {copied ? "Copied" : "Copy"}
            </button>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => {
                setIssued(null);
                setCopied(false);
              }}
            >
              Done
            </button>
          </div>
        </div>
      )}

      {data && (
        <div className="stat-grid">
          <div className="stat-card">
            <div className="stat-label">Applications</div>
            <div className="stat-value">{data.apps.length}</div>
            <div className="stat-hint">{data.apps.filter((a) => a.enabled).length} enabled</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Recent queries</div>
            <div className="stat-value">{totals.queries.toLocaleString()}</div>
            <div className="stat-hint">From the latest {data.activity_window_events.toLocaleString()} cache events</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Retrieval cache hit rate</div>
            <div className="stat-value">{pct(totals.retrievalHits, totals.retrievals)}</div>
            <div className="stat-hint">Vector searches skipped by the Context Cache</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Answer cache hit rate</div>
            <div className="stat-value">{pct(totals.llmHits, totals.queries)}</div>
            <div className="stat-hint">{money(totals.saved)} in model spend avoided</div>
          </div>
        </div>
      )}

      {showForm && isAdmin && data && (
        <form className="panel section-gap" onSubmit={handleRegister} style={{ marginBottom: 24 }}>
          <div className="card-title" style={{ marginBottom: 14 }}>
            Register a RAG application
          </div>

          <div className="two-col">
            <div className="field">
              <label>Name</label>
              <input
                type="text"
                value={form.name}
                placeholder="e.g. Support FAQ"
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    name: e.target.value,
                    app_id: f.appIdEdited ? f.app_id : slugify(e.target.value),
                  }))
                }
              />
            </div>
            <div className="field">
              <label>Application ID</label>
              <input
                type="text"
                value={form.app_id}
                placeholder="support-faq"
                onChange={(e) => setForm((f) => ({ ...f, app_id: e.target.value, appIdEdited: true }))}
              />
              <div className="field-hint">
                Lowercase letters, digits and dashes. Part of the endpoint:{" "}
                <span className="cell-mono">/v1/agent/rag/{form.app_id || "app-id"}/query</span>
              </div>
            </div>
          </div>

          <div className="two-col">
            <div className="field">
              <label>Owner</label>
              <input
                type="text"
                value={form.owner}
                placeholder="e.g. Customer Support Engineering"
                onChange={(e) => update("owner", e.target.value)}
              />
            </div>
            <div className="field">
              <label>Description</label>
              <input
                type="text"
                value={form.description}
                placeholder="What this application answers, and for whom"
                onChange={(e) => update("description", e.target.value)}
              />
            </div>
          </div>

          <div className="field">
            <label>Which roles may query it</label>
            {data.roles.map((role) => (
              <div className="checkbox-row" key={role}>
                <input
                  type="checkbox"
                  id={`ragrole-${role}`}
                  checked={form.allowed_roles.includes(role)}
                  onChange={() => toggleIn("allowed_roles", role)}
                />
                <label htmlFor={`ragrole-${role}`} style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  {role}
                </label>
              </div>
            ))}
            <div className="field-hint">
              Retrieval is always filtered by the caller's own role, so allowing a role here never lets it read a
              document it couldn't already. The first role ticked is the role of the application's own API key.
            </div>
          </div>

          <div className="field">
            <label>Knowledge set</label>
            <select
              value={form.set_id}
              onChange={(e) => setForm((f) => ({ ...f, set_id: e.target.value, document_ids: [] }))}
            >
              {(sets.length ? sets : [{ set_id: "default", name: "Default", model_label: "AOM default model" } as KnowledgeSet]).map(
                (s) => (
                  <option key={s.set_id} value={s.set_id}>
                    {s.name} - {s.model_label}
                    {s.document_count !== undefined ? ` (${s.document_count} documents)` : ""}
                  </option>
                )
              )}
            </select>
            <div className="field-hint">
              Questions are embedded with this set&rsquo;s model and searched in its own vector index. Create sets
              with other embedding models on <Link to="/knowledge">Knowledge Base</Link>.
            </div>
          </div>

          <div className="field">
            <label>Knowledge scope</label>
            <div className="checkbox-row">
              <input
                type="radio"
                id="scope-all"
                checked={form.scopeMode === "all"}
                onChange={() => update("scopeMode", "all")}
              />
              <label htmlFor="scope-all" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                Every document in the set the caller's role can read
              </label>
            </div>
            <div className="checkbox-row">
              <input
                type="radio"
                id="scope-selected"
                checked={form.scopeMode === "selected"}
                onChange={() => update("scopeMode", "selected")}
              />
              <label htmlFor="scope-selected" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                Only selected documents
              </label>
            </div>
            {form.scopeMode === "selected" && (
              <div className="rag-doc-list">
                {docsForRoles.length === 0 ? (
                  <div className="field-hint">
                    No Knowledge Base documents are readable by the selected roles yet - add some on{" "}
                    <Link to="/knowledge">Knowledge Base</Link>.
                  </div>
                ) : (
                  docsForRoles.map((d) => (
                    <div className="checkbox-row" key={d.document_id}>
                      <input
                        type="checkbox"
                        id={`ragdoc-${d.document_id}`}
                        checked={form.document_ids.includes(d.document_id)}
                        onChange={() => toggleIn("document_ids", d.document_id)}
                      />
                      <label
                        htmlFor={`ragdoc-${d.document_id}`}
                        style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}
                      >
                        {d.title}{" "}
                        <span className="cell-muted cell-mono" style={{ fontSize: 11.5 }}>
                          {d.allowed_roles.join(", ")} · {d.chunk_count} chunks
                        </span>
                      </label>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>

          <div className="three-col-fields">
            <div className="field">
              <label>Passages per answer (top K)</label>
              <input
                type="number"
                min={1}
                max={10}
                value={form.top_k}
                onChange={(e) => update("top_k", Number(e.target.value))}
              />
            </div>
            <div className="field">
              <label>Minimum relevance score</label>
              <input
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={form.min_score}
                onChange={(e) => update("min_score", Number(e.target.value))}
              />
              <div className="field-hint">0 keeps every match; raise it to drop weak passages.</div>
            </div>
            <div className="field">
              <label>Retrieval cache (Context Cache)</label>
              <select
                value={form.retrieval_ttl_seconds}
                onChange={(e) => update("retrieval_ttl_seconds", Number(e.target.value))}
              >
                {TTL_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              <div className="field-hint">Knowledge Base changes invalidate it immediately either way.</div>
            </div>
          </div>

          <div className="three-col-fields">
            <div className="field">
              <label>LLM provider</label>
              <select
                value={form.provider}
                onChange={(e) => setForm((f) => ({ ...f, provider: e.target.value, model: "" }))}
              >
                <option value="">Appliance default (Providers &amp; Policy)</option>
                {providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                    {p.api_key_configured ? "" : " (no key configured)"}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Model</label>
              <select value={form.model} disabled={!form.provider} onChange={(e) => update("model", e.target.value)}>
                <option value="">Provider default</option>
                {providerModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Answer cache (LLM Cache)</label>
              <div className="checkbox-row" style={{ marginTop: 6 }}>
                <input
                  type="checkbox"
                  id="rag-semantic"
                  checked={form.semantic_cache}
                  onChange={(e) => update("semantic_cache", e.target.checked)}
                />
                <label htmlFor="rag-semantic" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  Match paraphrased questions (semantic)
                </label>
              </div>
              <div className="field-hint">Off = exact-match only. Either way, only within identical context.</div>
            </div>
          </div>

          <div className="field">
            <label>Instructions (system prompt)</label>
            <textarea rows={3} value={form.system_prompt} onChange={(e) => update("system_prompt", e.target.value)} />
          </div>
          <div className="field">
            <label>Answer when nothing relevant is found</label>
            <input type="text" value={form.no_answer_text} onChange={(e) => update("no_answer_text", e.target.value)} />
            <div className="field-hint">Returned without calling the model at all.</div>
          </div>

          <div className="checkbox-row" style={{ marginBottom: 14 }}>
            <input
              type="checkbox"
              id="rag-issue-key"
              checked={form.issue_key}
              onChange={(e) => update("issue_key", e.target.checked)}
            />
            <label htmlFor="rag-issue-key" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
              Issue this application its own API key (an Agent Identity), so its traffic is attributed, rate-limited
              and revocable on its own
            </label>
          </div>

          <button
            className="btn btn-primary"
            type="submit"
            disabled={
              saving ||
              !form.name.trim() ||
              form.allowed_roles.length === 0 ||
              (form.scopeMode === "selected" && form.document_ids.length === 0)
            }
          >
            {saving ? "Registering..." : "Register application"}
          </button>
        </form>
      )}

      <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Applications</h2>
      {data && data.apps.length === 0 ? (
        <div className="card empty-state">
          No RAG applications yet.{" "}
          {isAdmin ? "Register one above to serve it from the Knowledge Base." : "An admin can register one."}
        </div>
      ) : (
        data && (
          <div className="card">
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Application</th>
                    <th>Roles</th>
                    <th>Knowledge scope</th>
                    <th>Retrieval cache</th>
                    <th>Model</th>
                    <th style={{ textAlign: "right" }}>Queries</th>
                    <th style={{ textAlign: "right" }}>Retrieval hits</th>
                    <th style={{ textAlign: "right" }}>Answer hits</th>
                    <th style={{ textAlign: "right" }}>Saved</th>
                    <th>Status</th>
                    {isAdmin && <th></th>}
                  </tr>
                </thead>
                <tbody>
                  {data.apps.map((a) => {
                    const s = a.activity;
                    return (
                      <tr key={a.app_id}>
                        <td>
                          <div style={{ fontWeight: 600 }}>{a.name}</div>
                          <div className="cell-muted cell-mono" style={{ fontSize: 12 }}>
                            {a.app_id}
                          </div>
                          {a.description && (
                            <div className="cell-muted" style={{ fontSize: 12 }}>
                              {a.description}
                            </div>
                          )}
                        </td>
                        <td className="cell-muted cell-mono">{a.allowed_roles.join(", ")}</td>
                        <td className="cell-muted">
                          <div style={{ color: "var(--text)" }}>
                            {sets.find((x) => x.set_id === (a.set_id || "default"))?.name || a.set_id || "Default"}
                          </div>
                          {a.document_ids.length === 0
                            ? "All role-readable documents"
                            : `${a.document_ids.length} document${a.document_ids.length === 1 ? "" : "s"}`}
                          <div className="cell-mono" style={{ fontSize: 11.5 }}>
                            top {a.top_k}
                            {a.min_score ? ` · score ≥ ${a.min_score}` : ""}
                          </div>
                        </td>
                        <td className="cell-muted">{ttlLabel(a.retrieval_ttl_seconds)}</td>
                        <td className="cell-muted cell-mono" style={{ fontSize: 12 }}>
                          {a.provider ? `${a.provider}${a.model ? `:${a.model}` : ""}` : "appliance default"}
                          <div>{a.semantic_cache ? "semantic + exact" : "exact only"}</div>
                        </td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>
                          {s?.queries ?? 0}
                        </td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>
                          {s ? pct(s.retrieval_hits, s.retrievals) : "-"}
                        </td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>
                          {s ? pct(s.llm_hits, s.queries) : "-"}
                        </td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>
                          {s ? money(s.cost_saved_usd) : "-"}
                        </td>
                        <td>
                          <span className={a.enabled ? "badge badge-success" : "badge badge-neutral"}>
                            {a.enabled ? "enabled" : "disabled"}
                          </span>
                        </td>
                        {isAdmin && (
                          <td style={{ whiteSpace: "nowrap" }}>
                            <div className="flex-row" style={{ gap: 6 }}>
                              <button
                                className="btn btn-secondary btn-sm"
                                disabled={busyId === a.app_id}
                                onClick={() => toggleEnabled(a)}
                              >
                                {a.enabled ? "Disable" : "Enable"}
                              </button>
                              <button
                                className="btn btn-danger-outline btn-sm"
                                disabled={busyId === a.app_id}
                                onClick={() => handleDelete(a)}
                                onBlur={() => setConfirmDelete((c) => (c === a.app_id ? null : c))}
                              >
                                {confirmDelete === a.app_id ? "Confirm delete" : "Delete"}
                              </button>
                            </div>
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )
      )}

      {data && data.apps.length > 0 && (
        <>
          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Try an application</h2>
          <form className="panel section-gap" onSubmit={handleAsk} style={{ marginBottom: 24 }}>
            <div className="three-col-fields">
              <div className="field">
                <label>Application</label>
                <select value={testAppId} onChange={(e) => setTestAppId(e.target.value)}>
                  <option value="">Choose...</option>
                  {data.apps.map((a) => (
                    <option key={a.app_id} value={a.app_id} disabled={!a.enabled}>
                      {a.name} ({a.app_id}){a.enabled ? "" : " - disabled"}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>API key</label>
                <input
                  type="password"
                  value={apiKey}
                  placeholder="aom_..."
                  onChange={(e) => setApiKey(e.target.value)}
                />
                <div className="field-hint">The app's own key, or any key whose role the app allows.</div>
              </div>
              <div className="field">
                <label>Options</label>
                <div className="checkbox-row" style={{ marginTop: 6 }}>
                  <input type="checkbox" id="rag-bypass" checked={bypass} onChange={(e) => setBypass(e.target.checked)} />
                  <label htmlFor="rag-bypass" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                    Bypass both caches for this question
                  </label>
                </div>
              </div>
            </div>
            <div className="field">
              <label>Question</label>
              <input
                type="text"
                value={question}
                placeholder="What is our refund window?"
                onChange={(e) => setQuestion(e.target.value)}
              />
            </div>
            <button className="btn btn-primary" type="submit" disabled={asking || !testAppId || !apiKey || !question.trim()}>
              {asking ? "Retrieving and generating..." : "Ask"}
            </button>
            <span className="cell-muted" style={{ marginLeft: 12, fontSize: 12.5 }}>
              Ask twice, or reword the question, to watch the caches take over.
            </span>

            {askError && (
              <div className="error-note" style={{ marginTop: 14 }}>
                {askError}
              </div>
            )}

            {result && (
              <div style={{ marginTop: 18 }}>
                <div className="flex-row" style={{ flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
                  <span className={cacheBadge(result.retrieval.cache)}>retrieval: {result.retrieval.cache}</span>
                  <span className={cacheBadge(result.llm?.cache.status)}>
                    answer: {result.llm ? result.llm.cache.status.replace("_", " ") : "no context - model not called"}
                    {result.llm?.cache.similarity != null ? ` (${result.llm.cache.similarity.toFixed(3)})` : ""}
                  </span>
                  <span className="badge badge-neutral">{result.latency_ms} ms</span>
                  {result.llm && (
                    <span className="badge badge-neutral">
                      {result.llm.cache.status.startsWith("hit")
                        ? `${money(result.llm.cost_saved_usd)} saved`
                        : `${money(result.llm.cost_usd)} spent`}
                    </span>
                  )}
                  <span className="badge badge-neutral">role: {result.role}</span>
                  {result.llm?.stub && <span className="badge badge-medium">offline stub - no provider key</span>}
                </div>
                <div className="card" style={{ marginBottom: 12 }}>
                  <div style={{ fontSize: 14.5, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{result.answer}</div>
                </div>
                {result.sources.length > 0 && (
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>#</th>
                          <th>Source</th>
                          <th>Passage</th>
                          <th style={{ textAlign: "right" }}>Score</th>
                        </tr>
                      </thead>
                      <tbody>
                        {result.sources.map((src) => (
                          <tr key={src.n}>
                            <td className="cell-mono">[{src.n}]</td>
                            <td>
                              <div style={{ fontWeight: 600 }}>{src.document_title}</div>
                              <div className="cell-muted cell-mono" style={{ fontSize: 11.5 }}>
                                part {src.chunk_index + 1}
                              </div>
                            </td>
                            <td className="cell-muted" style={{ fontSize: 12.5 }}>
                              {src.preview}
                              {src.preview.length >= 240 ? "…" : ""}
                            </td>
                            <td className="cell-mono" style={{ textAlign: "right" }}>
                              {src.score != null ? src.score.toFixed(3) : "-"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <div className="cell-muted" style={{ fontSize: 12.5, marginTop: 10 }}>
                  Trace <span className="cell-mono">{result.trace.trace_id}</span> - open{" "}
                  <Link to="/traces">Traces</Link> to see retrieval, cache and model spans together.
                </div>
              </div>
            )}
          </form>

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Call it from your application</h2>
          <div className="two-col" style={{ marginBottom: 24 }}>
            <div className="panel">
              <div style={{ fontWeight: 600, marginBottom: 8 }}>Python (AOM SDK)</div>
              <pre className="json-block agent-code-block" style={{ margin: 0 }}>{sdkSnippet}</pre>
            </div>
            <div className="panel">
              <div style={{ fontWeight: 600, marginBottom: 8 }}>HTTP</div>
              <pre className="json-block agent-code-block" style={{ margin: 0 }}>{curlSnippet}</pre>
              <div className="cell-muted" style={{ fontSize: 12.5, marginTop: 10 }}>
                Use your operations-manager URL (port 8090 by default), or the dashboard URL on Helm installs. The
                response carries <span className="cell-mono">answer</span>,{" "}
                <span className="cell-mono">sources</span> numbered to match the citations, and the cache outcome
                of both steps.
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
