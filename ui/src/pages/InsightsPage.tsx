import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { Finding } from "../api/types";
import { FindingCard } from "../components/dashboard/FindingCard";

const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"];

export function InsightsPage() {
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<string>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.insights();
      setFindings(res.findings);
    } catch (e: any) {
      setError(e.message || "Failed to load insights");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const counts = SEVERITY_ORDER.reduce<Record<string, number>>((acc, sev) => {
    acc[sev] = (findings || []).filter((f) => f.severity === sev).length;
    return acc;
  }, {});

  const visible = (findings || []).filter((f) => filter === "all" || f.severity === filter);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Insights</h1>
          <p className="page-subtitle">
            Findings derived from the current tool catalog, server registry, and recent audit log - recomputed on
            every load, nothing here is stored.
          </p>
        </div>
        <button className="btn btn-primary" onClick={load} disabled={loading}>
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}

      <div className="flex-row" style={{ marginBottom: 18, flexWrap: "wrap" }}>
        <FilterChip label={`All (${(findings || []).length})`} active={filter === "all"} onClick={() => setFilter("all")} />
        {SEVERITY_ORDER.filter((s) => counts[s] > 0).map((sev) => (
          <FilterChip
            key={sev}
            label={`${sev} (${counts[sev]})`}
            active={filter === sev}
            onClick={() => setFilter(sev)}
          />
        ))}
      </div>

      {findings && visible.length === 0 && (
        <div className="card empty-state">
          {findings.length === 0
            ? "No open findings - the operations manager, catalog, and RBAC policy all look healthy."
            : "No findings match this filter."}
        </div>
      )}

      {visible.map((f) => (
        <FindingCard key={f.id} finding={f} />
      ))}
    </div>
  );
}

function FilterChip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      className={`btn btn-sm ${active ? "btn-primary" : "btn-secondary"}`}
      style={{ textTransform: "capitalize" }}
      onClick={onClick}
    >
      {label}
    </button>
  );
}
