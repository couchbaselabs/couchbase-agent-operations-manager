import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { EmbeddingModelOption, KnowledgeChunkResult, KnowledgeResponse } from "../api/types";

const NEW_SET = "__new__";

// Expiry choices for a new document, and for changing one later.
const EXPIRY_OPTIONS: Array<{ value: string; label: string; seconds?: number }> = [
  { value: "never", label: "Never" },
  { value: "1d", label: "1 day", seconds: 86400 },
  { value: "7d", label: "7 days", seconds: 7 * 86400 },
  { value: "30d", label: "30 days", seconds: 30 * 86400 },
  { value: "90d", label: "90 days", seconds: 90 * 86400 },
  { value: "365d", label: "1 year", seconds: 365 * 86400 },
  { value: "date", label: "On a date..." },
];

// How to produce a file this page can read, per warehouse. Shown next to
// the dump upload so nobody has to look up export syntax mid-task.
const EXPORT_HELP: Record<string, { title: string; code: string; note: string }> = {
  snowflake: {
    title: "Export from Snowflake",
    code: `COPY INTO @~/orders_export/
  FROM sales.public.orders
  FILE_FORMAT = (TYPE = CSV COMPRESSION = GZIP FIELD_OPTIONALLY_ENCLOSED_BY = '"')
  HEADER = TRUE;
-- then, from SnowSQL:
GET @~/orders_export/ file:///tmp/orders_export/;`,
    note: "Upload the .csv.gz (or zip several unload files together). Without HEADER = TRUE there's no header row - set Header row to No and list the columns. TYPE = PARQUET works too.",
  },
  databricks: {
    title: "Export from Databricks",
    code: `(spark.table("main.sales.orders")
   .coalesce(1)
   .write.mode("overwrite")
   .parquet("/Volumes/main/default/exports/orders"))`,
    note: "Download the part-*.parquet file, or zip the whole output folder - _SUCCESS and .crc files are skipped. Use .option('header', True).csv(...) for CSV. Don't zip a Delta table folder: its data files can include removed rows.",
  },
  bigquery: {
    title: "Export from BigQuery",
    code: `EXPORT DATA OPTIONS (
  uri = 'gs://my-bucket/orders/*.parquet',
  format = 'PARQUET',
  overwrite = true
) AS SELECT * FROM shop.orders;
-- or, from a shell:
-- bq extract --destination_format=NEWLINE_DELIMITED_JSON --compression=GZIP shop.orders gs://my-bucket/orders-*.json.gz`,
    note: "Download the files from Cloud Storage; zip multiple shards together. Parquet, Avro, newline-delimited JSON and CSV (gzip or not) are all accepted.",
  },
  other: {
    title: "Other sources",
    code: "",
    note: "Any CSV/TSV, newline-delimited JSON, Parquet or Avro export works - gzip-compressed or zipped.",
  },
};

function expiryLabel(expiresAt?: string | null) {
  if (!expiresAt) return "Never";
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms <= 0) return "Expired - deleting";
  const days = ms / 86400000;
  const when = expiresAt.replace("T", " ").replace(":00Z", " UTC");
  if (days < 1) return `in ${Math.max(1, Math.round(ms / 3600000))}h · ${when}`;
  return `in ${Math.round(days)}d · ${when}`;
}
const IMPORT_MODEL = "__import__";

function modelOptionLabel(m: EmbeddingModelOption) {
  if (m.custom && m.status !== "ready") {
    return `${m.label} · ${m.status === "pending" ? "downloading and verifying..." : "failed verification"}`;
  }
  const bits = [`${m.dims} dims`];
  if (m.size) bits.push(m.size);
  if (m.multilingual) bits.push("multilingual");
  return `${m.label} · ${bits.join(" · ")}${m.is_default ? " · AOM default" : ""}${
    m.available ? "" : ` · needs ${m.requires}`
  }`;
}

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
  const [addMode, setAddMode] = useState<"document" | "dump">("document");
  const [dumpSource, setDumpSource] = useState("snowflake");
  const [tableName, setTableName] = useState("");
  const [headerMode, setHeaderMode] = useState<"auto" | "yes" | "no">("auto");
  const [columnNames, setColumnNames] = useState("");
  const [expiryChoice, setExpiryChoice] = useState("never");
  const [expiryDate, setExpiryDate] = useState("");
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const [models, setModels] = useState<EmbeddingModelOption[]>([]);
  const [setId, setSetId] = useState("");
  const [newSetName, setNewSetName] = useState("");
  const [newSetModel, setNewSetModel] = useState("");
  const [newSetDescription, setNewSetDescription] = useState("");
  const [creatingSet, setCreatingSet] = useState(false);
  const [searchSetId, setSearchSetId] = useState("default");

  const [importKind, setImportKind] = useState<"huggingface" | "openai_compatible">("huggingface");
  const [importName, setImportName] = useState("");
  const [importLabel, setImportLabel] = useState("");
  const [importBaseUrl, setImportBaseUrl] = useState("");
  const [importApiKey, setImportApiKey] = useState("");
  const [importQueryPrefix, setImportQueryPrefix] = useState("");
  const [importDocPrefix, setImportDocPrefix] = useState("");
  const [importing, setImporting] = useState(false);
  const [awaitingModel, setAwaitingModel] = useState<string | null>(null);

  const [apiKey, setApiKey] = useState(() => localStorage.getItem(API_KEY_STORAGE_KEY) || "");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<KnowledgeChunkResult[] | null>(null);
  const [searchRole, setSearchRole] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [kb, catalog] = await Promise.all([api.knowledge(), api.embeddingModels().catch(() => null)]);
      setData(kb);
      if (catalog) setModels(catalog.models);
      setSetId((current) => (current && current !== NEW_SET ? current : current || kb.default_set_id));
    } catch (e: any) {
      setError(e.message || "Failed to load the knowledge base");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // An imported Hugging Face model downloads and verifies in the background;
  // poll until it's ready (or failed), then select it for the new set.
  const pendingImports = models.some((m) => m.custom && m.status === "pending");
  useEffect(() => {
    if (!pendingImports) return;
    const timer = window.setInterval(async () => {
      try {
        const catalog = await api.embeddingModels();
        setModels(catalog.models);
        const waited = awaitingModel && catalog.models.find((m) => m.id === awaitingModel);
        if (waited && waited.status === "ready") {
          setNewSetModel(waited.id);
          setNotice(`Imported model "${waited.label}" is ready (${waited.dims} dimensions).`);
          setAwaitingModel(null);
        } else if (waited && waited.status === "error") {
          setError(`Imported model "${waited.label}" failed verification: ${waited.error}`);
          setAwaitingModel(null);
        }
      } catch {
        // Next tick tries again.
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [pendingImports, awaitingModel]);

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

      const expiryOpt = EXPIRY_OPTIONS.find((o) => o.value === expiryChoice);
      const expiry: { ttl_seconds?: number; expires_at?: string } =
        expiryChoice === "date" ? { expires_at: expiryDate } : expiryOpt?.seconds ? { ttl_seconds: expiryOpt.seconds } : {};
      if (expiryChoice === "date" && !expiryDate) throw new Error("Pick the date this document expires");

      if (addMode === "dump") {
        if (!file) throw new Error("Pick the export file to import");
        // Always sent as bytes: dumps are often gzip, zip, Parquet or Avro.
        const res = await api.ingestKnowledge({
          title: title || tableName || file.name,
          content: await readAsBase64(file),
          encoding: "base64",
          filename: file.name,
          source: file.name,
          allowed_roles: allowedRoles,
          set_id: setId,
          ...expiry,
          dump: {
            source: dumpSource,
            table_name: tableName || undefined,
            header: headerMode === "auto" ? null : headerMode === "yes",
            column_names:
              headerMode === "no" && columnNames.trim()
                ? columnNames.split(",").map((c) => c.trim()).filter(Boolean)
                : undefined,
          },
        });
        const dump = res.document.dump;
        setNotice(
          `Imported ${dump?.source_label} table "${dump?.table_name}" - ${dump?.rows_indexed.toLocaleString()} of ` +
            `${dump?.total_rows.toLocaleString()} row(s) across ${dump?.column_count} column(s) into ` +
            `${res.document.chunk_count} chunk(s)` +
            (res.document.expires_at ? `, expiring ${res.document.expires_at.replace("T", " ").replace("Z", " UTC")}` : "") +
            "." +
            (dump?.notes?.length ? " " + dump.notes.join(" ") : "")
        );
        setTitle("");
        setTableName("");
        setColumnNames("");
        setFile(null);
        if (fileInput.current) fileInput.current.value = "";
        await load();
        return;
      }

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
        set_id: setId,
        ...expiry,
      });
      setNotice(
        `Ingested "${res.document.title}" - ${res.document.chunk_count} chunk(s) from ` +
          `${res.document.char_count.toLocaleString()} characters, readable by ${res.document.allowed_roles.join(", ")}, ` +
          `embedded with ${res.document.embedding_model} into set "${res.document.set_id}".`
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

  async function handleChangeExpiry(documentId: string, choice: string) {
    const opt = EXPIRY_OPTIONS.find((o) => o.value === choice);
    let expiry: { ttl_seconds?: number; expires_at?: string } = {};
    if (choice === "date") {
      const date = prompt("Expire on which date? (YYYY-MM-DD, UTC)");
      if (!date) return;
      expiry = { expires_at: date };
    } else if (opt?.seconds) {
      expiry = { ttl_seconds: opt.seconds };
    }
    setBusyId(`exp:${documentId}`);
    setError(null);
    try {
      await api.setKnowledgeExpiry(documentId, expiry);
      await load();
    } catch (e: any) {
      setError(e.message || "Could not change the expiry");
    } finally {
      setBusyId(null);
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

  async function handleCreateSet() {
    setCreatingSet(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.createKnowledgeSet({
        name: newSetName,
        model_id: newSetModel,
        description: newSetDescription,
      });
      const local = res.set.provider === "local";
      setNotice(
        `Created knowledge set "${res.set.name}" on ${res.set.model_label} (${res.set.dims} dimensions) with its own ` +
          `vector index.` +
          (local && !models.find((m) => m.id === res.set.model_id)?.is_default
            ? " The model is downloading in the background - the first upload may wait for it to finish."
            : "")
      );
      setNewSetName("");
      setNewSetModel("");
      setNewSetDescription("");
      setSetId(res.set.set_id);
      await load();
    } catch (e: any) {
      setError(e.message || "Could not create the knowledge set");
    } finally {
      setCreatingSet(false);
    }
  }

  async function handleImportModel() {
    setImporting(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.importEmbeddingModel({
        kind: importKind,
        model_name: importName,
        label: importLabel,
        base_url: importKind === "openai_compatible" ? importBaseUrl : undefined,
        api_key: importKind === "openai_compatible" && importApiKey ? importApiKey : undefined,
        query_prefix: importQueryPrefix,
        doc_prefix: importDocPrefix,
      });
      const catalog = await api.embeddingModels();
      setModels(catalog.models);
      if (res.model.status === "ready") {
        setNewSetModel(res.model.id);
        setNotice(`Imported "${res.model.label}" (${res.model.dims} dimensions) - it's selected for the new set.`);
      } else {
        setNewSetModel("");
        setAwaitingModel(res.model.id);
        setNotice(
          `Importing "${res.model.label}" - AOM is downloading it and measuring its dimensions. ` +
            "It'll be selected here automatically when it's ready; large models can take a few minutes."
        );
      }
      setImportName("");
      setImportLabel("");
      setImportBaseUrl("");
      setImportApiKey("");
      setImportQueryPrefix("");
      setImportDocPrefix("");
    } catch (e: any) {
      setError(e.message || "Import failed");
    } finally {
      setImporting(false);
    }
  }

  async function handleDeleteModel(id: string, label: string) {
    if (!confirm(`Remove the imported model "${label}"?`)) return;
    setBusyId(`model:${id}`);
    setError(null);
    try {
      await api.deleteEmbeddingModel(id);
      if (newSetModel === id) setNewSetModel("");
      setModels((await api.embeddingModels()).models);
    } catch (e: any) {
      setError(e.message || "Remove failed");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDeleteSet(id: string, name: string) {
    if (!confirm(`Delete the empty knowledge set "${name}" and its vector index?`)) return;
    setBusyId(`set:${id}`);
    setError(null);
    try {
      await api.deleteKnowledgeSet(id);
      if (setId === id) setSetId(data?.default_set_id || "default");
      if (searchSetId === id) setSearchSetId("default");
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
      const res = await api.searchKnowledge(apiKey, query, 5, searchSetId);
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
                <label>Knowledge set</label>
                <select value={setId} onChange={(e) => setSetId(e.target.value)}>
                  {data.sets.map((s) => (
                    <option key={s.set_id} value={s.set_id}>
                      {s.name} - {s.model_label} ({s.dims} dims)
                    </option>
                  ))}
                  <option value={NEW_SET}>+ Create a new knowledge set with a different embedding model...</option>
                </select>
                <div className="field-hint">
                  A set fixes the embedding model: every document in it is embedded with that model and searched
                  through the set&rsquo;s own vector index, because vectors from different models can&rsquo;t be
                  compared.
                </div>
              </div>
              <div className="field">
                <label>Embedding model</label>
                {setId === NEW_SET ? (
                  <div className="field-hint" style={{ marginTop: 8 }}>Choose one for the new set below.</div>
                ) : (
                  (() => {
                    const current = data.sets.find((s) => s.set_id === setId);
                    const model = models.find((m) => m.id === current?.model_id);
                    return current ? (
                      <div className="set-model-summary">
                        <div style={{ fontWeight: 600 }}>{current.model_label}</div>
                        <div className="cell-muted" style={{ fontSize: 12.5 }}>
                          {current.provider_label} · {current.dims} dimensions
                          {model?.size ? ` · ${model.size}` : ""} · {current.document_count} document(s)
                        </div>
                        {model?.notes && (
                          <div className="cell-muted" style={{ fontSize: 12.5 }}>
                            {model.notes}
                          </div>
                        )}
                        {!current.available && (
                          <div className="error-note" style={{ marginTop: 6 }}>
                            This set&rsquo;s provider key isn&rsquo;t configured on the operations manager.
                          </div>
                        )}
                      </div>
                    ) : null;
                  })()
                )}
              </div>
            </div>

            {setId === NEW_SET && (
              <div className="new-set-panel">
                <div className="two-col">
                  <div className="field">
                    <label>Set name</label>
                    <input
                      type="text"
                      value={newSetName}
                      placeholder="e.g. Contracts (Voyage)"
                      onChange={(e) => setNewSetName(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label>Embedding model</label>
                    <select value={newSetModel} onChange={(e) => setNewSetModel(e.target.value)}>
                      <option value="">Choose one of {models.length} models...</option>
                      {Array.from(new Set(models.map((m) => m.provider_label))).map((group) => (
                        <optgroup key={group} label={group}>
                          {models
                            .filter((m) => m.provider_label === group)
                            .map((m) => (
                              <option key={m.id} value={m.id} disabled={!m.available}>
                                {modelOptionLabel(m)}
                              </option>
                            ))}
                        </optgroup>
                      ))}
                      <option value={IMPORT_MODEL}>+ Import your own model...</option>
                    </select>
                    {(() => {
                      const m = models.find((x) => x.id === newSetModel);
                      if (newSetModel === IMPORT_MODEL) {
                        return <div className="field-hint">Describe the model below and import it.</div>;
                      }
                      if (!m) {
                        return (
                          <div className="field-hint">
                            Local models run inside AOM and download once on first use; hosted models need their
                            provider&rsquo;s API key on the operations manager.
                          </div>
                        );
                      }
                      return (
                        <div className="field-hint">
                          {m.notes} {m.provider === "local"
                            ? "Runs on the operations manager's CPU; downloads from Hugging Face on first use."
                            : `Calls ${m.provider_label}'s API with ${m.requires}.`}
                        </div>
                      );
                    })()}
                  </div>
                </div>
                {newSetModel === IMPORT_MODEL && (
                  <div className="import-model-panel">
                    <div className="card-title" style={{ fontSize: 13.5, marginBottom: 10 }}>
                      Import your own embedding model
                    </div>
                    <div className="checkbox-row">
                      <input
                        type="radio"
                        id="imp-hf"
                        checked={importKind === "huggingface"}
                        onChange={() => setImportKind("huggingface")}
                      />
                      <label htmlFor="imp-hf" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                        Hugging Face sentence-transformers model - runs inside AOM
                      </label>
                    </div>
                    <div className="checkbox-row" style={{ marginBottom: 12 }}>
                      <input
                        type="radio"
                        id="imp-oai"
                        checked={importKind === "openai_compatible"}
                        onChange={() => setImportKind("openai_compatible")}
                      />
                      <label htmlFor="imp-oai" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                        OpenAI-compatible embeddings endpoint - Ollama, vLLM, TEI, LiteLLM, an internal gateway
                      </label>
                    </div>
                    <div className="two-col">
                      <div className="field">
                        <label>{importKind === "huggingface" ? "Model ID or local path" : "Model name"}</label>
                        <input
                          type="text"
                          value={importName}
                          placeholder={
                            importKind === "huggingface" ? "e.g. my-org/domain-embedder or /models/my-embedder" : "e.g. nomic-embed-text"
                          }
                          onChange={(e) => setImportName(e.target.value)}
                        />
                        <div className="field-hint">
                          {importKind === "huggingface"
                            ? "Downloaded from Hugging Face on import (or loaded from an absolute path already on the operations manager). Models that need trust_remote_code are refused."
                            : "The model name the endpoint expects."}
                        </div>
                      </div>
                      <div className="field">
                        <label>Display name (optional)</label>
                        <input
                          type="text"
                          value={importLabel}
                          placeholder="Shown in this dropdown"
                          onChange={(e) => setImportLabel(e.target.value)}
                        />
                      </div>
                    </div>
                    {importKind === "openai_compatible" && (
                      <div className="two-col">
                        <div className="field">
                          <label>Base URL</label>
                          <input
                            type="text"
                            value={importBaseUrl}
                            placeholder="e.g. http://ollama.internal:11434/v1"
                            onChange={(e) => setImportBaseUrl(e.target.value)}
                          />
                          <div className="field-hint">AOM calls &lt;base URL&gt;/embeddings from the operations manager.</div>
                        </div>
                        <div className="field">
                          <label>API key (optional)</label>
                          <input
                            type="password"
                            value={importApiKey}
                            placeholder="Sent as a Bearer token"
                            onChange={(e) => setImportApiKey(e.target.value)}
                          />
                          <div className="field-hint">Encrypted at rest; never shown again.</div>
                        </div>
                      </div>
                    )}
                    <div className="two-col">
                      <div className="field">
                        <label>Query prefix (optional)</label>
                        <input
                          type="text"
                          value={importQueryPrefix}
                          placeholder='e.g. "query: "'
                          onChange={(e) => setImportQueryPrefix(e.target.value)}
                        />
                      </div>
                      <div className="field">
                        <label>Document prefix (optional)</label>
                        <input
                          type="text"
                          value={importDocPrefix}
                          placeholder='e.g. "passage: "'
                          onChange={(e) => setImportDocPrefix(e.target.value)}
                        />
                      </div>
                    </div>
                    <div className="field-hint" style={{ marginBottom: 10 }}>
                      Only if the model&rsquo;s card says queries and documents need different prefixes. AOM measures
                      the vector dimension itself on import.
                    </div>
                    <button
                      className="btn btn-secondary"
                      type="button"
                      disabled={
                        importing || !importName.trim() || (importKind === "openai_compatible" && !importBaseUrl.trim())
                      }
                      onClick={handleImportModel}
                    >
                      {importing ? "Importing..." : "Import model"}
                    </button>
                  </div>
                )}

                <div className="field">
                  <label>Description (optional)</label>
                  <input
                    type="text"
                    value={newSetDescription}
                    placeholder="What belongs in this set"
                    onChange={(e) => setNewSetDescription(e.target.value)}
                  />
                </div>
                <button
                  className="btn btn-secondary"
                  type="button"
                  disabled={creatingSet || !newSetName.trim() || !newSetModel || newSetModel === IMPORT_MODEL}
                  onClick={handleCreateSet}
                >
                  {creatingSet ? "Creating set and vector index..." : "Create knowledge set"}
                </button>
              </div>
            )}

            <div className="field">
              <label>What are you adding?</label>
              <div className="flex-row" style={{ gap: 18 }}>
                <div className="checkbox-row" style={{ margin: 0 }}>
                  <input
                    type="radio"
                    id="mode-doc"
                    checked={addMode === "document"}
                    onChange={() => {
                      setAddMode("document");
                      setFile(null);
                      if (fileInput.current) fileInput.current.value = "";
                    }}
                  />
                  <label htmlFor="mode-doc" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                    A document (text, Markdown, PDF, HTML...)
                  </label>
                </div>
                <div className="checkbox-row" style={{ margin: 0 }}>
                  <input
                    type="radio"
                    id="mode-dump"
                    checked={addMode === "dump"}
                    onChange={() => {
                      setAddMode("dump");
                      setFile(null);
                      if (fileInput.current) fileInput.current.value = "";
                    }}
                  />
                  <label htmlFor="mode-dump" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                    A data dump (Snowflake, Databricks or BigQuery table export)
                  </label>
                </div>
              </div>
            </div>

            {addMode === "dump" ? (
              <>
                <div className="two-col">
                  <div className="field">
                    <label>Source</label>
                    <select value={dumpSource} onChange={(e) => setDumpSource(e.target.value)}>
                      {data.dump_sources.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label>Export file</label>
                    <input
                      ref={fileInput}
                      type="file"
                      accept={data.dump_extensions.join(",")}
                      onChange={(e) => {
                        const f = e.target.files?.[0] || null;
                        setFile(f);
                        if (f && !tableName) setTableName(f.name.replace(/(\.(gz|zip|csv|tsv|json|jsonl|ndjson|parquet|avro|snappy))+$/i, ""));
                      }}
                    />
                    <div className="field-hint">
                      CSV/TSV, newline-delimited JSON, Parquet or Avro - gzip-compressed, or several files zipped
                      together. Up to {data.max_upload_mb} MB and {data.max_dump_rows.toLocaleString()} rows.
                    </div>
                  </div>
                </div>
                <div className="two-col">
                  <div className="field">
                    <label>Table name</label>
                    <input
                      type="text"
                      value={tableName}
                      placeholder="e.g. SALES.PUBLIC.ORDERS"
                      onChange={(e) => setTableName(e.target.value)}
                    />
                    <div className="field-hint">Repeated in every chunk, so a retrieved row always says what table it came from.</div>
                  </div>
                  <div className="field">
                    <label>Header row (CSV/TSV)</label>
                    <select value={headerMode} onChange={(e) => setHeaderMode(e.target.value as "auto" | "yes" | "no")}>
                      <option value="auto">Auto-detect</option>
                      <option value="yes">First row is the header</option>
                      <option value="no">No header row</option>
                    </select>
                    {headerMode === "no" && (
                      <input
                        type="text"
                        style={{ marginTop: 8 }}
                        value={columnNames}
                        placeholder="Column names, comma-separated (optional)"
                        onChange={(e) => setColumnNames(e.target.value)}
                      />
                    )}
                    <div className="field-hint">
                      Ignored for JSON, Parquet and Avro, which carry their own column names.
                    </div>
                  </div>
                </div>
                {EXPORT_HELP[dumpSource] && (
                  <details className="export-help">
                    <summary>{EXPORT_HELP[dumpSource].title}</summary>
                    {EXPORT_HELP[dumpSource].code && (
                      <pre className="json-block agent-code-block" style={{ margin: "8px 0" }}>
                        {EXPORT_HELP[dumpSource].code}
                      </pre>
                    )}
                    <div className="field-hint">{EXPORT_HELP[dumpSource].note}</div>
                  </details>
                )}
              </>
            ) : (
              <>
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
              </>
            )}

            <div className="two-col">
              <div className="field">
                <label>Expires</label>
                <select value={expiryChoice} onChange={(e) => setExpiryChoice(e.target.value)}>
                  {EXPIRY_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.value === "never" ? "Never (keep until deleted)" : o.label}
                    </option>
                  ))}
                </select>
                {expiryChoice === "date" && (
                  <input
                    type="date"
                    style={{ marginTop: 8 }}
                    value={expiryDate}
                    onChange={(e) => setExpiryDate(e.target.value)}
                  />
                )}
                <div className="field-hint">
                  An expired document stops being retrieved the moment it expires (RAG answers included) and is
                  deleted within {Math.round(data.expiry_sweep_seconds / 60)} minutes. Change it any time below.
                </div>
              </div>
            </div>

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

            <button
              className="btn btn-primary"
              type="submit"
              disabled={uploading || allowedRoles.length === 0 || setId === NEW_SET || (addMode === "dump" && !file)}
            >
              {uploading
                ? addMode === "dump"
                  ? "Parsing rows and embedding..."
                  : "Chunking and embedding..."
                : addMode === "dump"
                  ? "Import data dump"
                  : "Ingest document"}
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
                <label style={{ marginTop: 10 }}>Knowledge set</label>
                <select value={searchSetId} onChange={(e) => setSearchSetId(e.target.value)}>
                  {data.sets.map((s) => (
                    <option key={s.set_id} value={s.set_id}>
                      {s.name} - {s.model_label}
                    </option>
                  ))}
                </select>
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

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Knowledge sets</h2>
          <div className="card" style={{ marginBottom: 24 }}>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Set</th>
                    <th>Embedding model</th>
                    <th style={{ textAlign: "right" }}>Dims</th>
                    <th style={{ textAlign: "right" }}>Documents</th>
                    <th style={{ textAlign: "right" }}>Chunks</th>
                    <th>Vector index</th>
                    <th>RAG apps</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {data.sets.map((s) => (
                    <tr key={s.set_id}>
                      <td>
                        <div style={{ fontWeight: 600 }}>{s.name}</div>
                        <div className="cell-muted cell-mono" style={{ fontSize: 12 }}>
                          {s.set_id}
                        </div>
                      </td>
                      <td>
                        <div>{s.model_label}</div>
                        <div className="cell-muted" style={{ fontSize: 12 }}>
                          {s.provider_label}
                          {!s.available && " · key not configured"}
                        </div>
                      </td>
                      <td className="cell-mono" style={{ textAlign: "right" }}>{s.dims}</td>
                      <td className="cell-mono" style={{ textAlign: "right" }}>{s.document_count}</td>
                      <td className="cell-mono" style={{ textAlign: "right" }}>{s.chunk_count}</td>
                      <td className="cell-muted cell-mono" style={{ fontSize: 12 }}>{s.index_name}</td>
                      <td className="cell-muted cell-mono" style={{ fontSize: 12 }}>
                        {s.rag_apps.length ? s.rag_apps.join(", ") : "-"}
                      </td>
                      <td>
                        {!s.builtin && (
                          <button
                            className="btn btn-danger-outline btn-sm"
                            disabled={busyId === `set:${s.set_id}` || s.document_count > 0 || s.rag_apps.length > 0}
                            title={
                              s.document_count > 0 || s.rag_apps.length > 0
                                ? "Delete its documents and RAG applications first"
                                : undefined
                            }
                            onClick={() => handleDeleteSet(s.set_id, s.name)}
                          >
                            Delete
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {models.some((m) => m.custom) && (
            <>
              <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Imported embedding models</h2>
              <div className="card" style={{ marginBottom: 24 }}>
                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Model</th>
                        <th>Source</th>
                        <th style={{ textAlign: "right" }}>Dims</th>
                        <th>Status</th>
                        <th>Used by</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {models
                        .filter((m) => m.custom)
                        .map((m) => {
                          const usedBy = data.sets.filter((s) => s.model_id === m.id).map((s) => s.name);
                          return (
                            <tr key={m.id}>
                              <td>
                                <div style={{ fontWeight: 600 }}>{m.label}</div>
                                <div className="cell-muted cell-mono" style={{ fontSize: 12 }}>
                                  {m.id}
                                </div>
                              </td>
                              <td className="cell-muted cell-mono" style={{ fontSize: 12 }}>
                                {m.provider === "custom_openai" ? `${m.base_url} · ${m.source}` : m.source}
                                {m.has_api_key ? " · key on file" : ""}
                              </td>
                              <td className="cell-mono" style={{ textAlign: "right" }}>{m.dims ?? "-"}</td>
                              <td>
                                <span
                                  className={
                                    m.status === "ready"
                                      ? "badge badge-success"
                                      : m.status === "error"
                                        ? "badge badge-danger"
                                        : "badge badge-medium"
                                  }
                                  title={m.error || undefined}
                                >
                                  {m.status === "pending" ? "downloading" : m.status}
                                </span>
                                {m.status === "error" && m.error && (
                                  <div className="cell-muted" style={{ fontSize: 11.5, maxWidth: 320 }}>
                                    {m.error}
                                  </div>
                                )}
                              </td>
                              <td className="cell-muted" style={{ fontSize: 12 }}>{usedBy.join(", ") || "-"}</td>
                              <td>
                                <button
                                  className="btn btn-danger-outline btn-sm"
                                  disabled={busyId === `model:${m.id}` || usedBy.length > 0}
                                  title={usedBy.length ? "Delete the sets using it first" : undefined}
                                  onClick={() => handleDeleteModel(m.id, m.label)}
                                >
                                  Remove
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}

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
                      <th>Set</th>
                      <th>Format</th>
                      <th>Readable by</th>
                      <th style={{ textAlign: "right" }}>Chunks</th>
                      <th style={{ textAlign: "right" }}>Characters</th>
                      <th>Added</th>
                      <th>Expires</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.documents.map((d) => (
                      <tr key={d.document_id}>
                        <td>
                          <div style={{ fontWeight: 600 }}>{d.title}</div>
                          <div className="cell-muted cell-mono" style={{ fontSize: 12 }}>{d.source}</div>
                          {d.dump && (
                            <div className="cell-muted" style={{ fontSize: 12 }}>
                              {d.dump.source_label} dump · {d.dump.table_name} · {d.dump.rows_indexed.toLocaleString()}
                              {d.dump.truncated ? ` of ${d.dump.total_rows.toLocaleString()}` : ""} rows ·{" "}
                              {d.dump.column_count} columns
                            </div>
                          )}
                        </td>
                        <td className="cell-muted cell-mono" style={{ fontSize: 12 }}>
                          {data.sets.find((s) => s.set_id === (d.set_id || "default"))?.name || d.set_id || "default"}
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
                        <td className="cell-muted" style={{ fontSize: 12, minWidth: 150 }}>
                          <div>{expiryLabel(d.expires_at)}</div>
                          <select
                            className="expiry-select"
                            value=""
                            disabled={busyId === `exp:${d.document_id}`}
                            onChange={(e) => e.target.value && handleChangeExpiry(d.document_id, e.target.value)}
                          >
                            <option value="">Change...</option>
                            {EXPIRY_OPTIONS.map((o) => (
                              <option key={o.value} value={o.value}>
                                {o.value === "never" ? "Never expire" : o.value === "date" ? "On a date..." : `In ${o.label}`}
                              </option>
                            ))}
                          </select>
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
