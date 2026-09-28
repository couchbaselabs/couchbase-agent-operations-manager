import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { EvalComparison, EvalDataset, EvalRun, EvalRunSummary, EvalsResponse } from "../api/types";

const TREND_BADGE: Record<EvalComparison["trend"], string> = {
  improved: "badge-success",
  stable: "badge-neutral",
  regressed: "badge-critical",
  baseline: "badge-info",
};

const STATUS_BADGE: Record<string, string> = {
  passed: "badge-success",
  failed: "badge-danger",
  error: "badge-error",
};

function shortTime(ts?: string | null) {
  return ts ? ts.replace("T", " ").replace("Z", "") : "-";
}

function scorePercent(score: number) {
  return `${Math.round(score * 100)}%`;
}

function TrendPill({ comparison }: { comparison?: EvalComparison }) {
  if (!comparison) return <span className="badge badge-neutral">not run</span>;
  const delta = comparison.delta;
  return (
    <span className={`badge ${TREND_BADGE[comparison.trend]}`}>
      {comparison.trend}
      {comparison.trend !== "baseline" && delta !== 0 ? ` ${delta > 0 ? "+" : ""}${delta.toFixed(3)}` : ""}
    </span>
  );
}

function RunDetail({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [run, setRun] = useState<EvalRun | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .evalRun(runId)
      .then((r) => !cancelled && setRun(r.run))
      .catch((e: any) => !cancelled && setError(e.message || "Failed to load run"));
    return () => {
      cancelled = true;
    };
  }, [runId]);

  if (error) return <div className="error-note">{error}</div>;
  if (!run) return <div className="card empty-state">Loading run...</div>;

  return (
    <div className="card" style={{ marginBottom: 24 }}>
      <div className="flex-between" style={{ marginBottom: 12 }}>
        <div>
          <div className="card-title">{run.dataset_name}</div>
          <div className="cell-muted" style={{ fontSize: 13 }}>
            {shortTime(run.started_at)} &middot; triggered by {run.trigger}
          </div>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          Close
        </button>
      </div>

      {run.comparison && (
        <div
          className={`helper-banner${run.comparison.trend === "regressed" ? "" : " helper-banner-neutral"}`}
          style={{ marginBottom: 16 }}
        >
          <div className="helper-banner-heading">
            <TrendPill comparison={run.comparison} /> against the previous run
          </div>
          {run.comparison.summary}
        </div>
      )}

      <div className="stat-grid" style={{ marginBottom: 16 }}>
        <div className="stat-card">
          <div className="stat-label">Score</div>
          <div className="stat-value">{scorePercent(run.score)}</div>
          <div className="stat-hint">Weighted across {run.case_count} case(s)</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Passed</div>
          <div className="stat-value">{run.passed}</div>
          <div className="stat-hint">{run.failed} failed, {run.errored} errored</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Duration</div>
          <div className="stat-value">{(run.duration_ms / 1000).toFixed(1)}s</div>
          <div className="stat-hint">Against the live gateway</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Catalog</div>
          <div className="stat-value" style={{ fontSize: 18 }}>
            {run.catalog_version ? run.catalog_version.slice(0, 10) : "-"}
          </div>
          <div className="stat-hint">Fingerprint the run scored against</div>
        </div>
      </div>

      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Case</th>
              <th>Kind</th>
              <th>Role</th>
              <th>Result</th>
              <th>What happened</th>
            </tr>
          </thead>
          <tbody>
            {run.results.map((r) => (
              <tr key={r.case_id}>
                <td className="cell-mono" style={{ fontWeight: 600 }}>
                  {r.case_id}
                  {r.description && (
                    <div className="cell-muted" style={{ fontWeight: 400, fontSize: 12, marginTop: 4 }}>
                      {r.description}
                    </div>
                  )}
                </td>
                <td className="cell-muted">{r.kind}</td>
                <td className="cell-muted">{r.role || "-"}</td>
                <td>
                  <span className={`badge ${STATUS_BADGE[r.status] || "badge-neutral"}`}>{r.status}</span>
                </td>
                <td className="cell-muted">{r.error || r.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function EvalsPage() {
  const [data, setData] = useState<EvalsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.evals());
    } catch (e: any) {
      setError(e.message || "Failed to load evaluations");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleRun(datasetId?: string) {
    setRunning(datasetId || "*");
    setError(null);
    try {
      const res = await api.runEvals(datasetId);
      await load();
      if (res.runs.length) setSelectedRun(res.runs[0].run_id);
    } catch (e: any) {
      setError(e.message || "Run failed");
    } finally {
      setRunning(null);
    }
  }

  async function handleToggleGate(dataset: EvalDataset) {
    try {
      await api.saveEvalDataset({ ...dataset, run_on_catalog_change: !dataset.run_on_catalog_change });
      await load();
    } catch (e: any) {
      setError(e.message || "Could not save the dataset");
    }
  }

  const regressed = (data?.datasets || []).filter(
    (d) => d.latest_run?.comparison?.trend === "regressed"
  );

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Evaluations</h1>
          <p className="page-subtitle">
            Saved trajectories replayed against the live gateway: which tools discovery surfaces for a role, which
            invocations are allowed, and whether a completion still means what it used to. Every run is scored and
            compared against the previous one, so a change that made the agents worse is visible as a regression
            rather than a number.
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => handleRun()} disabled={!!running || loading}>
          {running === "*" ? "Running..." : "Run all"}
        </button>
      </div>

      {error && <div className="error-note">{error}</div>}

      {data && (
        <>
          {regressed.length > 0 && (
            <div className="helper-banner" style={{ marginBottom: 18 }}>
              <div className="helper-banner-heading">
                {regressed.length} dataset{regressed.length === 1 ? "" : "s"} regressed
              </div>
              {regressed.map((d) => (
                <div key={d.dataset_id} style={{ marginTop: 4 }}>
                  <span className="mono">{d.name}</span> &mdash; {d.latest_run?.comparison?.summary}
                </div>
              ))}
            </div>
          )}

          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">Datasets</div>
              <div className="stat-value">{data.datasets.length}</div>
              <div className="stat-hint">
                {data.datasets.filter((d) => d.run_on_catalog_change).length} gate on catalog change
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Regressed</div>
              <div className="stat-value">{regressed.length}</div>
              <div className="stat-hint">Against each dataset's previous run</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Runs recorded</div>
              <div className="stat-value">{data.runs.length}</div>
              <div className="stat-hint">Most recent first</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Last gate</div>
              <div className="stat-value" style={{ fontSize: 18 }}>
                {data.last_gate_at ? shortTime(data.last_gate_at) : "not yet run"}
              </div>
              <div className="stat-hint">
                {data.run_on_catalog_change ? "Runs on every catalog change" : "Catalog trigger disabled"}
              </div>
            </div>
          </div>

          {selectedRun && <RunDetail runId={selectedRun} onClose={() => setSelectedRun(null)} />}

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Datasets</h2>
          {data.datasets.length === 0 ? (
            <div className="card empty-state">
              No datasets yet. The bundled starter dataset is seeded on first boot unless
              <span className="mono"> EVAL_SEED_STARTER_DATASET=false</span>.
            </div>
          ) : (
            <div className="card" style={{ marginBottom: 24 }}>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Dataset</th>
                      <th style={{ textAlign: "right" }}>Cases</th>
                      <th>Last run</th>
                      <th>Score</th>
                      <th>Trend</th>
                      <th>Catalog gate</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.datasets.map((d) => (
                      <tr key={d.dataset_id}>
                        <td>
                          <div className="cell-mono" style={{ fontWeight: 600 }}>{d.name}</div>
                          <div className="cell-muted" style={{ fontSize: 12, marginTop: 4, maxWidth: 420 }}>
                            {d.description}
                          </div>
                        </td>
                        <td className="cell-mono" style={{ textAlign: "right" }}>{d.cases.length}</td>
                        <td className="cell-muted cell-mono">{shortTime(d.latest_run?.started_at)}</td>
                        <td className="cell-mono">
                          {d.latest_run ? scorePercent(d.latest_run.score) : "-"}
                        </td>
                        <td>
                          <TrendPill comparison={d.latest_run?.comparison} />
                        </td>
                        <td>
                          <label className="checkbox-row" style={{ margin: 0 }}>
                            <input
                              type="checkbox"
                              checked={d.run_on_catalog_change}
                              onChange={() => handleToggleGate(d)}
                            />
                            <span className="cell-muted" style={{ fontSize: 12 }}>
                              run on catalog change
                            </span>
                          </label>
                        </td>
                        <td>
                          <button
                            className="btn btn-secondary btn-sm"
                            disabled={!!running}
                            onClick={() => handleRun(d.dataset_id)}
                          >
                            {running === d.dataset_id ? "Running..." : "Run"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <h2 style={{ fontSize: 16, margin: "22px 0 14px 0" }}>Run history</h2>
          {data.runs.length === 0 ? (
            <div className="card empty-state">Nothing has run yet.</div>
          ) : (
            <div className="card">
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Started (UTC)</th>
                      <th>Dataset</th>
                      <th>Trigger</th>
                      <th>Score</th>
                      <th>Result</th>
                      <th>Trend</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.runs.map((r: EvalRunSummary) => (
                      <tr key={r.run_id}>
                        <td className="cell-mono cell-muted">{shortTime(r.started_at)}</td>
                        <td className="cell-mono">{r.dataset_name}</td>
                        <td className="cell-muted">{r.trigger}</td>
                        <td className="cell-mono">{scorePercent(r.score)}</td>
                        <td>
                          <span className={`badge ${STATUS_BADGE[r.status] || "badge-neutral"}`}>{r.status}</span>
                          <span className="cell-muted" style={{ marginLeft: 8, fontSize: 12 }}>
                            {r.passed}/{r.case_count}
                          </span>
                        </td>
                        <td>
                          <TrendPill comparison={r.comparison} />
                        </td>
                        <td>
                          <button className="btn btn-secondary btn-sm" onClick={() => setSelectedRun(r.run_id)}>
                            Open
                          </button>
                        </td>
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
