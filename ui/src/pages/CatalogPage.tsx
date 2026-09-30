import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { ServerDoc, ToolDoc } from "../api/types";
import { SeverityBadge, TrustBadge } from "../components/badges/Badges";

/** One server's slice of the catalog. The catalog is grouped and sorted by
 *  server because that is the unit an operator actually reasons about -
 *  "what can this server do, and do I trust it" - and because the server
 *  column then stops repeating itself on every row. */
interface ServerGroup {
  server_id: string;
  label: string;
  owner: string | null;
  mcp_url: string | null;
  registered: boolean;
  trust_status: string;
  tools: ToolDoc[];
}

export function CatalogPage() {
  const [tools, setTools] = useState<ToolDoc[] | null>(null);
  const [servers, setServers] = useState<ServerDoc[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [serverFilter, setServerFilter] = useState("all");
  const [roleFilter, setRoleFilter] = useState("all");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      // Server docs come along for the ride so each section can be headed
      // by the server's real label, owner and trust status rather than its
      // bare server_id.
      const [cat, srv] = await Promise.all([api.catalog(), api.servers()]);
      setTools(cat.tools);
      setServers(srv.servers);
    } catch (e: any) {
      setError(e.message || "Failed to load catalog");
    }
  }, []);

  async function handleQuarantine(toolId: string) {
    setBusyId(toolId);
    try {
      await api.quarantineTool(toolId);
      await load();
    } catch (e: any) {
      setError(`Quarantine failed for '${toolId}': ${e.message}`);
    } finally {
      setBusyId(null);
    }
  }

  async function handleRelease(toolId: string) {
    setBusyId(toolId);
    try {
      await api.releaseTool(toolId);
      await load();
    } catch (e: any) {
      setError(`Release failed for '${toolId}': ${e.message}`);
    } finally {
      setBusyId(null);
    }
  }

  useEffect(() => {
    load();
  }, [load]);

  const serverIds = useMemo(
    () => Array.from(new Set((tools || []).map((t) => t.server_id))).sort(),
    [tools]
  );
  const roles = useMemo(
    () => Array.from(new Set((tools || []).flatMap((t) => t.allowed_roles || []))).sort(),
    [tools]
  );

  const filtered = useMemo(
    () =>
      (tools || []).filter((t) => {
        if (serverFilter !== "all" && t.server_id !== serverFilter) return false;
        if (roleFilter !== "all" && !(t.allowed_roles || []).includes(roleFilter)) return false;
        if (search && !`${t.name} ${t.description} ${t.tool_id}`.toLowerCase().includes(search.toLowerCase()))
          return false;
        return true;
      }),
    [tools, serverFilter, roleFilter, search]
  );

  /** Group by server, sorted by server label; tools sorted by name within
   *  each group. This is the default ordering - there is no unsorted view. */
  const groups: ServerGroup[] = useMemo(() => {
    const byId = new Map(servers.map((s) => [s.server_id, s]));
    const buckets = new Map<string, ToolDoc[]>();
    for (const t of filtered) {
      const list = buckets.get(t.server_id);
      if (list) list.push(t);
      else buckets.set(t.server_id, [t]);
    }
    return Array.from(buckets.entries())
      .map(([server_id, list]) => {
        const doc = byId.get(server_id);
        return {
          server_id,
          label: doc?.label || server_id,
          owner: doc?.owner ?? null,
          mcp_url: doc?.mcp_url ?? null,
          // A tool whose owning server is no longer registered is worth
          // seeing rather than silently hiding - it can't be invoked, and
          // the header says so.
          registered: !!doc,
          trust_status: doc?.trust_status || "unregistered",
          tools: [...list].sort((a, b) => a.name.localeCompare(b.name)),
        };
      })
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [filtered, servers]);

  const totalShown = groups.reduce((n, g) => n + g.tools.length, 0);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Tool Catalog</h1>
          <p className="page-subtitle">
            Full transparency view of everything ingested into Couchbase's tool registry - what discovery can ever
            return, for any role - grouped by the server that provides it. Quarantined tools were flagged by{" "}
            <Link to="/threat-detection">Threat Detection</Link>.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={load}>
          Refresh
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}

      <div className="flex-row" style={{ marginBottom: 16, flexWrap: "wrap", gap: 10 }}>
        <input
          type="text"
          placeholder="Search name, description, tool_id..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{
            background: "var(--bg-elevated)",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: "8px 12px",
            minWidth: 260,
            color: "var(--text)",
          }}
        />
        <select
          value={serverFilter}
          onChange={(e) => setServerFilter(e.target.value)}
          style={{ background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px" }}
        >
          <option value="all">All servers</option>
          {serverIds.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          value={roleFilter}
          onChange={(e) => setRoleFilter(e.target.value)}
          style={{ background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px" }}
        >
          <option value="all">All roles</option>
          {roles.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        {tools && (
          <span className="cell-muted" style={{ fontSize: 12.5, alignSelf: "center", marginLeft: "auto" }}>
            {totalShown} tool{totalShown === 1 ? "" : "s"} across {groups.length} server
            {groups.length === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {tools && groups.length === 0 && (
        <div className="card empty-state">
          {tools.length === 0
            ? "No tools ingested yet - register a trusted server first."
            : "No tools match these filters."}
        </div>
      )}

      {groups.map((g) => {
        const isCollapsed = !!collapsed[g.server_id];
        const quarantined = g.tools.filter((t) => t.trust_status === "quarantined").length;
        return (
          <section className="card catalog-group" key={g.server_id}>
            <header className="catalog-group-header">
              <button
                type="button"
                className="catalog-group-toggle"
                aria-expanded={!isCollapsed}
                onClick={() => setCollapsed((c) => ({ ...c, [g.server_id]: !c[g.server_id] }))}
                title={isCollapsed ? "Expand" : "Collapse"}
              >
                {isCollapsed ? "▸" : "▾"}
              </button>
              <span className={`status-dot${g.trust_status === "trusted" ? "" : " down"}`} />
              <h2 className="catalog-group-title">{g.label}</h2>
              <span className="cell-mono catalog-group-id">{g.server_id}</span>
              {g.registered ? (
                <TrustBadge trust={g.trust_status} />
              ) : (
                <span className="badge badge-untrusted">unregistered</span>
              )}
              {quarantined > 0 && (
                <span className="badge badge-critical">
                  {quarantined} quarantined
                </span>
              )}
              <span className="catalog-group-count">
                {g.tools.length} tool{g.tools.length === 1 ? "" : "s"}
              </span>
            </header>

            {g.owner && (
              <div className="catalog-group-meta">
                {g.owner}
                {g.mcp_url ? <span className="cell-mono"> · {g.mcp_url}</span> : null}
              </div>
            )}

            {!isCollapsed && (
              <div className="table-wrap">
                <table className="data-table catalog-table">
                  {/* Every section renders its own table, so the columns are
                      pinned to fixed widths - otherwise each server's table
                      sizes itself to its own content and the sections read
                      as a ragged stack rather than one catalog. */}
                  <colgroup>
                    <col style={{ width: "44%" }} />
                    <col style={{ width: "22%" }} />
                    <col style={{ width: "10%" }} />
                    <col style={{ width: "12%" }} />
                    <col style={{ width: "12%" }} />
                  </colgroup>
                  <thead>
                    <tr>
                      <th>Tool</th>
                      <th>Allowed roles</th>
                      <th>Risk</th>
                      <th>Trust</th>
                      <th></th>
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
                          {(t.allowed_roles || []).map((r) => (
                            <span key={r} className="tag-chip" style={{ marginRight: 5, marginBottom: 4 }}>
                              {r}
                            </span>
                          ))}
                        </td>
                        <td>
                          <SeverityBadge severity={t.risk_level} displayAs={{ critical: "high" }} />
                        </td>
                        <td>
                          <TrustBadge trust={t.trust_status} />
                          {t.hijack_status === "flagged" && t.trust_status !== "quarantined" && (
                            <div style={{ marginTop: 4 }}>
                              <SeverityBadge severity="high" />
                            </div>
                          )}
                        </td>
                        <td>
                          {t.trust_status === "quarantined" ? (
                            <button
                              className="btn btn-secondary btn-sm"
                              disabled={busyId === t.tool_id}
                              onClick={() => handleRelease(t.tool_id)}
                            >
                              Release
                            </button>
                          ) : (
                            <button
                              className="btn btn-danger-outline btn-sm"
                              disabled={busyId === t.tool_id}
                              onClick={() => handleQuarantine(t.tool_id)}
                            >
                              Quarantine
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
