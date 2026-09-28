import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { DiscoveredTool, HijackScanResult } from "../api/types";
import { SeverityBadge } from "../components/badges/Badges";

const API_KEY_STORAGE_KEY = "agent-tool-audit-api-key";

function loadStoredApiKey(): string {
  try {
    return localStorage.getItem(API_KEY_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function AgentToolAuditPage() {
  const [apiKey, setApiKey] = useState(loadStoredApiKey());
  const [query, setQuery] = useState("look up a customer support ticket");
  const [topK, setTopK] = useState(5);

  const [discoverLoading, setDiscoverLoading] = useState(false);
  const [discoverError, setDiscoverError] = useState<string | null>(null);
  const [discoverResult, setDiscoverResult] = useState<{ role: string; tools: DiscoveredTool[]; latency_ms: number } | null>(null);

  const [toolId, setToolId] = useState("");
  const [argsText, setArgsText] = useState("{}");
  const [invokeLoading, setInvokeLoading] = useState(false);
  const [invokeError, setInvokeError] = useState<string | null>(null);
  const [invokeResult, setInvokeResult] = useState<unknown>(null);
  const [invokeLatency, setInvokeLatency] = useState<number | null>(null);
  const [invokeHijackWarning, setInvokeHijackWarning] = useState<HijackScanResult | null>(null);

  function persistApiKey(v: string) {
    setApiKey(v);
    try {
      localStorage.setItem(API_KEY_STORAGE_KEY, v);
    } catch {
      // best-effort only
    }
  }

  async function handleDiscover(e: React.FormEvent) {
    e.preventDefault();
    setDiscoverLoading(true);
    setDiscoverError(null);
    setDiscoverResult(null);
    try {
      const res = await api.discover(apiKey, query, topK);
      setDiscoverResult(res);
    } catch (err) {
      const e2 = err as ApiError;
      setDiscoverError(e2.message || "Discovery failed");
    } finally {
      setDiscoverLoading(false);
    }
  }

  async function handleInvoke(e: React.FormEvent) {
    e.preventDefault();
    setInvokeLoading(true);
    setInvokeError(null);
    setInvokeResult(null);
    setInvokeLatency(null);
    setInvokeHijackWarning(null);
    let parsedArgs: Record<string, unknown> = {};
    try {
      parsedArgs = argsText.trim() ? JSON.parse(argsText) : {};
    } catch {
      setInvokeError("Arguments must be valid JSON.");
      setInvokeLoading(false);
      return;
    }
    try {
      const res = await api.invoke(apiKey, toolId, parsedArgs);
      setInvokeResult(res.result);
      setInvokeLatency(res.latency_ms);
      setInvokeHijackWarning(res.hijack_warning);
    } catch (err) {
      const e2 = err as ApiError;
      setInvokeError(`${e2.status ? `[${e2.status}] ` : ""}${e2.message || "Invoke failed"}`);
    } finally {
      setInvokeLoading(false);
    }
  }

  function loadToolIntoInvoke(t: DiscoveredTool) {
    setToolId(t.tool_id);
    setArgsText("{}");
    setInvokeResult(null);
    setInvokeError(null);
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Agent Tool Audit</h1>
          <p className="page-subtitle">
            Call the live discover/invoke API as a given role and see exactly what Couchbase's RBAC + vector
            pre-filter returns - no LLM in the loop, just the raw gateway.
          </p>
        </div>
      </div>

      <div className="field" style={{ maxWidth: 420, marginBottom: 22 }}>
        <label>API key</label>
        <input
          type="text"
          placeholder="e.g. demo-support-agent-9f21"
          value={apiKey}
          onChange={(e) => persistApiKey(e.target.value)}
        />
        <div className="field-hint">
          Sent as <code>Authorization: Bearer &lt;api_key&gt;</code>. Kept only in this browser's local storage -
          never sent anywhere but this appliance's own operations manager.
        </div>
      </div>

      <div className="two-col">
        <div className="panel">
          <h3 className="card-title">1. Discover</h3>
          <form onSubmit={handleDiscover}>
            <div className="field">
              <label>Query</label>
              <textarea rows={3} value={query} onChange={(e) => setQuery(e.target.value)} />
            </div>
            <div className="field" style={{ maxWidth: 140 }}>
              <label>top_k</label>
              <input type="number" min={1} max={20} value={topK} onChange={(e) => setTopK(Number(e.target.value))} />
            </div>
            <button className="btn btn-primary" type="submit" disabled={discoverLoading || !apiKey}>
              {discoverLoading ? "Discovering..." : "Discover tools"}
            </button>
          </form>

          {discoverError && <div className="error-note">{discoverError}</div>}

          {discoverResult && (
            <div className="section-gap">
              <div className="loading-note">
                Resolved role <strong>{discoverResult.role}</strong> - {discoverResult.tools.length} result(s) in{" "}
                {discoverResult.latency_ms}ms
              </div>
              {discoverResult.tools.length === 0 && (
                <div className="empty-state">
                  Zero tools matched this role's RBAC + trust pre-filter - either nothing relevant is registered
                  for this role, or this is exactly the "role has no business seeing this" case working as
                  intended.
                </div>
              )}
              {discoverResult.tools.map((t) => (
                <div
                  key={t.tool_id}
                  className="card"
                  style={{ marginTop: 10, cursor: "pointer" }}
                  onClick={() => loadToolIntoInvoke(t)}
                >
                  <div className="flex-between">
                    <div style={{ fontWeight: 600 }}>{t.name}</div>
                    <SeverityBadge severity={t.risk_level} displayAs={{ critical: "high" }} />
                  </div>
                  <div className="cell-muted" style={{ margin: "6px 0" }}>
                    {t.description}
                  </div>
                  <div className="flex-row" style={{ fontSize: 12 }}>
                    <span className="cell-mono cell-muted">{t.tool_id}</span>
                    <span className="cell-muted">similarity {t.similarity.toFixed(3)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="panel">
          <h3 className="card-title">2. Invoke</h3>
          <form onSubmit={handleInvoke}>
            <div className="field">
              <label>Tool ID</label>
              <input
                type="text"
                placeholder="e.g. jira::search_issues, or shadow-diagnostics::run_diagnostic to test a deny"
                value={toolId}
                onChange={(e) => setToolId(e.target.value)}
              />
              <div className="field-hint">
                Click a discovered tool on the left to fill this in, or type any tool_id directly - including one
                that was never registered, to see it denied independently of what discovery returned.
              </div>
            </div>
            <div className="field">
              <label>Arguments (JSON)</label>
              <textarea rows={4} value={argsText} onChange={(e) => setArgsText(e.target.value)} />
            </div>
            <button className="btn btn-primary" type="submit" disabled={invokeLoading || !apiKey || !toolId}>
              {invokeLoading ? "Invoking..." : "Invoke tool"}
            </button>
          </form>

          {invokeError && <div className="error-note">{invokeError}</div>}

          {invokeHijackWarning && (
            <div className="helper-banner section-gap" style={{ borderColor: "var(--red-border)", background: "var(--red-bg)", color: "var(--red-dim)" }}>
              <strong>Response flagged for possible prompt injection</strong> (severity: {invokeHijackWarning.severity}).
              This response was still returned - see app/hijack_detection.py for why response poisoning is flagged
              rather than blocked - but it's now logged on the Threat Detection page, and correlated against your
              next invoke as a possible cross-tool hijack chain.
              <div style={{ marginTop: 8 }}>
                {invokeHijackWarning.signals.map((s, i) => (
                  <div key={i} style={{ marginTop: 4 }}>
                    <SeverityBadge severity={s.severity} /> {s.category}:{" "}
                    <span className="cell-mono">&ldquo;{s.matched_text}&rdquo;</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {invokeResult !== null && (
            <div className="section-gap">
              <div className="loading-note">Result in {invokeLatency}ms</div>
              <div className="json-block">{JSON.stringify(invokeResult, null, 2)}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
