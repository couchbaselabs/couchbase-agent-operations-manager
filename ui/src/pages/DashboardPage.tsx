import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { usePoll } from "../hooks/usePoll";
import type { DashboardResponse } from "../api/types";
import { StatCard } from "../components/dashboard/StatCard";
import { BarChart, DonutChart } from "../components/dashboard/Charts";
import { FindingCard } from "../components/dashboard/FindingCard";

export function DashboardPage() {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.dashboard();
      setData(res);
    } catch (e: any) {
      setError(e.message || "Failed to load dashboard");
    } finally {
      setLoading(false);
    }
  }, []);

  usePoll(load, 30000);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <p className="page-subtitle">
            {data ? `${data.summary.access_events_24h.toLocaleString()} access-log event(s) in the last 24 hours` : "Loading..."}
          </p>
        </div>
        <button className="btn btn-primary" onClick={load} disabled={loading}>
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}

      {data && (
        <>
          <div className="stat-grid">
            <StatCard
              label="Open Findings"
              value={data.summary.open_findings}
              hint={data.summary.open_findings > 0 ? "See Insights for detail" : "Nothing to review"}
            />
            <StatCard
              label="Registered Servers"
              value={data.summary.registered_servers}
              hint={`${data.summary.trusted_servers} trusted`}
            />
            <StatCard
              label="Tools Ingested"
              value={data.summary.tools_ingested}
              hint={
                data.summary.quarantined_tools > 0
                  ? `${data.summary.quarantined_tools} quarantined`
                  : `${data.summary.roles} RBAC roles`
              }
            />
            <StatCard
              label="Access Events (24h)"
              value={data.summary.access_events_24h.toLocaleString()}
              hint={`${data.summary.deny_rate_pct}% denied`}
            />
          </div>

          <div className="chart-grid">
            <div className="card">
              <h3 className="card-title">Access Volume (last 12h)</h3>
              <BarChart
                data={data.hourly_volume.map((h) => ({ label: h.hour, value: h.count }))}
                color="#2dd4c8"
                height={200}
              />
            </div>
            <div className="card">
              <h3 className="card-title">Access Decisions</h3>
              <DonutChart
                segments={[
                  { label: "Allow", value: data.decision_breakdown.ALLOW, color: "#3ecf8e" },
                  { label: "Deny", value: data.decision_breakdown.DENY, color: "#ea2328" },
                  { label: "Error", value: data.decision_breakdown.ERROR, color: "#e8a33d" },
                ]}
              />
            </div>
          </div>

          <div className="flex-between" style={{ marginBottom: 14 }}>
            <h2 style={{ fontSize: 16, margin: 0 }}>Highest severity findings</h2>
            <Link to="/insights" className="btn btn-secondary btn-sm">
              View all findings
            </Link>
          </div>

          {data.top_findings.length === 0 ? (
            <div className="card empty-state">No open findings - the operations manager, catalog, and RBAC policy all look healthy.</div>
          ) : (
            data.top_findings.map((f) => <FindingCard key={f.id} finding={f} />)
          )}
        </>
      )}
    </div>
  );
}
