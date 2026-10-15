import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { Role, RoleUsage, ServerDoc, ToolDoc } from "../api/types";
import { SeverityBadge, TrustBadge } from "../components/badges/Badges";
import { useAuth } from "../auth/AuthContext";

const ROLE_ID_PATTERN = "[a-z][a-z0-9_]{1,47}";

/** One vendor/system's slice of a role's accessible tools - the same
 *  grouping the Tool Catalog uses per-server, just scoped to whichever
 *  tools list this role in allowed_roles. */
interface VendorGroup {
  server_id: string;
  label: string;
  owner: string | null;
  tools: ToolDoc[];
}

export function RolesPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [roles, setRoles] = useState<Role[] | null>(null);
  const [tools, setTools] = useState<ToolDoc[]>([]);
  const [servers, setServers] = useState<ServerDoc[]>([]);
  const [usage, setUsage] = useState<Record<string, RoleUsage>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  // Admin-only role management (add / edit description / delete).
  const [showForm, setShowForm] = useState(false);
  const [newId, setNewId] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDescription, setEditDescription] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [r, c, s] = await Promise.all([api.roles(), api.catalog(), api.servers()]);
      setRoles(r.roles);
      setTools(c.tools);
      setServers(s.servers);
      if (isAdmin) {
        try {
          const u = await api.rolesUsage();
          setUsage(u.usage);
        } catch {
          // usage counts are decoration; the page works without them
        }
      }
    } catch (e: any) {
      setError(e.message || "Failed to load roles");
    }
  }, [isAdmin]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      await api.createRole(newId.trim().toLowerCase(), newDescription.trim());
      setNotice(
        `Role '${newId.trim().toLowerCase()}' created. Grant it on tools (via a server's reviewed tool policy or default roles), ` +
          "RAG applications, and issue it an API key from Settings → Agent Identities."
      );
      setNewId("");
      setNewDescription("");
      setShowForm(false);
      await load();
    } catch (e: any) {
      setError(e.message || "Could not create the role");
    } finally {
      setSaving(false);
    }
  }

  function startEdit(role: Role) {
    setEditingId(role.id);
    setEditDescription(role.description);
    setNotice(null);
  }

  async function handleSaveEdit(roleId: string) {
    setBusyId(roleId);
    setError(null);
    try {
      await api.updateRole(roleId, editDescription.trim());
      setEditingId(null);
      await load();
    } catch (e: any) {
      setError(e.message || "Could not update the role");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(role: Role) {
    const u = usage[role.id];
    const inUse = u && (u.tools > 0 || u.servers > 0 || u.rag_apps > 0);
    const msg = inUse
      ? `'${role.id}' is still granted on ${u.tools} tool(s), ${u.servers} server(s) and ${u.rag_apps} RAG application(s). ` +
        "Delete it anyway? Those grants will be removed - callers holding this role lose that access immediately."
      : `Delete role '${role.id}'? Agent identities and API keys issued for it will stop authorizing.`;
    if (!confirm(msg)) return;
    setBusyId(role.id);
    setError(null);
    try {
      await api.deleteRole(role.id, !!inUse);
      setNotice(`Role '${role.id}' deleted.`);
      await load();
    } catch (e: any) {
      setError(e.message || "Could not delete the role");
    } finally {
      setBusyId(null);
    }
  }

  useEffect(() => {
    load();
  }, [load]);

  const serverById = useMemo(() => new Map(servers.map((s) => [s.server_id, s])), [servers]);

  /** For one role, every accessible tool grouped by the vendor/system that
   *  provides it - mirrors CatalogPage's per-server grouping, just with
   *  the role filter applied first. Tools are shown regardless of
   *  quarantine state (this is a transparency view of what's *configured*
   *  for the role, same as the Tool Catalog doesn't hide quarantined
   *  tools either) - the Trust column is what tells you whether a given
   *  tool is actually invokable right now. */
  function vendorGroupsForRole(roleId: string): VendorGroup[] {
    const buckets = new Map<string, ToolDoc[]>();
    for (const t of tools) {
      if (!(t.allowed_roles || []).includes(roleId)) continue;
      const list = buckets.get(t.server_id);
      if (list) list.push(t);
      else buckets.set(t.server_id, [t]);
    }
    return Array.from(buckets.entries())
      .map(([server_id, list]) => {
        const doc = serverById.get(server_id);
        return {
          server_id,
          label: doc?.label || server_id,
          owner: doc?.owner ?? null,
          tools: [...list].sort((a, b) => a.name.localeCompare(b.name)),
        };
      })
      .sort((a, b) => a.label.localeCompare(b.label));
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Roles &amp; RBAC</h1>
          <p className="page-subtitle">
            Every discover/invoke is authorized against these roles in Couchbase. Built-in roles are seeded from
            operations-manager/app/rbac_policy.py; admins add and edit roles here, and every change is written to the
            Audit Log.
          </p>
        </div>
        <div className="flex-row" style={{ gap: 8 }}>
          {isAdmin && (
            <button className="btn btn-primary" onClick={() => setShowForm((v) => !v)}>
              {showForm ? "Cancel" : "Add role"}
            </button>
          )}
          <button className="btn btn-secondary" onClick={load}>
            Refresh
          </button>
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}
      {notice && <div className="helper-banner helper-banner-neutral">{notice}</div>}

      {isAdmin && showForm && (
        <form className="panel section-gap" onSubmit={handleCreate} style={{ marginBottom: 22 }}>
          <div className="two-col">
            <div className="field">
              <label>Role ID</label>
              <input
                type="text"
                required
                pattern={ROLE_ID_PATTERN}
                placeholder="e.g. data_engineer"
                value={newId}
                onChange={(e) => setNewId(e.target.value)}
              />
              <div className="field-hint">
                lowercase letters, digits and underscores; immutable once created - it becomes the value in every
                tool's allowed_roles and in the audit log.
              </div>
            </div>
            <div className="field">
              <label>Description</label>
              <input
                type="text"
                required
                maxLength={300}
                placeholder="Who holds this role and what it is for"
                value={newDescription}
                onChange={(e) => setNewDescription(e.target.value)}
              />
            </div>
          </div>
          <div className="field-hint" style={{ marginBottom: 12 }}>
            A new role starts with no tool access (deny by default). Grant tools by registering or re-registering a
            server with it in <em>default allowed roles</em> or a reviewed per-tool policy, allow it on RAG
            applications, and issue it a bearer key from Settings → Agent Identities.
          </div>
          <button className="btn btn-primary" type="submit" disabled={saving || !newId || !newDescription.trim()}>
            {saving ? "Creating..." : "Create role"}
          </button>
        </form>
      )}

      <div className="helper-banner helper-banner-neutral">
        <div className="helper-banner-heading">How it works</div>
        Each role authenticates via a bearer API key (<code>Authorization: Bearer &lt;api_key&gt;</code>), set as an
        environment variable on the operations-manager container - see <code>.env.example</code>. Raw keys are never
        returned by any API in this appliance; only a hash is stored, and the audit log only ever shows the last 4
        characters.
      </div>

      {roles?.map((role) => {
        const groups = vendorGroupsForRole(role.id);
        const total = groups.reduce((n, g) => n + g.tools.length, 0);
        const isCollapsed = !!collapsed[role.id];
        return (
          <section className="card catalog-group" key={role.id}>
            <header className="catalog-group-header">
              <button
                type="button"
                className="catalog-group-toggle"
                aria-expanded={!isCollapsed}
                onClick={() => setCollapsed((c) => ({ ...c, [role.id]: !c[role.id] }))}
                title={isCollapsed ? "Expand" : "Collapse"}
              >
                {isCollapsed ? "▸" : "▾"}
              </button>
              <h2 className="catalog-group-title cell-mono">{role.id}</h2>
              {role.builtin ? (
                <span className="badge badge-neutral" title="Seeded by rbac_policy.py - editable, not deletable">
                  built-in
                </span>
              ) : (
                <span className="badge badge-success" title="Added through this page">
                  custom
                </span>
              )}
              <span className="catalog-group-count">
                {total} tool{total === 1 ? "" : "s"}
                {usage[role.id]
                  ? ` · ${usage[role.id].servers} server${usage[role.id].servers === 1 ? "" : "s"} · ${usage[role.id].rag_apps} RAG app${usage[role.id].rag_apps === 1 ? "" : "s"}`
                  : ""}
              </span>
              {isAdmin && editingId !== role.id && (
                <div className="flex-row" style={{ gap: 6, marginLeft: "auto" }}>
                  <button className="btn btn-secondary btn-sm" type="button" onClick={() => startEdit(role)}>
                    Edit
                  </button>
                  {!role.builtin && (
                    <button
                      className="btn btn-danger-outline btn-sm"
                      type="button"
                      disabled={busyId === role.id}
                      onClick={() => handleDelete(role)}
                    >
                      Delete
                    </button>
                  )}
                </div>
              )}
            </header>

            {editingId === role.id ? (
              <div className="flex-row" style={{ gap: 8, margin: "6px 0 12px 0", alignItems: "center" }}>
                <input
                  type="text"
                  maxLength={300}
                  style={{ flex: 1 }}
                  value={editDescription}
                  onChange={(e) => setEditDescription(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") setEditingId(null);
                  }}
                />
                <button
                  className="btn btn-primary btn-sm"
                  type="button"
                  disabled={busyId === role.id || !editDescription.trim()}
                  onClick={() => handleSaveEdit(role.id)}
                >
                  Save
                </button>
                <button className="btn btn-secondary btn-sm" type="button" onClick={() => setEditingId(null)}>
                  Cancel
                </button>
              </div>
            ) : (
              <div className="catalog-group-meta">{role.description}</div>
            )}

            {!isCollapsed &&
              (total === 0 ? (
                <div className="card empty-state" style={{ marginLeft: 0 }}>
                  No tool currently lists this role in its allowed roles.
                </div>
              ) : (
                groups.map((g) => (
                  <div key={g.server_id} style={{ marginBottom: 18, marginLeft: 24 }}>
                    <div className="flex-row" style={{ gap: 8, marginBottom: 6, alignItems: "baseline" }}>
                      <span style={{ fontWeight: 600, fontSize: 13 }}>{g.label}</span>
                      <span className="cell-mono cell-muted" style={{ fontSize: 11.5 }}>
                        {g.server_id}
                      </span>
                      {g.owner && (
                        <span className="cell-muted" style={{ fontSize: 11.5 }}>
                          · {g.owner}
                        </span>
                      )}
                    </div>
                    <div className="table-wrap">
                      <table className="data-table catalog-table">
                        <colgroup>
                          <col style={{ width: "62%" }} />
                          <col style={{ width: "19%" }} />
                          <col style={{ width: "19%" }} />
                        </colgroup>
                        <thead>
                          <tr>
                            <th>Tool</th>
                            <th>Risk</th>
                            <th>Trust</th>
                          </tr>
                        </thead>
                        <tbody>
                          {g.tools.map((t) => (
                            <tr key={t.tool_id}>
                              <td>
                                <div style={{ fontWeight: 600 }}>{t.name}</div>
                                <div className="cell-muted" style={{ maxWidth: 460 }}>
                                  {t.description}
                                </div>
                              </td>
                              <td>
                                <SeverityBadge severity={t.risk_level} displayAs={{ critical: "high" }} />
                              </td>
                              <td>
                                <TrustBadge trust={t.trust_status} />
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                ))
              ))}
          </section>
        );
      })}
    </div>
  );
}
