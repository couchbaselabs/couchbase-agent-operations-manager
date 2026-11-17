import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type {
  CustomLLMPreset,
  CustomLLMProvider,
  CustomLLMProviderPayload,
  CustomLLMTestResult,
  LLMAuthHeader,
  LLMMaxTokensParam,
} from "../../api/types";

/**
 * Custom LLM providers - the "Add LLM provider" form and the list of what
 * has been added.
 *
 * Every custom provider is an endpoint that speaks OpenAI's
 * chat-completions API. The provider-type dropdown pre-fills the form for
 * the hosted platforms that do (Azure OpenAI, Snowflake Cortex,
 * Databricks, Google DeepMind, Cohere, Mistral AI) and for a self-hosted
 * server; "Custom LLM provider" starts it blank. Once saved, a provider
 * shows up as a card above this panel and in every provider dropdown on
 * the page (per-role / per-agent overrides, RAG applications), exactly
 * like a built-in one. See "Custom providers" in app/llm_cache.py.
 */

const CUSTOM = "custom";

type ModelRow = { id: string; input: string; output: string };

type Form = {
  editingId: string | null;
  preset: string; // CUSTOM or a preset id
  label: string;
  base_url: string;
  api_key: string;
  had_api_key: boolean;
  remove_api_key: boolean;
  auth_header: LLMAuthHeader;
  max_tokens_param: LLMMaxTokensParam;
  models: ModelRow[];
  default_model: string;
};

function blankForm(): Form {
  return {
    editingId: null,
    preset: CUSTOM,
    label: "",
    base_url: "",
    api_key: "",
    had_api_key: false,
    remove_api_key: false,
    auth_header: "bearer",
    max_tokens_param: "max_tokens",
    models: [{ id: "", input: "0", output: "0" }],
    default_model: "",
  };
}

function fromPreset(preset: CustomLLMPreset | undefined): Form {
  if (!preset) return blankForm();
  const models = preset.models.map((m) => ({
    id: m.id,
    input: String(m.input_usd_per_1m),
    output: String(m.output_usd_per_1m),
  }));
  return {
    ...blankForm(),
    preset: preset.id,
    label: preset.label,
    base_url: preset.base_url,
    auth_header: preset.auth_header,
    max_tokens_param: preset.max_tokens_param,
    models: models.length ? models : [{ id: "", input: "0", output: "0" }],
    default_model: models[0]?.id || "",
  };
}

function fromProvider(p: CustomLLMProvider): Form {
  return {
    editingId: p.id,
    preset: p.preset || CUSTOM,
    label: p.label,
    base_url: p.base_url,
    api_key: "",
    had_api_key: p.has_api_key,
    remove_api_key: false,
    auth_header: p.auth_header,
    max_tokens_param: p.max_tokens_param,
    models: p.models.map((m) => ({
      id: m.id,
      input: String(m.input_usd_per_1m),
      output: String(m.output_usd_per_1m),
    })),
    default_model: p.default_model,
  };
}

function toPayload(f: Form): CustomLLMProviderPayload {
  const models = f.models
    .filter((m) => m.id.trim())
    .map((m) => ({
      id: m.id.trim(),
      input_usd_per_1m: Number(m.input) || 0,
      output_usd_per_1m: Number(m.output) || 0,
    }));
  // On an edit, a blank key field means "keep the stored key".
  let api_key: string | null = f.api_key.trim();
  if (f.editingId) api_key = f.remove_api_key ? "" : api_key || null;
  return {
    preset: f.preset === CUSTOM ? null : f.preset,
    label: f.label.trim(),
    base_url: f.base_url.trim(),
    models,
    default_model: f.default_model.trim(),
    max_tokens_param: f.max_tokens_param,
    auth_header: f.auth_header,
    api_key,
  };
}

export function CustomProvidersPanel({ onChanged }: { onChanged: () => void | Promise<void> }) {
  const [providers, setProviders] = useState<CustomLLMProvider[]>([]);
  const [presets, setPresets] = useState<CustomLLMPreset[]>([]);
  const [form, setForm] = useState<Form | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [test, setTest] = useState<{ target: string; result: CustomLLMTestResult } | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.customLlmProviders();
      setProviders(res.providers);
      setPresets(res.presets);
    } catch (e: any) {
      setError(e.message || "Failed to load custom LLM providers");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const preset = presets.find((p) => p.id === form?.preset);

  function patch(next: Partial<Form>) {
    setForm((f) => (f ? { ...f, ...next } : f));
  }

  function patchModel(index: number, next: Partial<ModelRow>) {
    setForm((f) => {
      if (!f) return f;
      const models = f.models.map((m, i) => (i === index ? { ...m, ...next } : m));
      // Keep the default pointing at the row being renamed.
      const renamedDefault = next.id !== undefined && f.models[index].id === f.default_model;
      return { ...f, models, default_model: renamedDefault ? next.id! : f.default_model };
    });
  }

  function openForm(next: Form) {
    setError(null);
    setNote(null);
    setTest(null);
    setForm(next);
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!form) return;
    setBusy("save");
    setError(null);
    setNote(null);
    try {
      const payload = toPayload(form);
      const res = form.editingId
        ? await api.updateCustomLlmProvider(form.editingId, payload)
        : await api.createCustomLlmProvider(payload);
      setNote(
        form.editingId
          ? `${res.provider.label} updated.`
          : `${res.provider.label} added - select its card above to make it the default, or route a role or agent to it under "Per-role and per-agent overrides".`,
      );
      setForm(null);
      setTest(null);
      await load();
      await onChanged();
    } catch (e: any) {
      setError(e.message || "Failed to save the provider");
    } finally {
      setBusy(null);
    }
  }

  async function handleTestForm() {
    if (!form) return;
    setBusy("test-form");
    setError(null);
    setTest(null);
    try {
      const result = await api.testCustomLlmProvider({
        provider_id: form.editingId || undefined,
        provider: toPayload(form),
      });
      setTest({ target: "form", result });
    } catch (e: any) {
      setError(e.message || "Test failed");
    } finally {
      setBusy(null);
    }
  }

  async function handleTestSaved(p: CustomLLMProvider) {
    setBusy(`test-${p.id}`);
    setError(null);
    setTest(null);
    try {
      setTest({ target: p.id, result: await api.testCustomLlmProvider({ provider_id: p.id }) });
    } catch (e: any) {
      setError(e.message || "Test failed");
    } finally {
      setBusy(null);
    }
  }

  async function handleDelete(p: CustomLLMProvider) {
    if (!window.confirm(`Remove ${p.label}? Its stored API key and cached answers are deleted.`)) return;
    setBusy(`delete-${p.id}`);
    setError(null);
    setNote(null);
    try {
      const res = await api.deleteCustomLlmProvider(p.id);
      setNote(`${p.label} removed${res.entries_purged ? ` - ${res.entries_purged} cached entr(ies) purged` : ""}.`);
      if (form?.editingId === p.id) setForm(null);
      await load();
      await onChanged();
    } catch (e: any) {
      setError(e.message || "Failed to remove the provider");
    } finally {
      setBusy(null);
    }
  }

  function renderTest(result: CustomLLMTestResult) {
    return (
      <div
        className="helper-banner"
        style={
          result.ok
            ? { marginTop: 12, marginBottom: 0, background: "var(--gray-bg)", borderColor: "var(--border)", color: "var(--text)" }
            : { marginTop: 12, marginBottom: 0, background: "var(--red-bg)", borderColor: "var(--red-border)", color: "var(--red-dim)" }
        }
      >
        <div className="helper-banner-heading">
          {result.ok ? "✓ Connection works" : "✗ Connection failed"} - {result.model} ({result.latency_ms}ms)
        </div>
        {result.ok ? (
          <span>
            The model replied{result.reply ? `: "${result.reply}"` : " (with an empty message)"} -{" "}
            {result.prompt_tokens} tokens in, {result.completion_tokens} out.
          </span>
        ) : (
          <span style={{ wordBreak: "break-word" }}>{result.error}</span>
        )}
      </div>
    );
  }

  return (
    <div className="panel section-gap" style={{ marginBottom: 18 }}>
      <div className="flex-between" style={{ marginBottom: providers.length || form ? 14 : 0 }}>
        <div>
          <div style={{ fontWeight: 600 }}>Custom LLM providers</div>
          <div className="field-hint" style={{ marginTop: 2 }}>
            Add Azure OpenAI, Snowflake Cortex, Databricks-hosted LLMs, Google DeepMind, Cohere, Mistral AI, a
            self-hosted model or any other endpoint that speaks OpenAI's chat-completions API.
          </div>
        </div>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => (form ? setForm(null) : openForm(blankForm()))}
        >
          {form ? "Cancel" : "Add LLM provider"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}
      {note && <div className="loading-note">{note}</div>}

      {form && (
        <form onSubmit={handleSave} style={{ marginBottom: providers.length ? 18 : 0 }}>
          <div className="two-col">
            <div className="field">
              <label>Provider type</label>
              <select
                value={form.preset}
                disabled={!!form.editingId}
                onChange={(e) =>
                  openForm(e.target.value === CUSTOM ? blankForm() : fromPreset(presets.find((p) => p.id === e.target.value)))
                }
              >
                <option value={CUSTOM}>Custom LLM provider</option>
                {presets.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </select>
              <div className="field-hint">
                {form.editingId
                  ? "The type is fixed once a provider is saved."
                  : "A preconfigured type fills in the endpoint, authentication and suggested models - every field stays editable."}
              </div>
            </div>
            <div className="field">
              <label>Name</label>
              <input
                type="text"
                required
                maxLength={60}
                value={form.label}
                onChange={(e) => patch({ label: e.target.value })}
                placeholder="e.g. Internal vLLM cluster"
              />
              <div className="field-hint">How the provider appears on its card, in dropdowns and on the dashboard.</div>
            </div>
          </div>

          {preset?.notes && (
            <div className="helper-banner helper-banner-neutral">
              {preset.notes}{" "}
              {preset.docs_url && (
                <a href={preset.docs_url} target="_blank" rel="noopener noreferrer">
                  {preset.label} documentation
                </a>
              )}
            </div>
          )}

          <div className="two-col">
            <div className="field">
              <label>Base URL</label>
              <input
                type="text"
                required
                value={form.base_url}
                onChange={(e) => patch({ base_url: e.target.value })}
                placeholder="https://llm.example.com/v1"
                style={{ fontFamily: "var(--font-mono, monospace)" }}
              />
              <div className="field-hint">
                The root that <code>/chat/completions</code> is appended to. It must be reachable from the
                operations-manager container.
              </div>
            </div>
            <div className="field">
              <label>{preset?.api_key_label || "API key (leave blank if the endpoint needs none)"}</label>
              <input
                type="password"
                autoComplete="new-password"
                value={form.api_key}
                disabled={form.remove_api_key}
                required={!form.editingId && !!preset?.api_key_required}
                onChange={(e) => patch({ api_key: e.target.value })}
                placeholder={form.had_api_key ? "•••••••• (stored - leave blank to keep)" : ""}
              />
              <div className="field-hint">Stored encrypted in Couchbase and never shown again.</div>
              {form.had_api_key && (
                <div className="checkbox-row">
                  <input
                    type="checkbox"
                    id="custom-llm-remove-key"
                    checked={form.remove_api_key}
                    onChange={(e) => patch({ remove_api_key: e.target.checked, api_key: "" })}
                  />
                  <label htmlFor="custom-llm-remove-key" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                    Remove the stored key
                  </label>
                </div>
              )}
            </div>
          </div>

          <div className="two-col">
            <div className="field">
              <label>API key header</label>
              <select value={form.auth_header} onChange={(e) => patch({ auth_header: e.target.value as LLMAuthHeader })}>
                <option value="bearer">Authorization: Bearer &lt;key&gt;</option>
                <option value="api-key">api-key: &lt;key&gt; (Azure OpenAI)</option>
              </select>
            </div>
            <div className="field">
              <label>Output-token limit parameter</label>
              <select
                value={form.max_tokens_param}
                onChange={(e) => patch({ max_tokens_param: e.target.value as LLMMaxTokensParam })}
              >
                <option value="max_tokens">max_tokens</option>
                <option value="max_completion_tokens">max_completion_tokens</option>
              </select>
              <div className="field-hint">
                Most endpoints take <code>max_tokens</code>; newer OpenAI-family models require{" "}
                <code>max_completion_tokens</code>.
              </div>
            </div>
          </div>

          <div className="field">
            <label>Models</label>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Model ID (sent as "model")</th>
                    <th>Input $ / 1M tokens</th>
                    <th>Output $ / 1M tokens</th>
                    <th>Default</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {form.models.map((m, i) => (
                    <tr key={i}>
                      <td>
                        <input
                          type="text"
                          value={m.id}
                          onChange={(e) => patchModel(i, { id: e.target.value })}
                          placeholder={form.preset === "azure_openai" ? "your deployment name" : "model name"}
                          style={{ fontFamily: "var(--font-mono, monospace)", width: "100%" }}
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          min={0}
                          step="any"
                          value={m.input}
                          onChange={(e) => patchModel(i, { input: e.target.value })}
                          style={{ width: 110 }}
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          min={0}
                          step="any"
                          value={m.output}
                          onChange={(e) => patchModel(i, { output: e.target.value })}
                          style={{ width: 110 }}
                        />
                      </td>
                      <td>
                        <input
                          type="radio"
                          name="custom-llm-default-model"
                          aria-label={`Make ${m.id || "this model"} the default`}
                          checked={!!m.id && m.id === form.default_model}
                          disabled={!m.id.trim()}
                          onChange={() => patch({ default_model: m.id })}
                        />
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          disabled={form.models.length === 1}
                          onClick={() => patch({ models: form.models.filter((_, j) => j !== i) })}
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex-row" style={{ marginTop: 8 }}>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => patch({ models: [...form.models, { id: "", input: "0", output: "0" }] })}
              >
                Add model
              </button>
            </div>
            <div className="field-hint">
              Prices turn tokens saved into dollars saved on the LLM Cache dashboard and in the ROI export. Suggested
              prices are list-price estimates - replace them with your contracted rate, or leave 0 for a model you
              host yourself.
            </div>
          </div>

          <div className="flex-row">
            <button className="btn btn-primary" type="submit" disabled={busy === "save"}>
              {busy === "save" ? "Saving..." : form.editingId ? "Save changes" : "Add provider"}
            </button>
            <button type="button" className="btn btn-secondary" onClick={handleTestForm} disabled={busy === "test-form"}>
              {busy === "test-form" ? "Testing..." : "Test connection"}
            </button>
          </div>
          {test?.target === "form" && renderTest(test.result)}
        </form>
      )}

      {providers.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Provider</th>
                <th>Endpoint</th>
                <th>Models</th>
                <th>Key</th>
                <th>Used by</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => (
                <tr key={p.id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{p.label}</div>
                    <div className="cell-muted">{p.vendor}</div>
                  </td>
                  <td className="cell-mono" style={{ wordBreak: "break-all" }}>
                    {p.base_url}
                  </td>
                  <td>
                    {p.models.map((m) => (
                      <div key={m.id} className="cell-mono">
                        {m.id}
                        {m.id === p.default_model ? " (default)" : ""}
                      </div>
                    ))}
                  </td>
                  <td>
                    <span className={`badge ${p.has_api_key ? "badge-trusted" : "badge-neutral"}`}>
                      {p.has_api_key ? "key set" : "no key"}
                    </span>
                  </td>
                  <td className="cell-muted">{p.in_use_by?.length ? p.in_use_by.join(", ") : "-"}</td>
                  <td>
                    <div className="flex-row">
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        disabled={busy === `test-${p.id}`}
                        onClick={() => handleTestSaved(p)}
                      >
                        {busy === `test-${p.id}` ? "Testing..." : "Test"}
                      </button>
                      <button type="button" className="btn btn-secondary btn-sm" onClick={() => openForm(fromProvider(p))}>
                        Edit
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger-outline btn-sm"
                        disabled={busy === `delete-${p.id}`}
                        onClick={() => handleDelete(p)}
                      >
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {test && test.target !== "form" && renderTest(test.result)}
        </div>
      )}
    </div>
  );
}
