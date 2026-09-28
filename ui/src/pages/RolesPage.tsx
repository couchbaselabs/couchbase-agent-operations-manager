import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { Role, ServerDoc, ToolDoc } from "../api/types";
import { SeverityBadge, TrustBadge } from "../components/badges/Badges";

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
  const [roles, setRoles] = useState<Role[] | null>(null);
  const [tools, setTools] = useState<ToolDoc[]>([]);
  const [servers, setServers] = useState<ServerDoc[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      const [r, c, s] = await Promise.all([api.roles(), api.catalog(), api.servers()]);
      setRoles(r.roles);
      setTools(c.tools);
      setServers(s.servers);
    } catch (e: any) {
      setError(e.message || "Failed to load roles");
    }
  }, []);

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
            Roles are code-reviewed config (operations-manager/app/rbac_policy.py), not something added through this UI -
            the same way a security team would want role definitions to go through review, not a form.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={load}>
          Refresh
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}

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
              <span className="catalog-group-count">
                {total} tool{total === 1 ? "" : "s"}
              </span>
            </header>

            <div className="catalog-group-meta">{role.description}</div>

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
