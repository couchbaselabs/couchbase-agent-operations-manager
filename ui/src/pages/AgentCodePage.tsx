import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AGENT_EXAMPLES, type AgentExample, type AgentSetup } from "./agentCodeExamples";

// Renders `backtick` spans in example copy as <code>, so the data file can
// stay plain strings.
function Inline({ text }: { text: string }) {
  return (
    <>
      {text.split("`").map((part, i) => (i % 2 === 1 ? <code key={i}>{part}</code> : <span key={i}>{part}</span>))}
    </>
  );
}

function SetupSection({ setup }: { setup: AgentSetup }) {
  return (
    <div className="card section-gap">
      <div className="card-title">{setup.title}</div>
      <p className="cell-muted" style={{ marginBottom: 6 }}>
        <Inline text={setup.intro} />
      </p>
      <ol className="setup-steps">
        {setup.steps.map((step) => (
          <li key={step.title}>
            <div className="setup-step-title">{step.title}</div>
            <p className="cell-muted">
              <Inline text={step.body} />
            </p>
            {step.fields && (
              <div className="table-wrap" style={{ marginTop: 8 }}>
                <table className="data-table">
                  <tbody>
                    {step.fields.map(([name, value]) => (
                      <tr key={name}>
                        <td style={{ whiteSpace: "nowrap", fontWeight: 600, width: 1 }}>{name}</td>
                        <td className="cell-muted">
                          <Inline text={value} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {step.code && (
              <div style={{ position: "relative", marginTop: 8 }}>
                <pre className="json-block agent-code-block" style={{ maxHeight: "none" }}>{step.code}</pre>
                <div style={{ position: "absolute", top: 8, right: 8 }}>
                  <CopyButton text={step.code} />
                </div>
              </div>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

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
      </div>

      <div className="section-gap" style={{ display: "grid", gap: 24 }}>
        <div className="panel">
          <div style={{ fontWeight: 600, marginBottom: 8 }}>Install</div>
          <pre className="json-block" style={{ marginBottom: 14 }}>{example.install}</pre>
          <div style={{ fontWeight: 600, marginBottom: 8 }}>Configure</div>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Variable</th>
                  <th>Example</th>
                  <th>What to supply</th>
                </tr>
              </thead>
              <tbody>
                {example.env.map(([name, sample, help]) => (
                  <tr key={name}>
                    <td className="cell-mono" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>{name}</td>
                    <td className="cell-mono" style={{ fontSize: 11.5, overflowWrap: "anywhere" }}>{sample}</td>
                    <td className="cell-muted">
                      <Inline text={help} />
                    </td>
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

      {example.setup && <SetupSection setup={example.setup} />}

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
        <div className="helper-banner-heading">Agent Operations Manager - Agent Data Flow and Connections</div>
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
