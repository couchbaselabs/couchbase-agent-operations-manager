import { useCallback, useState } from "react";
import { api } from "../api/client";
import { usePoll } from "../hooks/usePoll";
import type { Approval, ApprovalConfig, ApprovalsResponse } from "../api/types";
import { SeverityBadge } from "../components/badges/Badges";

const STATUS_BADGE: Record<string, string> = {
  pending: "badge-medium",
  approved: "badge-success",
  denied: "badge-deny",
  consumed: "badge-neutral",
  expired: "badge-untrusted",
};

function shortTime(ts?: string | null) {
  return ts ? ts.replace("T", " ").replace("Z", "") : "-";
}

function remaining(expiresAt?: string | null) {
  if (!expiresAt) return null;
  const ms = new Date(expiresAt.replace(" ", "T")).getTime() - Date.now();
  if (Number.isNaN(ms)) return null;
  if (ms <= 0) return "expired";
  const minutes = Math.round(ms / 60000);
  return minutes >= 60 ? `${Math.round(minutes / 60)}h left` : `${minutes}m left`;
}

function PendingCard({
  approval,
  busy,
  onDecide,
}: {
  approval: Approval;
  busy: boolean;
  onDecide: (id: string, approved: boolean, note: string) => void;
}) {
  const [note, setNote] = useState("");
  const left = remaining(approval.expires_at);

  return (
    <div className="panel section-gap" style={{ marginBottom: 14 }}>
      <div className="flex-between" style={{ marginBottom: 10 }}>
        <div>
          <span className="cell-mono" style={{ fontWeight: 600, fontSize: 15 }}>
            {approval.tool_id}
          </span>{" "}
          {approval.risk_level && <SeverityBadge severity={approval.risk_level} />}
          <div className="cell-muted" style={{ fontSize: 12, marginTop: 4 }}>
            requested by <span className="cell-mono">{approval.subject}</span> as{" "}
            <span className="cell-mono">{approval.role}</span> &middot; {shortTime(approval.requested_at)}
            {left && <> &middot; {left}</>}
          </div>
        </div>
        <span className={`badge ${STATUS_BADGE[approval.status] || "badge-neutral"}`}>{approval.status}</span>
      </div>

      <div className="field-hint" style={{ marginBottom: 10 }}>{approval.requested_reason}</div>

      <div className="field">
        <label>Arguments this approval is bound to</label>
        <div className="json-block" style={{ fontSize: 12 }}>
          {Object.keys(approval.arguments || {}).length === 0
            ? "(no arguments)"
            : JSON.stringify(approval.arguments, null, 2)}
        </div>
        <div className="field-hint">
          Approving binds to exactly these arguments. The same agent cannot reuse the approval for a different
          call, and it is single-use.
        </div>
      </div>

      <div className="field">
        <label>Note (optional, shown to nobody but the audit log)</label>
        <input
          type="text"
          value={note}
          placeholder="why you are approving or denying this"
          onChange={(e) => setNote(e.target.value)}
        />
      </div>

      <div className="flex-row">
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => onDecide(approval.approval_id, true, note)}>
          Approve
        </button>
        <button
          className="btn btn-danger-outline btn-sm"
          disabled={busy}
          onClick={() => onDecide(approval.approval_id, false, note)}
        >
          Deny
        </button>
      </div>
    </div>
  );
}

export function ApprovalsPage() {
  const [data, setData] = useState<ApprovalsResponse | null>(null);
  const [config, setConfig] = useState<ApprovalConfig | null>(null);
  const [riskLevels, setRiskLevels] = useState<string[]>([]);
  const [roles, setRoles] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [queue, cfg] = await Promise.all([api.approvals(), api.approvalsConfig()]);
      setData(queue);
      setConfig(cfg.config);
      setRiskLevels(cfg.risk_levels);
      setRoles(cfg.roles);
    } catch (e: any) {
      setError(e.message || "Failed to load approvals");
    }
  }, []);

  // A reviewer leaves this page open waiting for work to arrive, so it
  // polls rather than making them refresh to discover a pending call.
  usePoll(load, 15000);

  async function handleDecide(approvalId: string, approved: boolean, note: string) {
    setBusy(true);
    setError(null);
    try {
      await api.decideApproval(approvalId, approved, note);
      await load();
    } catch (e: any) {
      setError(e.message || "Could not record that decision");
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveConfig() {
    if (!config) return;
    setSavingConfig(true);
    setError(null);
    try {
      const res = await api.saveApprovalsConfig(config);
      setConfig(res.config);
      await load();
    } catch (e: any) {
      setError(e.message || "Could not save the approval policy");
    } finally {
      setSavingConfig(false);
    }
  }

  function patch(update: Partial<ApprovalConfig>) {
    setConfig((c) => (c ? { ...c, ...update } : c));
  }

  const pending = (data?.approvals || []).filter((a) => a.status === "pending");
  const decided = (data?.approvals || []).filter((a) => a.status !== "pending");

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Approvals</h1>
          <p className="page-subtitle">
            The verdict between allow and deny. RBAC answers whether a role may ever call a tool; this answers
            whether this particular call should happen now &mdash; which only a person can. A held call parks
            here, and expires into a denial if nobody acts on it.
          </p>
        </div>
        <button className="btn btn-secondary" onClick={() => setShowSettings((v) => !v)}>
          {showSettings ? "Hide policy" : "Policy"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}

      {config && !config.enabled && (
        <div className="helper-banner helper-banner-neutral">
          The approval tier is off, so every authorized call runs straight through. Turn it on under Policy and
          choose which tools park for review.
        </div>
      )}

      {showSettings && config && (
        <div className="panel section-gap" style={{ marginBottom: 24 }}>
          <div className="two-col">
            <div className="field">
              <label className="checkbox-row">
                <input type="checkbox" checked={config.enabled} onChange={(e) => patch({ enabled: e.target.checked })} />
                <span>Require approval for high-risk calls</span>
              </label>
              <div className="field-hint">
                Off by default: parking calls in front of agents nobody warned looks like an outage.
              </div>
            </div>
            <div className="field">
              <label>Risk threshold</label>
              <select
                value={config.require_at_risk_level}
                onChange={(e) => patch({ require_at_risk_level: e.target.value })}
              >
                <option value="off">off - only the explicit list below</option>
                {riskLevels.map((level) => (
                  <option key={level} value={level}>
                    {level} and above
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="two-col">
            <div className="field">
              <label>Pending approval expires after (seconds)</label>
              <input
                type="number"
                min={60}
                value={config.ttl_seconds}
                onChange={(e) => patch({ ttl_seconds: Number(e.target.value) })}
              />
              <div className="field-hint">
                A Couchbase document TTL, so an approval nobody acts on becomes a denial by ceasing to exist.
              </div>
            </div>
            <div className="field">
              <label>Approved call must be made within (seconds)</label>
              <input
                type="number"
                min={30}
                value={config.grace_seconds}
                onChange={(e) => patch({ grace_seconds: Number(e.target.value) })}
              />
              <div className="field-hint">
                Short on purpose: the judgement was about the situation at that moment.
              </div>
            </div>
          </div>

          <div className="field">
            <label>Always require approval for these tools</label>
            <input
              type="text"
              value={(config.require_for_tools || []).join(", ")}
              placeholder="snowflake::manage_users, billing::issue_refund"
              onChange={(e) =>
                patch({
                  require_for_tools: e.target.value.split(",").map((t) => t.trim()).filter(Boolean),
                })
              }
            />
            <div className="field-hint">Comma-separated tool IDs, whatever their risk level.</div>
          </div>

          <div className="field">
            <label>Roles exempt from approval</label>
            <div className="flex-row" style={{ flexWrap: "wrap", gap: 10, marginTop: 6 }}>
              {roles.map((role) => (
                <label key={role} className="checkbox-row" style={{ margin: 0 }}>
                  <input
                    type="checkbox"
                    checked={(config.exempt_roles || []).includes(role)}
                    onChange={() =>
                      patch({
                        exempt_roles: (config.exempt_roles || []).includes(role)
                          ? config.exempt_roles.filter((r) => r !== role)
                          : [...(config.exempt_roles || []), role],
                      })
                    }
                  />
                  <span className="cell-mono" style={{ fontSize: 13 }}>{role}</span>
                </label>
              ))}
            </div>
          </div>

          <button className="btn btn-primary" onClick={handleSaveConfig} disabled={savingConfig}>
            {savingConfig ? "Saving..." : "Save policy"}
          </button>
        </div>
      )}

      {data && (
        <>
          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">Waiting on a person</div>
              <div className="stat-value">{data.pending_count}</div>
              <div className="stat-hint">Each one is an agent currently blocked</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Threshold</div>
              <div className="stat-value" style={{ fontSize: 18 }}>
                {config?.enabled ? config.require_at_risk_level : "off"}
              </div>
              <div className="stat-hint">
                {config?.require_for_tools?.length || 0} tool(s) on the explicit list
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Expires after</div>
              <div className="stat-value" style={{ fontSize: 18 }}>
                {config ? `${Math.round(config.ttl_seconds / 60)}m` : "-"}
              </div>
              <div className="stat-hint">Unanswered means denied</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Recently decided</div>
              <div className="stat-value">{decided.length}</div>
              <div className="stat-hint">In the current window</div>
            </div>
          </div>

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Waiting for a decision</h2>
          {pending.length === 0 ? (
            <div className="card empty-state">Nothing is waiting. Held calls appear here the moment they are raised.</div>
          ) : (
            pending.map((a) => (
              <PendingCard key={a.approval_id} approval={a} busy={busy} onDecide={handleDecide} />
            ))
          )}

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Recently decided</h2>
          {decided.length === 0 ? (
            <div className="card empty-state">No decisions recorded yet.</div>
          ) : (
            <div className="card">
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Requested (UTC)</th>
                      <th>Tool</th>
                      <th>Caller</th>
                      <th>Status</th>
                      <th>Decided by</th>
                      <th>Note</th>
                    </tr>
                  </thead>
                  <tbody>
                    {decided.map((a) => (
                      <tr key={a.approval_id}>
                        <td className="cell-mono cell-muted">{shortTime(a.requested_at)}</td>
                        <td className="cell-mono">{a.tool_id}</td>
                        <td className="cell-muted cell-mono">{a.subject}</td>
                        <td>
                          <span className={`badge ${STATUS_BADGE[a.status] || "badge-neutral"}`}>{a.status}</span>
                        </td>
                        <td className="cell-muted">{a.decided_by || "-"}</td>
                        <td className="cell-muted">{a.decision_note || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
