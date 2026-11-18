import { useState } from "react";
import { api } from "../../api/client";
import type { EmbeddingModelOption, KnowledgeSet } from "../../api/types";

/**
 * Edit a knowledge set: its name, its description and its embedding model.
 *
 * Name and description save immediately. A different embedding model is a
 * bigger change - vectors from two models can't be compared, so every chunk
 * the set already holds is embedded again with the new one. The operations
 * manager does that in the background, beside the live data: the set keeps
 * answering from its current model until the new vectors are complete, then
 * switches over in one step (see update_knowledge_set in app/main.py). This
 * panel only starts it; the Knowledge sets table shows the progress.
 */
export function EditKnowledgeSetPanel({
  set,
  models,
  onSaved,
  onCancel,
}: {
  set: KnowledgeSet;
  models: EmbeddingModelOption[];
  onSaved: (note: string) => void | Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(set.name);
  const [description, setDescription] = useState(set.description || "");
  const [modelId, setModelId] = useState(set.model_id);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const running = set.reembed?.status === "running";
  const target = models.find((m) => m.id === modelId);
  const modelChanged = modelId !== set.model_id;
  const selectable = (m: EmbeddingModelOption) =>
    m.id === set.model_id || (m.available && (!m.custom || m.status === "ready"));

  const providers: string[] = [];
  for (const m of models) if (!providers.includes(m.provider_label)) providers.push(m.provider_label);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (
      modelChanged &&
      set.chunk_count > 0 &&
      !window.confirm(
        `Re-embed all ${set.chunk_count.toLocaleString()} chunk(s) in "${set.name}" with ${target?.label || modelId}?`,
      )
    )
      return;
    setSaving(true);
    setError(null);
    try {
      const res = await api.updateKnowledgeSet(set.set_id, {
        name: name.trim(),
        description: description.trim(),
        model_id: modelChanged ? modelId : undefined,
      });
      await onSaved(
        res.reembed_started
          ? `"${res.set.name}" is being re-embedded with ${res.set.reembed?.model_label || modelId}. It keeps answering from ${set.model_label} until that finishes.`
          : `Knowledge set "${res.set.name}" saved.`,
      );
    } catch (e: any) {
      setError(e.message || "Could not save the knowledge set");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="panel section-gap" onSubmit={handleSubmit} style={{ marginBottom: 16 }}>
      <div className="flex-between" style={{ marginBottom: 12 }}>
        <div style={{ fontWeight: 600 }}>
          Edit knowledge set <span className="cell-mono cell-muted">{set.set_id}</span>
        </div>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onCancel}>
          Close
        </button>
      </div>
      {error && <div className="error-note">{error}</div>}

      <div className="two-col">
        <div className="field">
          <label>Name</label>
          <input type="text" required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
          <div className="field-hint">The set ID, storage location and RAG applications are not affected by a rename.</div>
        </div>
        <div className="field">
          <label>Description</label>
          <input type="text" maxLength={300} value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
      </div>

      <div className="field">
        <label>Embedding model</label>
        <select value={modelId} disabled={running} onChange={(e) => setModelId(e.target.value)}>
          {providers.map((provider) => (
            <optgroup key={provider} label={provider}>
              {models
                .filter((m) => m.provider_label === provider)
                .map((m) => (
                  <option key={m.id} value={m.id} disabled={!selectable(m)}>
                    {m.label} - {m.dims} dims
                    {m.id === set.model_id ? " (current)" : ""}
                    {!m.available ? ` - needs ${m.requires || "its API key"}` : ""}
                    {m.custom && m.status === "pending" ? " - still downloading" : ""}
                    {m.custom && m.status === "error" ? " - failed verification" : ""}
                  </option>
                ))}
            </optgroup>
          ))}
          {!models.some((m) => m.id === set.model_id) && <option value={set.model_id}>{set.model_label} (current)</option>}
        </select>
        <div className="field-hint">
          {running
            ? "A model change is already running for this set - wait for it to finish, or cancel it from the table below."
            : "Note: when determining what embedding model to use, it's always a good idea to research which embedding models best support the LLM and your use-case."}
        </div>
      </div>

      {modelChanged && target && (
        <div className="helper-banner">
          <div className="helper-banner-heading">
            Changing the model re-embeds {set.chunk_count.toLocaleString()} chunk(s) across{" "}
            {set.document_count.toLocaleString()} document(s)
          </div>
          <ul style={{ margin: "4px 0 0 18px", padding: 0 }}>
            <li>
              Vectors from {set.model_label} and {target.label} can&rsquo;t be compared, so every chunk already in this set
              is embedded again from its stored text. Nothing needs to be uploaded again.
            </li>
            <li>
              It runs in the background. Search and RAG applications keep answering from {set.model_label} until the new
              vectors are complete, then the set switches over in one step
              {target.dims !== set.dims ? ` (${set.dims} to ${target.dims} dimensions, on a new vector index)` : ""}.
            </li>
            <li>New documents, cloud imports and warehouse syncs for this set are refused until it finishes.</li>
            <li>While it runs the set stores both sets of vectors; the old ones are removed afterwards.</li>
            {target.provider !== "local" && !target.custom && (
              <li>
                {target.label} is a hosted model: re-embedding sends every chunk&rsquo;s text to {target.provider_label}{" "}
                and is billed by them.
              </li>
            )}
            {set.rag_apps.length > 0 && (
              <li>
                RAG application(s) {set.rag_apps.join(", ")} use this set. They keep working throughout; answers may
                change once retrieval is ranked by the new model.
              </li>
            )}
          </ul>
        </div>
      )}

      <button className="btn btn-primary" type="submit" disabled={saving || !name.trim()}>
        {saving
          ? "Saving..."
          : modelChanged
            ? set.chunk_count > 0
              ? `Save and re-embed ${set.chunk_count.toLocaleString()} chunk(s)`
              : "Save and change model"
            : "Save changes"}
      </button>
    </form>
  );
}
