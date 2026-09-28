import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { DownstreamAuthMode, Role, ServerDoc } from "../api/types";
import { TrustBadge } from "../components/badges/Badges";

const EMPTY_FORM = {
  server_id: "",
  label: "",
  owner: "",
  mcp_url: "",
  trust_status: "trusted" as "trusted" | "untrusted",
  default_allowed_roles: [] as string[],
  auth_mode: "none" as DownstreamAuthMode,
  auth_header_name: "X-Api-Key",
  auth_username: "",
  auth_secret: "",
};

const AUTH_MODE_LABELS: Record<DownstreamAuthMode, string> = {
  none: "none - the server does not authenticate this gateway",
  bearer: "bearer token (Authorization: Bearer ...)",
  header: "API key in a named header",
  basic: "HTTP basic (username + password)",
};

export function ServersPage() {
  const [servers, setServers] = useState<ServerDoc[] | null>(null);
  const [roles, setRoles] = useState<Role[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [submitResult, setSubmitResult] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [s, r] = await Promise.all([api.servers(), api.roles()]);
      setServers(s.servers);
      setRoles(r.roles);
    } catch (e: any) {
      setError(e.message || "Failed to load servers");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleReingest(serverId: string) {
    setBusyId(serverId);
    setError(null);
    try {
      await api.reingestServer(serverId);
      await load();
    } catch (e: any) {
      setError(`Re-ingest failed for '${serverId}': ${e.message}`);
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(serverId: string) {
    if (!confirm(`Unregister '${serverId}'? Its ingested tools will be removed from the catalog immediately.`)) return;
    setBusyId(serverId);
    setError(null);
    try {
      await api.deleteServer(serverId);
      await load();
    } catch (e: any) {
      setError(`Delete failed for '${serverId}': ${e.message}`);
    } finally {
      setBusyId(null);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setSubmitResult(null);
    setError(null);
    try {
      const res = await api.registerServer({
        server_id: form.server_id,
        label: form.label,
        owner: form.owner,
        mcp_url: form.mcp_url,
        trust_status: form.trust_status,
        default_allowed_roles: form.default_allowed_roles,
        auth: {
          mode: form.auth_mode,
          header_name: form.auth_header_name,
          username: form.auth_username,
        },
        auth_secret: form.auth_secret || undefined,
      });
      if (res.ingest_error) {
        setSubmitResult(`Registered, but catalog ingestion failed: ${res.ingest_error}`);
      } else {
        setSubmitResult(
          `Registered '${form.server_id}' - ${res.ingested_tools} tool(s) ingested` +
            (res.drifted_tools ? `, ${res.drifted_tools} quarantined for a changed definition` : "") +
            "."
        );
      }
      setForm(EMPTY_FORM);
      await load();
    } catch (e: any) {
      setError(e.message || "Failed to register server");
    } finally {
      setSubmitting(false);
    }
  }

  function toggleRole(roleId: string) {
    setForm((f) => ({
      ...f,
      default_allowed_roles: f.default_allowed_roles.includes(roleId)
        ? f.default_allowed_roles.filter((r) => r !== roleId)
        : [...f.default_allowed_roles, roleId],
    }));
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">MCP Servers</h1>
          <p className="page-subtitle">
            Only servers registered here - and marked trusted - ever have their tools ingested. Nothing else is
            reachable through discovery or invoke, no matter how well a query matches it.
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Cancel" : "Register server"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}

      {showForm && (
        <form className="panel section-gap" onSubmit={handleSubmit} style={{ marginBottom: 24 }}>
          <div className="two-col">
            <div className="field">
              <label>Server ID</label>
              <input
                type="text"
                required
                placeholder="e.g. billing-service"
                pattern="[a-z0-9][a-z0-9_-]{1,63}"
                value={form.server_id}
                onChange={(e) => setForm((f) => ({ ...f, server_id: e.target.value }))}
              />
              <div className="field-hint">lowercase, digits, hyphens/underscores - used as the catalog's server_id.</div>
            </div>
            <div className="field">
              <label>Label</label>
              <input
                type="text"
                required
                placeholder="e.g. Billing Service (Internal)"
                value={form.label}
                onChange={(e) => setForm((f) => ({ ...f, label: e.target.value }))}
              />
            </div>
          </div>
          <div className="two-col">
            <div className="field">
              <label>Owner</label>
              <input
                type="text"
                placeholder="e.g. Platform Team"
                value={form.owner}
                onChange={(e) => setForm((f) => ({ ...f, owner: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>Trust status</label>
              <select
                value={form.trust_status}
                onChange={(e) => setForm((f) => ({ ...f, trust_status: e.target.value as "trusted" | "untrusted" }))}
              >
                <option value="trusted">trusted - ingest its catalog now</option>
                <option value="untrusted">untrusted - register only, don't ingest yet</option>
              </select>
            </div>
          </div>
          <div className="field">
            <label>MCP URL (Streamable HTTP endpoint)</label>
            <input
              type="url"
              required
              placeholder="http://host:port/path/mcp"
              value={form.mcp_url}
              onChange={(e) => setForm((f) => ({ ...f, mcp_url: e.target.value }))}
            />
          </div>
          <div className="field">
            <label>Default allowed roles</label>
            {roles.map((r) => (
              <div className="checkbox-row" key={r.id}>
                <input
                  type="checkbox"
                  id={`role-${r.id}`}
                  checked={form.default_allowed_roles.includes(r.id)}
                  onChange={() => toggleRole(r.id)}
                />
                <label htmlFor={`role-${r.id}`} style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  {r.id}
                </label>
              </div>
            ))}
            <div className="field-hint">
              Applied to any tool discovered on this server that isn't already covered by a reviewed policy in
              rbac_policy.py. Leave every role unchecked to keep new tools admin-only (deny by default) until
              you assign them explicitly.
            </div>
          </div>
          <div className="field">
            <label>How this gateway authenticates to the server</label>
            <select
              value={form.auth_mode}
              onChange={(e) => setForm((f) => ({ ...f, auth_mode: e.target.value as DownstreamAuthMode }))}
            >
              {(Object.keys(AUTH_MODE_LABELS) as DownstreamAuthMode[]).map((mode) => (
                <option key={mode} value={mode}>
                  {AUTH_MODE_LABELS[mode]}
                </option>
              ))}
            </select>
            <div className="field-hint">
              Separate from the caller identity. Every downstream call already carries a signed assertion of the
              role and subject it is for, so a server can apply its own per-principal policy and log the real
              caller; this is the credential the server uses to authenticate <em>this gateway</em>.
            </div>
          </div>

          {form.auth_mode !== "none" && (
            <div className="two-col">
              {form.auth_mode === "header" && (
                <div className="field">
                  <label>Header name</label>
                  <input
                    type="text"
                    placeholder="X-Api-Key"
                    value={form.auth_header_name}
                    onChange={(e) => setForm((f) => ({ ...f, auth_header_name: e.target.value }))}
                  />
                </div>
              )}
              {form.auth_mode === "basic" && (
                <div className="field">
                  <label>Username</label>
                  <input
                    type="text"
                    value={form.auth_username}
                    onChange={(e) => setForm((f) => ({ ...f, auth_username: e.target.value }))}
                  />
                </div>
              )}
              <div className="field">
                <label>{form.auth_mode === "basic" ? "Password" : "Credential"}</label>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={form.auth_secret}
                  onChange={(e) => setForm((f) => ({ ...f, auth_secret: e.target.value }))}
                />
                <div className="field-hint">
                  Encrypted at rest with the same key that protects the LDAP bind password, and never returned by
                  the API - the table below shows only whether one is on file.
                </div>
              </div>
            </div>
          )}

          <button className="btn btn-primary" type="submit" disabled={submitting}>
            {submitting ? "Registering..." : "Register & ingest"}
          </button>
          {submitResult && <div className="loading-note">{submitResult}</div>}
        </form>
      )}

      <div className="card">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Server</th>
                <th>Owner</th>
                <th>MCP URL</th>
                <th>Trust</th>
                <th>Downstream auth</th>
                <th>Tools</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {servers?.map((s) => (
                <tr key={s.server_id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{s.label}</div>
                    <div className="cell-muted cell-mono">{s.server_id}</div>
                  </td>
                  <td className="cell-muted">{s.owner}</td>
                  <td className="cell-mono cell-muted">{s.mcp_url}</td>
                  <td>
                    <TrustBadge trust={s.trust_status} />
                  </td>
                  <td className="cell-muted">
                    {s.auth && s.auth.mode !== "none" ? (
                      <>
                        <span className="badge badge-neutral">{s.auth.mode}</span>
                        {!s.auth.secret_configured && (
                          <span className="badge badge-medium" style={{ marginLeft: 6 }}>
                            no credential
                          </span>
                        )}
                      </>
                    ) : (
                      <span className="cell-muted">identity headers only</span>
                    )}
                  </td>
                  <td>{s.tool_count}</td>
                  <td>
                    <div className="flex-row">
                      <button
                        className="btn btn-secondary btn-sm"
                        disabled={busyId === s.server_id || s.trust_status !== "trusted"}
                        onClick={() => handleReingest(s.server_id)}
                      >
                        Re-ingest
                      </button>
                      <button
                        className="btn btn-danger-outline btn-sm"
                        disabled={busyId === s.server_id}
                        onClick={() => handleDelete(s.server_id)}
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {servers && servers.length === 0 && <div className="empty-state">No servers registered yet.</div>}
        </div>
      </div>
    </div>
  );
}
