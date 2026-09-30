import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { AuthRole, AuthUser } from "../api/types";

const EMPTY_FORM = {
  username: "",
  password: "",
  role: "user",
  must_change_password: true,
};

export function SettingsAccountsPage() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<AuthUser[] | null>(null);
  const [roles, setRoles] = useState<AuthRole[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [u, r] = await Promise.all([api.authUsers(), api.authRoles()]);
      setUsers(u.users);
      setRoles(r.roles);
    } catch (e: any) {
      setError(e.message || "Failed to load accounts");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await api.createAuthUser(form);
      setForm(EMPTY_FORM);
      setShowForm(false);
      await load();
    } catch (e: any) {
      setError(e.message || "Failed to create account");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRoleChange(username: string, role: string) {
    setBusy(username);
    setError(null);
    try {
      await api.updateAuthUser(username, { role });
      await load();
    } catch (e: any) {
      setError(`Could not update '${username}': ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  async function handleToggleActive(u: AuthUser) {
    setBusy(u.username);
    setError(null);
    try {
      await api.updateAuthUser(u.username, { active: !u.active });
      await load();
    } catch (e: any) {
      setError(`Could not update '${u.username}': ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  async function handleResetPassword(username: string) {
    const pw = window.prompt(`New temporary password for '${username}' (at least 8 characters):`);
    if (!pw) return;
    setBusy(username);
    setError(null);
    try {
      await api.updateAuthUser(username, { password: pw, must_change_password: true });
      await load();
    } catch (e: any) {
      setError(`Could not reset password for '${username}': ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  async function handleDelete(username: string) {
    if (!confirm(`Delete local account '${username}'? This cannot be undone.`)) return;
    setBusy(username);
    setError(null);
    try {
      await api.deleteAuthUser(username);
      await load();
    } catch (e: any) {
      setError(`Could not delete '${username}': ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Accounts &amp; Roles</h1>
          <p className="page-subtitle">
            Local sign-in accounts for this dashboard - separate from the agent identities on the Roles &amp; RBAC
            page, which authenticate MCP tool calls with an API key, not a person opening this UI.
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Cancel" : "Add local account"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}

      {showForm && (
        <form className="panel section-gap" onSubmit={handleSubmit} style={{ marginBottom: 24 }}>
          <div className="two-col">
            <div className="field">
              <label>Username</label>
              <input
                type="text"
                required
                pattern="[a-zA-Z0-9._-]{2,64}"
                value={form.username}
                onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>Initial password</label>
              <input
                type="password"
                required
                minLength={8}
                value={form.password}
                onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
              />
            </div>
          </div>
          <div className="two-col">
            <div className="field">
              <label>Role</label>
              <select value={form.role} onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}>
                {roles.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.id} - {r.description}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>&nbsp;</label>
              <div className="checkbox-row">
                <input
                  type="checkbox"
                  id="force-reset"
                  checked={form.must_change_password}
                  onChange={(e) => setForm((f) => ({ ...f, must_change_password: e.target.checked }))}
                />
                <label htmlFor="force-reset" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  Require a password change at first login
                </label>
              </div>
            </div>
          </div>
          <button className="btn btn-primary" type="submit" disabled={submitting}>
            {submitting ? "Creating..." : "Create account"}
          </button>
        </form>
      )}

      <div className="card">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Account</th>
                <th>Source</th>
                <th>Role</th>
                <th>Status</th>
                <th>Last login</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {users?.map((u) => (
                <tr key={u.username}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{u.username}</div>
                    {u.must_change_password && <span className="badge badge-medium">password change required</span>}
                  </td>
                  <td>
                    <span className={`badge ${u.source === "local" ? "badge-neutral" : "badge-info"}`}>{u.source}</span>
                  </td>
                  <td>
                    <select
                      value={u.role}
                      disabled={busy === u.username || (u.username === "admin" && u.role === "admin")}
                      onChange={(e) => handleRoleChange(u.username, e.target.value)}
                    >
                      {roles.map((r) => (
                        <option key={r.id} value={r.id}>
                          {r.id}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <span className={`badge ${u.active ? "badge-success" : "badge-danger"}`}>
                      {u.active ? "active" : "disabled"}
                    </span>
                  </td>
                  <td className="cell-muted">{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}</td>
                  <td>
                    <div className="flex-row">
                      {u.source === "local" && (
                        <button
                          className="btn btn-secondary btn-sm"
                          disabled={busy === u.username}
                          onClick={() => handleResetPassword(u.username)}
                        >
                          Reset password
                        </button>
                      )}
                      <button
                        className="btn btn-secondary btn-sm"
                        disabled={busy === u.username || u.username === me?.username || u.username === "admin"}
                        onClick={() => handleToggleActive(u)}
                      >
                        {u.active ? "Disable" : "Enable"}
                      </button>
                      <button
                        className="btn btn-danger-outline btn-sm"
                        disabled={busy === u.username || u.username === me?.username || u.username === "admin"}
                        onClick={() => handleDelete(u.username)}
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {users && users.length === 0 && <div className="empty-state">No accounts yet.</div>}
        </div>
      </div>
    </div>
  );
}
