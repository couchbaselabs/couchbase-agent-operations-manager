import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { DiscoveredTool, EvalDataset, EvalRun, ExperimentResponse, Role } from "../../api/types";
import { SeverityBadge } from "../badges/Badges";

/** Experimentation + regression automation for the Agent Tool Audit page.
 *
 *  Three things, all admin-only and all free of side effects on customer
 *  systems (discovery runs for real; invoke is *checked*, never executed -
 *  the same rule the eval runner follows):
 *
 *    1. Experiments - a matrix of queries × roles × top_k (and tool IDs ×
 *       roles for invoke decisions), answered in one table so "what does
 *       changing the role, the phrasing or top_k do?" takes one click.
 *    2. Lock in - capture the discover/invoke result just observed above
 *       as a regression case in an eval dataset. The observed answer
 *       becomes the expectation.
 *    3. Regression - run a dataset now and read the trend against its
 *       previous run (improved / stable / regressed, with the cases that
 *       flipped named), the same gate that already runs on every catalog
 *       change.
 */
export function ExperimentsPanel({
  roles,
  lastDiscover,
  lastInvoke,
}: {
  roles: Role[];
  lastDiscover: { role: string; query: string; topK: number; tools: DiscoveredTool[] } | null;
  lastInvoke: { role: string | null; toolId: string; args: Record<string, unknown>; decision: "ALLOW" | "DENY" } | null;
}) {
  // -- experiment matrix ------------------------------------------------
  const [expRoles, setExpRoles] = useState<string[]>([]);
  const [queriesText, setQueriesText] = useState("look up a customer support ticket\nrun a query against the data warehouse");
  const [topKsText, setTopKsText] = useState("5");
  const [toolIdsText, setToolIdsText] = useState("");
  const [running, setRunning] = useState(false);
  const [expError, setExpError] = useState<string | null>(null);
  const [exp, setExp] = useState<ExperimentResponse | null>(null);

  // -- datasets + regression --------------------------------------------
  const [datasets, setDatasets] = useState<EvalDataset[]>([]);
  const [datasetId, setDatasetId] = useState<string>("");
  const [newDatasetName, setNewDatasetName] = useState("Agent Tool Audit regression");
  const [saveNotice, setSaveNotice] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [regressionRunning, setRegressionRunning] = useState(false);
  const [regressionRuns, setRegressionRuns] = useState<EvalRun[] | null>(null);
  const [regressionError, setRegressionError] = useState<string | null>(null);

  const loadDatasets = useCallback(async () => {
    try {
      const res = await api.evals();
      setDatasets(res.datasets);
    } catch {
      // The panel still works for experiments without the dataset list.
    }
  }, []);

  useEffect(() => {
    loadDatasets();
  }, [loadDatasets]);

  useEffect(() => {
    // Default the matrix to every role, once roles arrive.
    if (roles.length && expRoles.length === 0) setExpRoles(roles.map((r) => r.id));
  }, [roles, expRoles.length]);

  function toggleRole(id: string) {
    setExpRoles((r) => (r.includes(id) ? r.filter((x) => x !== id) : [...r, id]));
  }

  const queries = queriesText
    .split("\n")
    .map((q) => q.trim())
    .filter(Boolean);
  const topKs = topKsText
    .split(/[,\s]+/)
    .map((k) => parseInt(k, 10))
    .filter((k) => !isNaN(k) && k > 0);
  const toolIds = toolIdsText
    .split(/[\n,]+/)
    .map((t) => t.trim())
    .filter(Boolean);
  const cells = expRoles.length * (queries.length * Math.max(1, topKs.length) + toolIds.length);

  async function runExperiment() {
    setRunning(true);
    setExpError(null);
    setExp(null);
    try {
      const res = await api.toolAuditExperiment({
        roles: expRoles,
        queries,
        top_ks: topKs.length ? topKs : [5],
        tool_ids: toolIds,
      });
      setExp(res);
    } catch (e: any) {
      setExpError(e.message || "Experiment failed");
    } finally {
      setRunning(false);
    }
  }

  async function saveCase(kind: "discover" | "invoke") {
    setSaving(true);
    setSaveError(null);
    setSaveNotice(null);
    try {
      const payload =
        kind === "discover" && lastDiscover
          ? {
              kind: "discover" as const,
              role: lastDiscover.role,
              query: lastDiscover.query,
              top_k: lastDiscover.topK,
              expect_tools: lastDiscover.tools.map((t) => t.tool_id),
              description: `discover as ${lastDiscover.role}: "${lastDiscover.query.slice(0, 80)}"`,
            }
          : lastInvoke
            ? {
                kind: "invoke" as const,
                role: lastInvoke.role || "",
                tool_id: lastInvoke.toolId,
                arguments: lastInvoke.args,
                expect_decision: lastInvoke.decision,
                description: `invoke ${lastInvoke.toolId} as ${lastInvoke.role}: expect ${lastInvoke.decision}`,
              }
            : null;
      if (!payload) throw new Error("Nothing to save yet - run a discover or invoke above first.");
      const res = await api.toolAuditSaveCase({
        dataset_id: datasetId || null,
        dataset_name: datasetId ? undefined : newDatasetName,
        case: payload,
      });
      setDatasetId(res.dataset.dataset_id);
      setSaveNotice(
        `Saved case '${res.case.case_id}' to '${res.dataset.name}' (${res.dataset.cases.length} case${res.dataset.cases.length === 1 ? "" : "s"}). ` +
          `It runs on every catalog change; run it now below to set the baseline.`
      );
      await loadDatasets();
    } catch (e: any) {
      setSaveError(e.message || "Could not save the case");
    } finally {
      setSaving(false);
    }
  }

  async function runRegression() {
    setRegressionRunning(true);
    setRegressionError(null);
    setRegressionRuns(null);
    try {
      const res = await api.runEvals(datasetId || undefined);
      setRegressionRuns(res.runs);
      await loadDatasets();
    } catch (e: any) {
      setRegressionError(e.message || "Regression run failed");
    } finally {
      setRegressionRunning(false);
    }
  }

  const discoverLocked = !!lastDiscover;
  const invokeLocked = !!lastInvoke && !!lastInvoke.role;

  return (
    <div className="section-gap" style={{ marginTop: 26 }}>
      <h2 style={{ fontSize: 16, margin: "0 0 6px 0" }}>3. Experiment &amp; regression-test</h2>
      <p className="cell-muted" style={{ fontSize: 12.5, margin: "0 0 14px 0" }}>
        Sweep a matrix of roles, queries and top_k in one run, lock in what the gateway answered as a regression case,
        and re-run the suite on demand. Invoke cells check the authorization decision only - nothing downstream is
        called - so an experiment is safe to run against production registrations.
      </p>

      <div className="two-col">
        {/* ---------------------------------------------------- experiment */}
        <div className="panel">
          <h3 className="card-title">Experiment matrix</h3>
          <div className="field">
            <label>Roles</label>
            <div className="flex-row" style={{ flexWrap: "wrap", gap: 10 }}>
              {roles.map((r) => (
                <label key={r.id} className="checkbox-row" style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  <input type="checkbox" checked={expRoles.includes(r.id)} onChange={() => toggleRole(r.id)} /> {r.id}
                </label>
              ))}
            </div>
          </div>
          <div className="field">
            <label>Queries (one per line)</label>
            <textarea rows={4} value={queriesText} onChange={(e) => setQueriesText(e.target.value)} />
          </div>
          <div className="two-col">
            <div className="field">
              <label>top_k values</label>
              <input type="text" value={topKsText} placeholder="e.g. 3, 5, 10" onChange={(e) => setTopKsText(e.target.value)} />
            </div>
            <div className="field">
              <label>Tool IDs to check invoke decisions (optional)</label>
              <input
                type="text"
                value={toolIdsText}
                placeholder="jira::create_issue, snowflake::manage_users"
                onChange={(e) => setToolIdsText(e.target.value)}
              />
            </div>
          </div>
          <div className="flex-row" style={{ gap: 10, alignItems: "center" }}>
            <button className="btn btn-primary" type="button" disabled={running || cells === 0 || cells > 400} onClick={runExperiment}>
              {running ? "Running..." : `Run ${cells} cell${cells === 1 ? "" : "s"}`}
            </button>
            {cells > 400 && <span className="cell-muted">over the 400-cell limit - trim the matrix</span>}
          </div>
          {expError && <div className="error-note">{expError}</div>}

          {exp && (
            <div className="section-gap">
              <div className="loading-note">
                {exp.cells} cells in {exp.latency_ms}ms · roles {exp.roles.join(", ")} · top_k {exp.top_ks.join("/")}
              </div>
              {exp.comparison.map((cmp) => (
                <div key={cmp.query} className="card" style={{ marginTop: 10 }}>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>&ldquo;{cmp.query}&rdquo;</div>
                  {cmp.tools.length === 0 ? (
                    <div className="empty-state">No tool surfaced for any role.</div>
                  ) : (
                    <div className="table-wrap">
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th>Tool</th>
                            <th style={{ textAlign: "right" }}>best sim.</th>
                            {exp.roles.map((r) =>
                              exp.top_ks.map((k) => (
                                <th key={`${r}@${k}`} style={{ textAlign: "center" }}>
                                  {r}
                                  {exp.top_ks.length > 1 ? ` @${k}` : ""}
                                </th>
                              ))
                            )}
                          </tr>
                        </thead>
                        <tbody>
                          {cmp.tools.map((t) => (
                            <tr key={t.tool_id}>
                              <td className="cell-mono">{t.tool_id}</td>
                              <td className="cell-mono" style={{ textAlign: "right" }}>
                                {t.best_similarity.toFixed(3)}
                              </td>
                              {exp.roles.map((r) =>
                                exp.top_ks.map((k) => {
                                  const rank = t.roles[`${r}@${k}`];
                                  return (
                                    <td key={`${r}@${k}`} style={{ textAlign: "center" }}>
                                      {rank ? (
                                        <span className="badge badge-success" title={`rank ${rank}`}>
                                          #{rank}
                                        </span>
                                      ) : (
                                        <span className="badge badge-neutral" title="not visible to this role / top_k">
                                          hidden
                                        </span>
                                      )}
                                    </td>
                                  );
                                })
                              )}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              ))}
              {exp.discover.some((c) => !c.ok) && (
                <div className="error-note">
                  {exp.discover.filter((c) => !c.ok).length} discover cell(s) errored:{" "}
                  {exp.discover
                    .filter((c) => !c.ok)
                    .slice(0, 3)
                    .map((c) => `${c.role}/${c.top_k}: ${c.error}`)
                    .join("; ")}
                </div>
              )}
              {exp.invoke.length > 0 && (
                <div className="card" style={{ marginTop: 10 }}>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>Invoke decisions</div>
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>Tool</th>
                          {exp.roles.map((r) => (
                            <th key={r} style={{ textAlign: "center" }}>
                              {r}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {exp.tool_ids.map((tid) => (
                          <tr key={tid}>
                            <td className="cell-mono">{tid}</td>
                            {exp.roles.map((r) => {
                              const cell = exp.invoke.find((c) => c.tool_id === tid && c.role === r);
                              return (
                                <td key={r} style={{ textAlign: "center" }} title={cell?.reason}>
                                  <span className={`badge ${cell?.decision === "ALLOW" ? "badge-allow" : "badge-deny"}`}>
                                    {cell?.decision || "?"}
                                  </span>
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* ------------------------------------------- lock in + regression */}
        <div className="panel">
          <h3 className="card-title">Regression suite</h3>
          <div className="field">
            <label>Dataset</label>
            <select value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
              <option value="">new dataset...</option>
              {datasets.map((d) => (
                <option key={d.dataset_id} value={d.dataset_id}>
                  {d.name} ({d.cases.length} case{d.cases.length === 1 ? "" : "s"}
                  {d.latest_run ? ` · last ${d.latest_run.status}${d.latest_run.comparison ? `, ${d.latest_run.comparison.trend}` : ""}` : ""})
                </option>
              ))}
            </select>
            {!datasetId && (
              <input
                type="text"
                style={{ marginTop: 8 }}
                value={newDatasetName}
                placeholder="Name for the new dataset"
                onChange={(e) => setNewDatasetName(e.target.value)}
              />
            )}
            <div className="field-hint">
              Datasets are the same ones on the <Link to="/evals">Evaluations</Link> page, and run automatically on every
              catalog change.
            </div>
          </div>

          <div className="field">
            <label>Lock in the result observed above</label>
            <div className="flex-row" style={{ gap: 8, flexWrap: "wrap" }}>
              <button
                className="btn btn-secondary btn-sm"
                type="button"
                disabled={saving || !discoverLocked}
                onClick={() => saveCase("discover")}
                title={discoverLocked ? `expect ${lastDiscover!.tools.length} tool(s) for ${lastDiscover!.role}` : "run a Discover first"}
              >
                Save discover result as case
              </button>
              <button
                className="btn btn-secondary btn-sm"
                type="button"
                disabled={saving || !invokeLocked}
                onClick={() => saveCase("invoke")}
                title={invokeLocked ? `expect ${lastInvoke!.decision} for ${lastInvoke!.toolId}` : "run an Invoke first"}
              >
                Save invoke decision as case
              </button>
            </div>
            <div className="field-hint">
              The result becomes the expectation: the exact tools discovery returned, or the ALLOW/DENY the invoke
              got. The next run fails if the gateway answers differently.
            </div>
            {lastDiscover && (
              <div className="cell-muted" style={{ fontSize: 12, marginTop: 6 }}>
                Discover: <span className="cell-mono">{lastDiscover.role}</span> · &ldquo;{lastDiscover.query.slice(0, 60)}&rdquo; →{" "}
                {lastDiscover.tools.length} tool(s)
              </div>
            )}
            {lastInvoke && (
              <div className="cell-muted" style={{ fontSize: 12, marginTop: 2 }}>
                Invoke: <span className="cell-mono">{lastInvoke.toolId}</span> as{" "}
                <span className="cell-mono">{lastInvoke.role || "?"}</span> → {lastInvoke.decision}
              </div>
            )}
          </div>
          {saveNotice && <div className="helper-banner helper-banner-neutral">{saveNotice}</div>}
          {saveError && <div className="error-note">{saveError}</div>}

          <div className="field">
            <label>Run</label>
            <button className="btn btn-primary" type="button" disabled={regressionRunning} onClick={runRegression}>
              {regressionRunning ? "Running..." : datasetId ? "Run this dataset now" : "Run every enabled dataset now"}
            </button>
          </div>
          {regressionError && <div className="error-note">{regressionError}</div>}

          {regressionRuns &&
            (regressionRuns.length === 0 ? (
              <div className="empty-state">No enabled dataset to run.</div>
            ) : (
              regressionRuns.map((run) => (
                <div key={run.run_id} className="card" style={{ marginTop: 10 }}>
                  <div className="flex-between">
                    <div style={{ fontWeight: 600 }}>{run.dataset_name}</div>
                    <div className="flex-row" style={{ gap: 6 }}>
                      <span className={`badge ${run.status === "passed" ? "badge-success" : run.status === "failed" ? "badge-high" : "badge-critical"}`}>
                        {run.status}
                      </span>
                      {run.comparison && (
                        <span
                          className={`badge ${
                            run.comparison.trend === "regressed"
                              ? "badge-critical"
                              : run.comparison.trend === "improved"
                                ? "badge-success"
                                : "badge-neutral"
                          }`}
                          title={run.comparison.summary}
                        >
                          {run.comparison.trend}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="cell-muted" style={{ fontSize: 12.5, margin: "4px 0 8px 0" }}>
                    score {(run.score * 100).toFixed(1)}% · {run.passed} passed · {run.failed} failed · {run.errored} errored ·{" "}
                    {run.duration_ms}ms
                    {run.comparison?.previous_score != null
                      ? ` · previous ${(run.comparison.previous_score * 100).toFixed(1)}%`
                      : " · baseline"}
                  </div>
                  {run.comparison && (run.comparison.newly_failing.length > 0 || run.comparison.newly_passing.length > 0) && (
                    <div style={{ fontSize: 12.5, marginBottom: 8 }}>
                      {run.comparison.newly_failing.length > 0 && (
                        <div>
                          <strong>Newly failing:</strong> <span className="cell-mono">{run.comparison.newly_failing.join(", ")}</span>
                        </div>
                      )}
                      {run.comparison.newly_passing.length > 0 && (
                        <div>
                          <strong>Newly passing:</strong> <span className="cell-mono">{run.comparison.newly_passing.join(", ")}</span>
                        </div>
                      )}
                    </div>
                  )}
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>Case</th>
                          <th>Kind</th>
                          <th>Role</th>
                          <th>Status</th>
                          <th>Detail</th>
                        </tr>
                      </thead>
                      <tbody>
                        {run.results.map((r) => (
                          <tr key={r.case_id}>
                            <td className="cell-mono">{r.case_id}</td>
                            <td className="cell-muted">{r.kind}</td>
                            <td className="cell-mono cell-muted">{r.role || "-"}</td>
                            <td>
                              <SeverityBadge
                                severity={r.status === "passed" ? "low" : r.status === "failed" ? "high" : "critical"}
                                displayAs={{ low: "passed", high: "failed", critical: "error" }}
                              />
                            </td>
                            <td className="cell-muted" style={{ fontSize: 12 }}>
                              {r.detail}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ))
            ))}
        </div>
      </div>
    </div>
  );
}
