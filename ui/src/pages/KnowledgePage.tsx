import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { KnowledgeChunkResult, KnowledgeResponse } from "../api/types";

const API_KEY_STORAGE_KEY = "aom.knowledge.api-key";

// Formats the browser can hand over as text. Anything else is sent as
// base64 and parsed server-side, which is what PDF needs.
const TEXT_EXTENSIONS = [".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".html", ".htm", ".log", ".rst"];

function isTextFile(name: string) {
  const lower = name.toLowerCase();
  return TEXT_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function readAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("Could not read that file"));
    reader.readAsText(file);
  });
}

function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      resolve(result.includes(",") ? result.split(",")[1] : result);
    };
    reader.onerror = () => reject(new Error("Could not read that file"));
    reader.readAsDataURL(file);
  });
}

export function KnowledgePage() {
  const [data, setData] = useState<KnowledgeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [title, setTitle] = useState("");
  const [allowedRoles, setAllowedRoles] = useState<string[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [pastedText, setPastedText] = useState("");
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const [apiKey, setApiKey] = useState(() => localStorage.getItem(API_KEY_STORAGE_KEY) || "");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<KnowledgeChunkResult[] | null>(null);
  const [searchRole, setSearchRole] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await api.knowledge());
    } catch (e: any) {
      setError(e.message || "Failed to load the knowledge base");
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

  function toggleRole(role: string) {
    setAllowedRoles((roles) => (roles.includes(role) ? roles.filter((r) => r !== role) : [...roles, role]));
  }

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    setUploading(true);
    setError(null);
    setNotice(null);
    try {
      let content: string;
      let encoding: "text" | "base64" = "text";
      let filename = "";

      if (file) {
        filename = file.name;
        if (isTextFile(file.name)) {
          content = await readAsText(file);
        } else {
          content = await readAsBase64(file);
          encoding = "base64";
        }
      } else {
        content = pastedText;
        filename = `${title || "pasted"}.txt`;
      }

      if (!content.trim()) throw new Error("Nothing to ingest - pick a file or paste some text");

      const res = await api.ingestKnowledge({
        title: title || file?.name || "Untitled",
        content,
        encoding,
        filename,
        source: file ? file.name : "pasted",
        allowed_roles: allowedRoles,
      });
      setNotice(
        `Ingested "${res.document.title}" - ${res.document.chunk_count} chunk(s) from ` +
          `${res.document.char_count.toLocaleString()} characters, readable by ${res.document.allowed_roles.join(", ")}.`
      );
      setTitle("");
      setPastedText("");
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await load();
    } catch (e: any) {
      setError(e.message || "Ingestion failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete(documentId: string, docTitle: string) {
    if (!confirm(`Remove "${docTitle}" and every chunk belonging to it?`)) return;
    setBusyId(documentId);
    setError(null);
    try {
      await api.deleteKnowledge(documentId);
      await load();
    } catch (e: any) {
      setError(e.message || "Delete failed");
    } finally {
      setBusyId(null);
    }
  }

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    setSearching(true);
    setError(null);
    setResults(null);
    try {
      const res = await api.searchKnowledge(apiKey, query, 5);
      setResults(res.results);
      setSearchRole(res.role);
    } catch (e: any) {
      setError(e.message || "Search failed");
    } finally {
      setSearching(false);
    }
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Knowledge Base</h1>
          <p className="page-subtitle">
            Documents chunked, embedded and stored in the same Couchbase cluster as everything else, retrieved
            with the same RBAC + vector pre-filter the tool catalog uses. A chunk outside the caller&rsquo;s role
            is never a candidate, however well it matches the query.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={load}>
          Refresh
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}
      {notice && <div className="helper-banner helper-banner-neutral">{notice}</div>}

      {data && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">Documents</div>
              <div className="stat-value">{data.documents.length}</div>
              <div className="stat-hint">{data.chunk_count.toLocaleString()} embedded chunk(s)</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Chunk size</div>
              <div className="stat-value" style={{ fontSize: 20 }}>
                {data.chunk_chars}
              </div>
              <div className="stat-hint">{data.chunk_overlap} character overlap</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Upload limit</div>
              <div className="stat-value" style={{ fontSize: 20 }}>
                {data.max_upload_mb} MB
              </div>
              <div className="stat-hint">Every chunk is embedded on CPU</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Formats</div>
              <div className="stat-value" style={{ fontSize: 15 }}>
                {data.supported_extensions.length}
              </div>
              <div className="stat-hint">{data.supported_extensions.join(" ")}</div>
            </div>
          </div>

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Add a document</h2>
          <form className="panel section-gap" onSubmit={handleUpload} style={{ marginBottom: 24 }}>
            <div className="two-col">
              <div className="field">
                <label>Title</label>
                <input
                  type="text"
                  value={title}
                  placeholder="e.g. Refund policy 2026"
                  onChange={(e) => setTitle(e.target.value)}
                />
                <div className="field-hint">Rides along with every chunk, so a chunk that never repeats the subject is still findable by it.</div>
              </div>
              <div className="field">
                <label>File</label>
                <input
                  ref={fileInput}
                  type="file"
                  accept={data.supported_extensions.join(",")}
                  onChange={(e) => setFile(e.target.files?.[0] || null)}
                />
                <div className="field-hint">Or leave this empty and paste text below.</div>
              </div>
            </div>

            {!file && (
              <div className="field">
                <label>Or paste text</label>
                <textarea rows={5} value={pastedText} onChange={(e) => setPastedText(e.target.value)} />
              </div>
            )}

            <div className="field">
              <label>Which roles may retrieve this</label>
              {data.roles.map((role) => (
                <div className="checkbox-row" key={role}>
                  <input
                    type="checkbox"
                    id={`krole-${role}`}
                    checked={allowedRoles.includes(role)}
                    onChange={() => toggleRole(role)}
                  />
                  <label htmlFor={`krole-${role}`} style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                    {role}
                  </label>
                </div>
              ))}
              <div className="field-hint">
                Required. A document no role can read is indexed, embedded and permanently invisible, so the form
                refuses rather than letting you create one.
              </div>
            </div>

            <button className="btn btn-primary" type="submit" disabled={uploading || allowedRoles.length === 0}>
              {uploading ? "Chunking and embedding..." : "Ingest document"}
            </button>
          </form>

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Try retrieval as an agent</h2>
          <form className="panel section-gap" onSubmit={handleSearch} style={{ marginBottom: 24 }}>
            <div className="two-col">
              <div className="field">
                <label>Agent API key</label>
                <input
                  type="password"
                  value={apiKey}
                  placeholder="demo-support-agent-9f21"
                  onChange={(e) => setApiKey(e.target.value)}
                />
                <div className="field-hint">
                  The key decides the role, and the role decides what comes back &mdash; run the same query with
                  two keys to see the pre-filter working.
                </div>
              </div>
              <div className="field">
                <label>Query</label>
                <input
                  type="text"
                  value={query}
                  placeholder="what is our refund window?"
                  onChange={(e) => setQuery(e.target.value)}
                />
              </div>
            </div>
            <button className="btn btn-secondary" type="submit" disabled={searching || !apiKey || !query}>
              {searching ? "Searching..." : "Search"}
            </button>

            {results && (
              <div style={{ marginTop: 16 }}>
                <div className="field-hint" style={{ marginBottom: 10 }}>
                  {results.length} chunk(s) visible to role <span className="cell-mono">{searchRole}</span>.
                </div>
                {results.length === 0 ? (
                  <div className="empty-state">
                    Nothing this role may read matched. That is the pre-filter, not an empty index.
                  </div>
                ) : (
                  results.map((r) => (
                    <div key={r.chunk_id} className="card" style={{ marginBottom: 10 }}>
                      <div className="flex-between" style={{ marginBottom: 6 }}>
                        <span className="cell-mono" style={{ fontWeight: 600 }}>{r.document_title}</span>
                        <span className="cell-muted cell-mono" style={{ fontSize: 12 }}>
                          chunk {r.chunk_index} &middot; score {r.score?.toFixed(4)}
                        </span>
                      </div>
                      <div style={{ fontSize: 14, lineHeight: 1.5 }}>{r.content}</div>
                    </div>
                  ))
                )}
              </div>
            )}
          </form>

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Documents</h2>
          {data.documents.length === 0 ? (
            <div className="card empty-state">
              Nothing ingested yet. Add a document above, or POST to /v1/knowledge.
            </div>
          ) : (
            <div className="card">
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Title</th>
                      <th>Format</th>
                      <th>Readable by</th>
                      <th style={{ textAlign: "right" }}>Chunks</th>
                      <th style={{ textAlign: "right" }}>Characters</th>
                      <th>Added</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.documents.map((d) => (
                      <tr key={d.document_id}>
                        <td>
                          <div style={{ fontWeight: 600 }}>{d.title}</div>
                          <div className="cell-muted cell-mono" style={{ fontSize: 12 }}>{d.source}</div>
                        </td>
                        <td className="cell-muted">{d.format}</td>
                        <td className="cell-muted cell-mono">{(d.allowed_roles || []).join(", ") || "-"}</td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>{d.chunk_count}</td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>
                          {d.char_count.toLocaleString()}
                        </td>
                        <td className="cell-muted cell-mono">
                          {d.created_at?.replace("T", " ").replace("Z", "")}
                        </td>
                        <td>
                          <button
                            className="btn btn-danger-outline btn-sm"
                            disabled={busyId === d.document_id}
                            onClick={() => handleDelete(d.document_id, d.title)}
                          >
                            Delete
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
