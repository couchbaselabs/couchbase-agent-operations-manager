import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { usePoll } from "../hooks/usePoll";
import type { AuditLogEntry } from "../api/types";
import { DecisionBadge } from "../components/badges/Badges";
import { SiemForwardingPanel } from "../components/siem/SiemForwardingPanel";

export function AuditLogPage() {
  const [entries, setEntries] = useState<AuditLogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionFilter, setActionFilter] = useState("all");
  const [decisionFilter, setDecisionFilter] = useState("all");
  const [roleFilter, setRoleFilter] = useState("all");
  const [live, setLive] = useState(true);
  const [limit, setLimit] = useState(100);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.auditLog(limit);
      setEntries(res.entries);
    } catch (e: any) {
      setError(e.message || "Failed to load audit log");
    }
  }, [limit]);

  // The fastest poll in the dashboard, and the one whose query reads the
  // most rows - so it is also the one that most needs to stop asking while
  // an earlier refresh is still outstanding.
  usePoll(load, 8000, live);

  // Still fetch once when live updates are switched off, so the page is
  // never empty just because the operator paused it.
  useEffect(() => {
    if (!live) load();
  }, [live, load]);

  const filtered = (entries || []).filter((e) => {
    if (actionFilter !== "all" && e.action !== actionFilter) return false;
    if (decisionFilter !== "all" && e.decision !== decisionFilter) return false;
    if (roleFilter !== "all" && e.role !== roleFilter) return false;
    return true;
  });

  // Distinct roles actually present in the currently-loaded page of
  // entries, not a hardcoded list -- so a custom role registered later
  // (Servers page / POST /v1/servers-adjacent role additions) shows up
  // here automatically instead of silently having no way to filter on it.
  const roleOptions = Array.from(new Set((entries || []).map((e) => e.role).filter((r): r is string => !!r))).sort();

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Audit Log</h1>
          <p className="page-subtitle">
            Every discover and invoke decision, ALLOW or DENY, written append-only to Couchbase - including
            authentication failures.
          </p>
        </div>
        <div className="flex-row">
          <label className="checkbox-row" style={{ marginBottom: 0 }}>
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
            Live (8s)
          </label>
          <button className="btn btn-secondary" onClick={load}>
            Refresh
          </button>
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}

      <SiemForwardingPanel />

      <div className="flex-row" style={{ marginBottom: 16, flexWrap: "wrap", gap: 10 }}>
        <select
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
          style={{ background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px" }}
        >
          <option value="all">All actions</option>
          <option value="authenticate">authenticate</option>
          <option value="discover">discover</option>
          <option value="invoke">invoke</option>
        </select>
        <select
          value={decisionFilter}
          onChange={(e) => setDecisionFilter(e.target.value)}
          style={{ background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px" }}
        >
          <option value="all">All decisions</option>
          <option value="ALLOW">ALLOW</option>
          <option value="DENY">DENY</option>
          <option value="ERROR">ERROR</option>
        </select>
        <select
          value={roleFilter}
          onChange={(e) => setRoleFilter(e.target.value)}
          style={{ background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px" }}
        >
          <option value="all">All roles</option>
          {roleOptions.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <select
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
          style={{ background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px" }}
        >
          <option value={50}>Last 50</option>
          <option value={100}>Last 100</option>
          <option value={200}>Last 200</option>
        </select>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Time (UTC)</th>
                <th>Action</th>
                <th>Role</th>
                <th>Subject</th>
                <th>Tool / Query</th>
                <th>Decision</th>
                <th>Reason</th>
                <th>Latency</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((e, i) => (
                <tr key={i}>
                  <td className="cell-mono cell-muted">{e.timestamp?.replace("T", " ").replace("Z", "")}</td>
                  <td className="cell-mono">{e.action}</td>
                  <td className="cell-mono">{e.role || "-"}</td>
                  <td className="cell-mono cell-muted">{e.subject || "-"}</td>
                  <td className="cell-mono">{e.tool_id || e.query || "-"}</td>
                  <td>
                    <DecisionBadge decision={e.decision} />
                  </td>
                  <td className="cell-muted" style={{ maxWidth: 360 }}>
                    {e.reason}
                  </td>
                  <td className="cell-muted">{e.latency_ms}ms</td>
                </tr>
              ))}
            </tbody>
          </table>
          {entries && filtered.length === 0 && <div className="empty-state">No matching audit log entries yet.</div>}
        </div>
      </div>
    </div>
  );
}
