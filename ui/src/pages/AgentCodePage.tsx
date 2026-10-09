import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AGENT_EXAMPLES, type AgentExample } from "./agentCodeExamples";

function downloadFile(filename: string, contents: string) {
  const url = URL.createObjectURL(new Blob([contents], { type: "text/x-python" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="btn btn-secondary btn-sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1600);
        } catch {
          setCopied(false);
        }
      }}
    >
      {copied ? "Copied" : label}
    </button>
  );
}

function layerBadge(layer: string): string {
  if (layer.startsWith("Context")) return "badge badge-info";
  if (layer.includes("semantic")) return "badge badge-success";
  return "badge badge-neutral";
}

function AgentExampleView({ example }: { example: AgentExample }) {
  return (
    <>
      <div className="card section-gap">
        <div className="flex-between" style={{ gap: 16, alignItems: "flex-start" }}>
          <div>
            <div className="card-title">{example.label}</div>
            <p className="cell-muted" style={{ marginBottom: 0 }}>
              {example.tagline}
            </p>
          </div>
          <div className="flex-row" style={{ flexShrink: 0 }}>
            <CopyButton text={example.code} label="Copy code" />
            <button className="btn btn-primary btn-sm" onClick={() => downloadFile(example.filename, example.code)}>
              <span>⤓</span> {example.filename}
            </button>
          </div>
        </div>
        <div className="helper-banner helper-banner-neutral" style={{ marginTop: 14, marginBottom: 0 }}>
          {example.note}
        </div>
      </div>

      <div className="two-col section-gap">
        <div className="panel">
          <div style={{ fontWeight: 600, marginBottom: 8 }}>Install</div>
          <pre className="json-block" style={{ marginBottom: 14 }}>{example.install}</pre>
          <div style={{ fontWeight: 600, marginBottom: 8 }}>Configure</div>
          <div className="table-wrap">
            <table className="data-table">
              <tbody>
                {example.env.map(([name, value]) => (
                  <tr key={name}>
                    <td className="cell-mono" style={{ whiteSpace: "nowrap" }}>{name}</td>
                    <td className="cell-muted">{value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel">
          <div style={{ fontWeight: 600, marginBottom: 8 }}>What gets cached</div>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Layer</th>
                  <th>What</th>
                  <th>Key</th>
                  <th>TTL</th>
                </tr>
              </thead>
              <tbody>
                {example.cache.map((c) => (
                  <tr key={c.layer + c.what}>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <span className={layerBadge(c.layer)}>{c.layer}</span>
                    </td>
                    <td>{c.what}</td>
                    <td className="cell-mono" style={{ fontSize: 11.5 }}>{c.key}</td>
                    <td className="cell-muted" style={{ whiteSpace: "nowrap" }}>{c.ttl}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="card section-gap">
        <div className="flex-between" style={{ marginBottom: 10 }}>
          <div className="card-title" style={{ marginBottom: 0 }}>
            <code>{example.filename}</code>
          </div>
          <CopyButton text={example.code} />
        </div>
        <pre className="json-block agent-code-block">{example.code}</pre>
      </div>
    </>
  );
}

export function AgentCodePage() {
  const [params, setParams] = useSearchParams();
  const selected = AGENT_EXAMPLES.find((e) => e.id === params.get("agent")) ?? AGENT_EXAMPLES[0];

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Agent Code</h1>
          <p className="page-subtitle">
            Pre-configured, runnable agents for common enterprise data sources - each wired to the AOM SDK with
            traced runs, Context Caching for data-source lookups, and LLM Caching for model calls.
          </p>
        </div>
        <Link className="btn btn-secondary" to="/developer-sdk">
          <span>⤓</span> Get the SDK
        </Link>
      </div>

      <div className="helper-banner helper-banner-neutral">
        <div className="helper-banner-heading">The pattern every example follows</div>
        Data-source reads go through <code>client.cached_context()</code> with a key and TTL matched to how fast that
        data changes, so a repeat lookup is a Couchbase KV get. Model calls go through <code>client.complete()</code>:
        semantic matching where paraphrases should share an answer (question → SQL), <code>semantic=False</code> where
        the prompt embeds live data. Everything runs inside <code>client.run()</code>, so each question is one trace on{" "}
        <Link to="/traces">Traces</Link>, and hits show up on <Link to="/context-cache">Context Cache</Link> and{" "}
        <Link to="/llm-caching">LLM Cache</Link>.
      </div>

      <div className="agent-tabs" role="tablist" aria-label="Agent examples">
        {AGENT_EXAMPLES.map((e) => (
          <button
            key={e.id}
            role="tab"
            aria-selected={e.id === selected.id}
            className={`agent-tab${e.id === selected.id ? " active" : ""}`}
            onClick={() => setParams({ agent: e.id }, { replace: true })}
          >
            {e.label}
          </button>
        ))}
      </div>

      <AgentExampleView example={selected} />
    </div>
  );
}
