import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { AgentIdentity, AgentOidcConfig, AgentsResponse, ToolDoc } from "../api/types";

const KEY_BADGE: Record<string, string> = {
  active: "badge-trusted",
  rotated: "badge-medium",
  revoked: "badge-untrusted",
};

function shortTime(ts?: string | null) {
  return ts ? ts.replace("T", " ").replace("Z", "") : "-";
}

function IssuedKey({ apiKey, note, onDone }: { apiKey: string; note: string; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="helper-banner" style={{ marginBottom: 18 }}>
      <div className="helper-banner-heading">Save this key now</div>
      <div className="json-block" style={{ fontSize: 13, margin: "8px 0" }}>{apiKey}</div>
      <div style={{ marginBottom: 10 }}>{note}</div>
      <div className="flex-row">
        <button
          className="btn btn-secondary btn-sm"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(apiKey);
              setCopied(true);
            } catch {
              setCopied(false);
            }
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
        <button className="btn btn-secondary btn-sm" onClick={onDone}>
          Done
        </button>
      </div>
    </div>
  );
}

export function SettingsAgentsPage() {
  const [data, setData] = useState<AgentsResponse | null>(null);
  const [tools, setTools] = useState<ToolDoc[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<{ key: string; note: string } | null>(null);

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", role: "", owner: "", description: "", expires_at: "" });
  const [scope, setScope] = useState<string[]>([]);

  const [showOidc, setShowOidc] = useState(false);
  const [oidc, setOidc] = useState<AgentOidcConfig | null>(null);
  const [oidcProblems, setOidcProblems] = useState<string[]>([]);
  const [testToken, setTestToken] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [res, cat] = await Promise.all([api.agents(), api.catalog()]);
      setData(res);
      setTools(cat.tools);
      if (!form.role && res.roles.length) setForm((f) => ({ ...f, role: res.roles[0].id }));
    } catch (e: any) {
      setError(e.message || "Failed to load agents");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function openOidc() {
    setShowOidc((v) => !v);
    if (!oidc) {
      try {
        const res = await api.agentOidcConfig();
        setOidc(res.config);
        setOidcProblems(res.problems);
      } catch (e: any) {
        setError(e.message || "Failed to load JWT settings");
      }
    }
  }

  const roleTools = tools.filter((t) => (t.allowed_roles || []).includes(form.role));

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.createAgent({
        name: form.name,
        role: form.role,
        owner: form.owner,
        description: form.description,
        allowed_tools: scope,
        expires_at: form.expires_at || null,
      });
      setIssued({ key: res.api_key, note: res.notice });
      setForm({ name: "", role: form.role, owner: "", description: "", expires_at: "" });
      setScope([]);
      setShowForm(false);
      await load();
    } catch (e: any) {
      setError(e.message || "Could not issue that agent");
    } finally {
      setBusy(false);
    }
  }

  async function handleRotate(agent: AgentIdentity, immediate: boolean) {
    if (
      immediate &&
      !confirm(
        `Revoke ${agent.name}'s current key immediately?\n\n` +
          `Anything still using it stops working on its very next request. Use the normal rotation ` +
          `unless you believe this key has leaked.`
      )
    )
      return;
    setBusy(true);
    try {
      const res = await api.rotateAgentKey(agent.agent_id, immediate);
      setIssued({
        key: res.api_key,
        note: immediate
          ? "The previous key was revoked immediately."
          : `The previous key keeps working until ${shortTime(res.previous_key_valid_until)}, then expires on its own.`,
      });
      await load();
    } catch (e: any) {
      setError(e.message || "Rotation failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleRevoke(agent: AgentIdentity) {
    if (!confirm(`Revoke "${agent.name}"? Every key it holds stops working on the next request.`)) return;
    setBusy(true);
    try {
      await api.revokeAgent(agent.agent_id);
      setNotice(`"${agent.name}" revoked. Its keys stop working immediately.`);
      await load();
    } catch (e: any) {
      setError(e.message || "Revocation failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveOidc() {
    if (!oidc) return;
    setBusy(true);
    try {
      const res = await api.saveAgentOidcConfig(oidc);
      setOidc(res.config);
      setOidcProblems(res.problems);
      setNotice(res.problems.length ? "Saved, but the configuration is incomplete." : "JWT settings saved.");
      await load();
    } catch (e: any) {
      setError(e.message || "Could not save JWT settings");
    } finally {
      setBusy(false);
    }
  }

  async function handleTestToken() {
    setTestResult(null);
    try {
      const res = await api.testAgentOidc(testToken);
      setTestResult(
        res.valid
          ? `Valid. Subject "${res.subject}" maps to role "${res.role}".`
          : `Rejected: ${res.reason}`
      );
    } catch (e: any) {
      setTestResult(e.message || "Test failed");
    }
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Agent Identities</h1>
          <p className="page-subtitle">
            Who may call this gateway. Agents are records rather than strings in a file: each has an owner, a
            role, an optional end date and an optional scope, and each can be rotated or revoked without a
            restart. Keys are stored only as a hash, so a lost key is rotated rather than looked up.
          </p>
        </div>
        <div className="flex-row">
          <button className="btn btn-secondary" onClick={openOidc}>
            {showOidc ? "Hide JWT" : "JWT / IdP"}
          </button>
          <button className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
            {showForm ? "Cancel" : "Issue agent"}
          </button>
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}
      {notice && <div className="helper-banner helper-banner-neutral">{notice}</div>}
      {issued && <IssuedKey apiKey={issued.key} note={issued.note} onDone={() => setIssued(null)} />}

      {showOidc && oidc && (
        <div className="panel section-gap" style={{ marginBottom: 24 }}>
          <h2 style={{ fontSize: 15, margin: "0 0 12px 0" }}>Federated identity (OIDC)</h2>
          <div className="field-hint" style={{ marginBottom: 14 }}>
            Accept bearer tokens your own identity provider issues, instead of keys this appliance issues. AOM
            validates the signature, issuer, audience and expiry against the provider&rsquo;s JWKS, then maps a
            claim to one of its roles. It never issues tokens of its own &mdash; becoming an authorization server
            would be a large surface for something your IdP already does better.
          </div>

          {oidcProblems.length > 0 && (
            <div className="error-note" style={{ marginBottom: 12 }}>
              {oidcProblems.map((p, i) => (
                <div key={i}>{p}</div>
              ))}
            </div>
          )}

          <div className="two-col">
            <div className="field">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={oidc.enabled}
                  onChange={(e) => setOidc({ ...oidc, enabled: e.target.checked })}
                />
                <span>Accept JWTs</span>
              </label>
            </div>
            <div className="field">
              <label>Issuer (iss)</label>
              <input
                type="text"
                value={oidc.issuer}
                placeholder="https://login.example.com/tenant"
                onChange={(e) => setOidc({ ...oidc, issuer: e.target.value })}
              />
            </div>
          </div>
          <div className="two-col">
            <div className="field">
              <label>JWKS URI</label>
              <input
                type="text"
                value={oidc.jwks_uri}
                placeholder="https://login.example.com/tenant/discovery/v2.0/keys"
                onChange={(e) => setOidc({ ...oidc, jwks_uri: e.target.value })}
              />
            </div>
            <div className="field">
              <label>Audience (aud)</label>
              <input
                type="text"
                value={oidc.audience}
                placeholder="api://agent-operations-manager"
                onChange={(e) => setOidc({ ...oidc, audience: e.target.value })}
              />
              <div className="field-hint">
                Required. Without it, a token the same issuer minted for any other service would be accepted here.
              </div>
            </div>
          </div>
          <div className="two-col">
            <div className="field">
              <label>Role claim</label>
              <input
                type="text"
                value={oidc.role_claim}
                onChange={(e) => setOidc({ ...oidc, role_claim: e.target.value })}
              />
            </div>
            <div className="field">
              <label>Default role when unmapped</label>
              <select
                value={oidc.default_role}
                onChange={(e) => setOidc({ ...oidc, default_role: e.target.value })}
              >
                <option value="">refuse (recommended)</option>
                {(data?.roles || []).map((r) => (
                  <option key={r.id} value={r.id}>{r.id}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="field">
            <label>Claim value &rarr; role, one per line as value=role</label>
            <textarea
              rows={3}
              value={Object.entries(oidc.role_map).map(([k, v]) => `${k}=${v}`).join("\n")}
              placeholder={"agents.support=support_agent\nagents.finance=finance_analyst"}
              onChange={(e) => {
                const map: Record<string, string> = {};
                e.target.value.split("\n").forEach((line) => {
                  const [k, v] = line.split("=");
                  if (k?.trim() && v?.trim()) map[k.trim()] = v.trim();
                });
                setOidc({ ...oidc, role_map: map });
              }}
            />
            <div className="field-hint">
              Explicit by design. Trusting whatever string a token calls a role would let a group created for
              something else quietly become an administrator here.
            </div>
          </div>

          <div className="flex-row">
            <button className="btn btn-primary" onClick={handleSaveOidc} disabled={busy}>
              Save JWT settings
            </button>
          </div>

          <div className="field" style={{ marginTop: 16 }}>
            <label>Test a real token</label>
            <textarea rows={3} value={testToken} onChange={(e) => setTestToken(e.target.value)} placeholder="eyJ..." />
            <div className="flex-row">
              <button className="btn btn-secondary btn-sm" onClick={handleTestToken} disabled={!testToken.trim()}>
                Validate
              </button>
            </div>
            {testResult && <div className="field-hint" style={{ marginTop: 8 }}>{testResult}</div>}
            <div className="field-hint">
              An IdP integration never tested against a token the IdP actually mints is a guess, and it fails as
              every agent getting 401 at once.
            </div>
          </div>
        </div>
      )}

      {showForm && data && (
        <form className="panel section-gap" onSubmit={handleCreate} style={{ marginBottom: 24 }}>
          <div className="two-col">
            <div className="field">
              <label>Name</label>
              <input
                type="text"
                required
                value={form.name}
                placeholder="e.g. Support triage bot"
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </div>
            <div className="field">
              <label>Owner</label>
              <input
                type="text"
                value={form.owner}
                placeholder="e.g. Support Engineering"
                onChange={(e) => setForm({ ...form, owner: e.target.value })}
              />
              <div className="field-hint">Who to ask before turning this key off.</div>
            </div>
          </div>
          <div className="two-col">
            <div className="field">
              <label>Role</label>
              <select
                value={form.role}
                onChange={(e) => {
                  setForm({ ...form, role: e.target.value });
                  setScope([]);
                }}
              >
                {data.roles.map((r) => (
                  <option key={r.id} value={r.id}>{r.id} &mdash; {r.description}</option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Expires (optional)</label>
              <input
                type="datetime-local"
                value={form.expires_at}
                onChange={(e) =>
                  setForm({ ...form, expires_at: e.target.value ? `${e.target.value}:00Z`.replace(" ", "T") : "" })
                }
              />
              <div className="field-hint">
                A credential issued for a pilot should stop working when the pilot does.
              </div>
            </div>
          </div>
          <div className="field">
            <label>Restrict to specific tools (optional)</label>
            <div style={{ maxHeight: 190, overflowY: "auto", marginTop: 6 }}>
              {roleTools.length === 0 ? (
                <div className="cell-muted">No tools are currently available to this role.</div>
              ) : (
                roleTools.map((t) => (
                  <div className="checkbox-row" key={t.tool_id}>
                    <input
                      type="checkbox"
                      id={`scope-${t.tool_id}`}
                      checked={scope.includes(t.tool_id)}
                      onChange={() =>
                        setScope((sc) =>
                          sc.includes(t.tool_id) ? sc.filter((x) => x !== t.tool_id) : [...sc, t.tool_id]
                        )
                      }
                    />
                    <label htmlFor={`scope-${t.tool_id}`} style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                      <span className="cell-mono">{t.tool_id}</span>
                    </label>
                  </div>
                ))
              )}
            </div>
            <div className="field-hint">
              Leave everything unchecked for the whole role. A scope can only narrow what the role allows &mdash;
              the tool&rsquo;s own RBAC check still runs afterwards, so this can never widen access.
            </div>
          </div>
          <button className="btn btn-primary" type="submit" disabled={busy || !form.name}>
            {busy ? "Issuing..." : "Issue agent & key"}
          </button>
        </form>
      )}

      {data && (
        <>
          {data.agents.some((a) => a.seeded) && (
            <div className="helper-banner helper-banner-neutral" style={{ marginBottom: 18 }}>
              Some identities below are the demo keys this appliance seeds so it works on first boot. They are
              published in the README and <span className="mono">.env.example</span> &mdash; revoke them before
              this deployment is reachable by anything you care about.
            </div>
          )}

          {data.agents.length === 0 ? (
            <div className="card empty-state">
              No agents issued yet. Callers are still authenticating with whatever seeded keys exist; issue a real
              agent to get an owner, an expiry and a revoke button.
            </div>
          ) : (
            data.agents.map((a) => (
              <div className="panel section-gap" key={a.agent_id} style={{ marginBottom: 14, opacity: a.status === "revoked" ? 0.6 : 1 }}>
                <div className="flex-between" style={{ marginBottom: 8 }}>
                  <div>
                    <span style={{ fontWeight: 600, fontSize: 15 }}>{a.name}</span>{" "}
                    <span className="badge badge-neutral">{a.role}</span>{" "}
                    {a.status === "revoked" && <span className="badge badge-untrusted">revoked</span>}
                    {a.expired && <span className="badge badge-medium">expired</span>}
                    {a.seeded && <span className="badge badge-medium">seeded demo</span>}
                    <div className="cell-muted" style={{ fontSize: 12, marginTop: 4 }}>
                      {a.owner && <>{a.owner} &middot; </>}
                      issued {shortTime(a.created_at)}
                      {a.created_by && <> by {a.created_by}</>}
                      {" · "}
                      {a.last_used_at ? `last used ${shortTime(a.last_used_at)} (${a.use_count}×)` : "never used"}
                      {a.expires_at && <> &middot; expires {shortTime(a.expires_at)}</>}
                    </div>
                  </div>
                  <div className="flex-row">
                    {a.status !== "revoked" && (
                      <>
                        <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => handleRotate(a, false)}>
                          Rotate
                        </button>
                        <button className="btn btn-danger-outline btn-sm" disabled={busy} onClick={() => handleRotate(a, true)}>
                          Rotate &amp; revoke now
                        </button>
                        <button className="btn btn-danger-outline btn-sm" disabled={busy} onClick={() => handleRevoke(a)}>
                          Revoke agent
                        </button>
                      </>
                    )}
                  </div>
                </div>

                {a.description && <div className="field-hint" style={{ marginBottom: 8 }}>{a.description}</div>}

                {a.allowed_tools.length > 0 && (
                  <div className="field-hint" style={{ marginBottom: 8 }}>
                    Scoped to {a.allowed_tools.length} tool(s):{" "}
                    <span className="cell-mono">{a.allowed_tools.join(", ")}</span>
                  </div>
                )}

                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Key</th>
                        <th>Status</th>
                        <th>Issued</th>
                        <th>Valid until</th>
                      </tr>
                    </thead>
                    <tbody>
                      {a.keys.map((k) => (
                        <tr key={k.key_prefix + k.created_at}>
                          <td className="cell-mono">{k.key_prefix}</td>
                          <td>
                            <span className={`badge ${KEY_BADGE[k.status] || "badge-neutral"}`}>{k.status}</span>
                          </td>
                          <td className="cell-muted cell-mono">{shortTime(k.created_at)}</td>
                          <td className="cell-muted cell-mono">{k.expires_at ? shortTime(k.expires_at) : "no expiry"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))
          )}
        </>
      )}
    </div>
  );
}
