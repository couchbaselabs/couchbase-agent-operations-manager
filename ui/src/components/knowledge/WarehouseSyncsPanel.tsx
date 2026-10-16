import React, { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { KnowledgeSet, Vendor, WarehouseSync, WarehouseSyncTestResult, WarehouseSyncsResponse } from "../../api/types";
import { VendorForm, ago } from "../observability/DestinationCard";
import { usePoll } from "../../hooks/usePoll";

/** Warehouse syncs: a user-defined query against Snowflake, Databricks or
 *  BigQuery, run on a schedule, loaded into the Knowledge Base through the
 *  same ingestion jobs as an upload. See operations-manager/app/
 *  warehouse_sync.py. */

const QUERY_HINT: Record<string, string> = {
  snowflake: "SELECT order_id, customer, region, amount, status, updated_at\nFROM sales.public.orders\nWHERE updated_at >= DATEADD(day, -90, CURRENT_DATE())",
  databricks: "SELECT order_id, customer, region, amount, status, updated_at\nFROM main.sales.orders\nWHERE updated_at >= date_sub(current_date(), 90)",
  bigquery: "SELECT order_id, customer, region, amount, status, updated_at\nFROM `my-project.shop.orders`\nWHERE updated_at >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)",
};

type Form = {
  name: string;
  vendor: string;
  connection: Record<string, string>;
  query: string;
  set_id: string;
  allowed_roles: string[];
  table_name: string;
  description: string;
  schedule_kind: "interval" | "cron" | "manual";
  every_minutes: number;
  cron: string;
  mode: "replace" | "snapshot";
  snapshot_ttl_days: number;
  enabled: boolean;
};

const EMPTY: Form = {
  name: "",
  vendor: "snowflake",
  connection: {},
  query: "",
  set_id: "default",
  allowed_roles: [],
  table_name: "",
  description: "",
  schedule_kind: "interval",
  every_minutes: 1440,
  cron: "0 2 * * *",
  mode: "replace",
  snapshot_ttl_days: 30,
  enabled: true,
};

function toForm(s: WarehouseSync): Form {
  return {
    name: s.name,
    vendor: s.vendor,
    connection: s.connection,
    query: s.query,
    set_id: s.set_id,
    allowed_roles: s.allowed_roles,
    table_name: s.table_name,
    description: s.description,
    schedule_kind: s.schedule.kind,
    every_minutes: s.schedule.every_minutes || 1440,
    cron: s.schedule.cron || "0 2 * * *",
    mode: s.mode,
    snapshot_ttl_days: Math.max(1, Math.round(s.snapshot_ttl_seconds / 86400)),
    enabled: s.enabled,
  };
}

function toPayload(f: Form): Partial<WarehouseSync> {
  return {
    name: f.name,
    vendor: f.vendor as WarehouseSync["vendor"],
    connection: f.connection,
    query: f.query,
    set_id: f.set_id,
    allowed_roles: f.allowed_roles,
    table_name: f.table_name,
    description: f.description,
    schedule:
      f.schedule_kind === "cron"
        ? { kind: "cron", cron: f.cron }
        : f.schedule_kind === "manual"
          ? { kind: "manual" }
          : { kind: "interval", every_minutes: f.every_minutes },
    mode: f.mode,
    snapshot_ttl_seconds: f.snapshot_ttl_days * 86400,
    enabled: f.enabled,
  };
}

function scheduleLabel(s: WarehouseSync) {
  if (s.schedule.kind === "cron") return `cron ${s.schedule.cron} (UTC)`;
  if (s.schedule.kind === "manual") return "manual only";
  const m = s.schedule.every_minutes || 0;
  if (m % 1440 === 0) return `every ${m / 1440} day${m / 1440 === 1 ? "" : "s"}`;
  if (m % 60 === 0) return `every ${m / 60} h`;
  return `every ${m} min`;
}

export function WarehouseSyncsPanel({ sets, roles, onChanged }: { sets: KnowledgeSet[]; roles: string[]; onChanged: () => void }) {
  const [data, setData] = useState<WarehouseSyncsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<Form>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<WarehouseSyncTestResult | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [historyId, setHistoryId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.warehouseSyncs();
      setData(res);
      setError(null);
    } catch (e: any) {
      setError(e.message || "Could not load warehouse syncs");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const anyActive = !!data?.syncs.some((s) => s.active_job_id);
  usePoll(
    async () => {
      const res = await api.warehouseSyncs();
      setData(res);
      if (!res.syncs.some((s) => s.active_job_id)) onChanged();
    },
    4000,
    anyActive
  );

  const vendor: Vendor | undefined = data?.vendors.find((v) => v.id === form.vendor);

  function startNew() {
    setForm({ ...EMPTY, set_id: sets[0]?.set_id || "default" });
    setEditingId(null);
    setTest(null);
    setShowForm(true);
  }

  function startEdit(s: WarehouseSync) {
    setForm(toForm(s));
    setEditingId(s.sync_id);
    setTest(null);
    setShowForm(true);
  }

  async function handleTest(withQuery: boolean) {
    setTesting(true);
    setTest(null);
    setError(null);
    try {
      setTest(
        await api.testWarehouseSync({
          vendor: form.vendor,
          connection: form.connection,
          query: withQuery ? form.query : undefined,
          sync_id: editingId || undefined,
        })
      );
    } catch (e: any) {
      setError(e.message || "Test failed");
    } finally {
      setTesting(false);
    }
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload = toPayload(form);
      const res = editingId ? await api.updateWarehouseSync(editingId, payload) : await api.createWarehouseSync(payload);
      setNotice(
        `Saved "${res.sync.name}"` +
          (res.sync.next_run_at ? ` - next run ${res.sync.next_run_at.replace("T", " ").replace("Z", " UTC")}.` : " - run it with Run now.")
      );
      setShowForm(false);
      setEditingId(null);
      await load();
    } catch (e: any) {
      setError(e.message || "Could not save the sync");
    } finally {
      setSaving(false);
    }
  }

  async function act(s: WarehouseSync, action: "run" | "pause" | "resume" | "delete" | "delete-doc") {
    if (action === "delete" && !confirm(`Delete sync "${s.name}"? The document it synced stays in the Knowledge Base.`)) return;
    if (action === "delete-doc" && !confirm(`Delete sync "${s.name}" AND remove its synced document from the Knowledge Base?`)) return;
    setBusyId(s.sync_id);
    setError(null);
    setNotice(null);
    try {
      if (action === "run") {
        const res = await api.runWarehouseSync(s.sync_id);
        setNotice(`Run started as job ${res.job.job_id} - watch it in the Ingestion jobs table.`);
      } else if (action === "pause") await api.setWarehouseSyncEnabled(s.sync_id, false);
      else if (action === "resume") await api.setWarehouseSyncEnabled(s.sync_id, true);
      else await api.deleteWarehouseSync(s.sync_id, action === "delete-doc");
      await load();
      if (action.startsWith("delete")) onChanged();
    } catch (e: any) {
      setError(e.message || `Could not ${action} the sync`);
    } finally {
      setBusyId(null);
    }
  }

  const statusBadge = (s: WarehouseSync) => {
    if (s.active_job_id) return <span className="badge badge-medium">running</span>;
    if (!s.enabled) return <span className="badge badge-neutral">paused</span>;
    const lr = s.last_run;
    if (!lr) return <span className="badge badge-neutral">never run</span>;
    if (lr.status === "done") return <span className="badge badge-success">ok</span>;
    if (lr.status === "failed") return <span className="badge badge-danger">failed</span>;
    return <span className="badge badge-neutral">{lr.status}</span>;
  };

  return (
    <>
      <div className="flex-between" style={{ margin: "22px 0 4px 0", alignItems: "baseline" }}>
        <h2 style={{ fontSize: 16, margin: 0 }}>Warehouse syncs</h2>
        <button className="btn btn-primary btn-sm" type="button" onClick={showForm && !editingId ? () => setShowForm(false) : startNew}>
          {showForm && !editingId ? "Cancel" : "Add sync"}
        </button>
      </div>
      <p className="cell-muted" style={{ fontSize: 12.5, margin: "0 0 14px 0" }}>
        A SQL query against Snowflake, Databricks or BigQuery, run on a schedule and loaded into the Knowledge Base as
        a table document. Results stream from the warehouse&apos;s own API into the ingestion spool, so a run costs
        the same memory as a small upload whatever its size. <em>Replace</em> swaps the previous run&apos;s document
        out only once the new one is fully indexed; <em>snapshot</em> keeps it with an expiry.
      </p>

      {error && <div className="error-note">{error}</div>}
      {notice && <div className="helper-banner helper-banner-neutral">{notice}</div>}

      {showForm && data && (
        <form className="panel section-gap" onSubmit={handleSave} style={{ marginBottom: 18 }}>
          <div className="two-col">
            <div className="field">
              <label>Name</label>
              <input type="text" required value={form.name} placeholder="e.g. Orders - last 90 days" onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
            </div>
            <div className="field">
              <label>Warehouse</label>
              <select
                value={form.vendor}
                disabled={!!editingId}
                onChange={(e) => {
                  setForm((f) => ({ ...f, vendor: e.target.value, connection: {} }));
                  setTest(null);
                }}
              >
                {data.vendors.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.label}
                  </option>
                ))}
              </select>
              {vendor && <div className="field-hint">{vendor.transport}</div>}
            </div>
          </div>

          {vendor && (
            <>
              <div style={{ fontWeight: 600, fontSize: 13, margin: "6px 0 8px 0" }}>Connection</div>
              <VendorForm
                vendor={vendor}
                value={{ type: vendor.id, enabled: true, fields: form.connection }}
                onChange={(fields) => setForm((f) => ({ ...f, connection: fields }))}
              />
            </>
          )}

          <div className="field">
            <label>Query</label>
            <textarea
              rows={6}
              className="cell-mono"
              required
              value={form.query}
              placeholder={QUERY_HINT[form.vendor]}
              onChange={(e) => setForm((f) => ({ ...f, query: e.target.value }))}
            />
            <div className="field-hint">
              One SELECT. Every row becomes a line of the table document (column: value pairs), so pick the columns an
              agent should be able to read and filter to the rows that matter - a WHERE on a date column keeps a
              nightly sync small. Secrets never appear in the query; the connection above carries them.
            </div>
          </div>

          <div className="flex-row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 10 }}>
            <button type="button" className="btn btn-secondary btn-sm" disabled={testing} onClick={() => handleTest(false)}>
              {testing ? "Testing..." : "Test connection"}
            </button>
            <button type="button" className="btn btn-secondary btn-sm" disabled={testing || !form.query.trim()} onClick={() => handleTest(true)}>
              Preview query (10 rows)
            </button>
            {test && (
              <span className={`badge ${test.ok ? "badge-success" : "badge-danger"}`} style={{ whiteSpace: "normal" }}>
                {test.detail}
              </span>
            )}
          </div>
          {test?.ok && test.rows.length > 0 && (
            <div className="table-wrap" style={{ marginBottom: 12, maxHeight: 260, overflow: "auto" }}>
              <table className="data-table">
                <thead>
                  <tr>
                    {test.columns.map((c) => (
                      <th key={c}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {test.rows.map((r, i) => (
                    <tr key={i}>
                      {test.columns.map((c) => (
                        <td key={c} className="cell-mono" style={{ fontSize: 12, maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {r[c] == null ? "" : String(r[c])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="two-col">
            <div className="field">
              <label>Table name (recorded on every chunk)</label>
              <input type="text" value={form.table_name} placeholder="defaults to the sync name" onChange={(e) => setForm((f) => ({ ...f, table_name: e.target.value }))} />
            </div>
            <div className="field">
              <label>Knowledge set</label>
              <select value={form.set_id} onChange={(e) => setForm((f) => ({ ...f, set_id: e.target.value }))}>
                {sets.map((s) => (
                  <option key={s.set_id} value={s.set_id}>
                    {s.name} - {s.model_label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="two-col">
            <div className="field">
              <label>Schedule</label>
              <select value={form.schedule_kind} onChange={(e) => setForm((f) => ({ ...f, schedule_kind: e.target.value as Form["schedule_kind"] }))}>
                <option value="interval">every N minutes / hours / days</option>
                <option value="cron">cron expression (UTC)</option>
                <option value="manual">manual only (Run now)</option>
              </select>
              {form.schedule_kind === "interval" && (
                <select style={{ marginTop: 8 }} value={form.every_minutes} onChange={(e) => setForm((f) => ({ ...f, every_minutes: Number(e.target.value) }))}>
                  {[15, 30, 60, 180, 360, 720, 1440, 10080].map((m) => (
                    <option key={m} value={m}>
                      {m < 60 ? `every ${m} min` : m < 1440 ? `every ${m / 60} h` : m === 1440 ? "daily" : "weekly"}
                    </option>
                  ))}
                </select>
              )}
              {form.schedule_kind === "cron" && (
                <input type="text" className="cell-mono" style={{ marginTop: 8 }} value={form.cron} placeholder="0 2 * * *" onChange={(e) => setForm((f) => ({ ...f, cron: e.target.value }))} />
              )}
              <div className="field-hint">
                Checked every {data.tick_seconds} s. A window missed while the operations manager was down runs once at
                startup; a run that is still going when the next window arrives is skipped and recorded.
              </div>
            </div>
            <div className="field">
              <label>When a run finishes</label>
              <select value={form.mode} onChange={(e) => setForm((f) => ({ ...f, mode: e.target.value as Form["mode"] }))}>
                <option value="replace">replace - delete the previous run&apos;s document</option>
                <option value="snapshot">snapshot - keep the previous document with an expiry</option>
              </select>
              {form.mode === "snapshot" && (
                <input type="number" min={1} max={365} style={{ marginTop: 8 }} value={form.snapshot_ttl_days} onChange={(e) => setForm((f) => ({ ...f, snapshot_ttl_days: Number(e.target.value) || 30 }))} />
              )}
              <div className="field-hint">
                {form.mode === "snapshot" ? "Days each superseded snapshot stays retrievable." : "Agents see the old document until the new one is fully indexed - never both, never neither."}
              </div>
            </div>
          </div>

          <div className="field">
            <label>Which roles may retrieve the synced table</label>
            {roles.map((role) => (
              <div className="checkbox-row" key={role}>
                <input
                  type="checkbox"
                  id={`sync-role-${role}`}
                  checked={form.allowed_roles.includes(role)}
                  onChange={() =>
                    setForm((f) => ({
                      ...f,
                      allowed_roles: f.allowed_roles.includes(role) ? f.allowed_roles.filter((r) => r !== role) : [...f.allowed_roles, role],
                    }))
                  }
                />
                <label htmlFor={`sync-role-${role}`} style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  {role}
                </label>
              </div>
            ))}
          </div>

          <div className="flex-row" style={{ gap: 8 }}>
            <button className="btn btn-primary" type="submit" disabled={saving || !form.name || !form.query.trim() || form.allowed_roles.length === 0}>
              {saving ? "Saving..." : editingId ? "Save changes" : "Create sync"}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setShowForm(false);
                setEditingId(null);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className="card" style={{ marginBottom: 24 }}>
        {!data ? (
          <div className="loading-note">Loading...</div>
        ) : data.syncs.length === 0 ? (
          <div className="empty-state">No warehouse syncs yet. Add one to keep a table in the Knowledge Base fresh on a schedule.</div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Sync</th>
                  <th>Schedule</th>
                  <th>Status</th>
                  <th>Last run</th>
                  <th>Next run</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {data.syncs.map((s) => (
                  <React.Fragment key={s.sync_id}>
                    <tr>
                      <td>
                        <div style={{ fontWeight: 600 }}>{s.name}</div>
                        <div className="cell-muted" style={{ fontSize: 12 }}>
                          <span className="badge badge-neutral">{s.vendor}</span> {s.table_name} · {s.mode} · roles {s.allowed_roles.join(", ")}
                        </div>
                      </td>
                      <td className="cell-muted">{scheduleLabel(s)}</td>
                      <td>{statusBadge(s)}</td>
                      <td style={{ fontSize: 12.5 }}>
                        {s.last_run ? (
                          <>
                            <div>
                              {ago(s.last_run.finished_at || s.last_run.started_at)} · {s.last_run.rows.toLocaleString()} rows
                              {s.last_run.trigger ? ` · ${s.last_run.trigger}` : ""}
                            </div>
                            {s.last_run.error && (
                              <div className="cell-muted" style={{ color: "var(--red-dim)", maxWidth: 320 }}>
                                {s.last_run.error}
                              </div>
                            )}
                            {s.current_document_id && (
                              <div className="cell-muted cell-mono" style={{ fontSize: 11.5 }}>
                                document {s.current_document_id}
                              </div>
                            )}
                          </>
                        ) : (
                          <span className="cell-muted">-</span>
                        )}
                      </td>
                      <td className="cell-muted" style={{ fontSize: 12.5 }}>
                        {s.next_run_at ? s.next_run_at.replace("T", " ").replace("Z", " UTC") : s.enabled ? "manual" : "paused"}
                      </td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <div className="flex-row" style={{ gap: 6 }}>
                          <button className="btn btn-primary btn-sm" disabled={busyId === s.sync_id || !!s.active_job_id} onClick={() => act(s, "run")}>
                            Run now
                          </button>
                          <button className="btn btn-secondary btn-sm" disabled={busyId === s.sync_id} onClick={() => act(s, s.enabled ? "pause" : "resume")}>
                            {s.enabled ? "Pause" : "Resume"}
                          </button>
                          <button className="btn btn-secondary btn-sm" onClick={() => startEdit(s)}>
                            Edit
                          </button>
                          <button className="btn btn-secondary btn-sm" onClick={() => setHistoryId(historyId === s.sync_id ? null : s.sync_id)}>
                            {historyId === s.sync_id ? "Hide runs" : `Runs (${s.runs.length})`}
                          </button>
                          <button className="btn btn-danger-outline btn-sm" disabled={busyId === s.sync_id} onClick={() => act(s, s.current_document_id ? "delete-doc" : "delete")} title={s.current_document_id ? "Deletes the sync and its synced document" : "Deletes the sync"}>
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                    {historyId === s.sync_id && (
                      <tr>
                        <td colSpan={6} style={{ paddingTop: 0 }}>
                          {s.runs.length === 0 ? (
                            <div className="cell-muted">No runs yet.</div>
                          ) : (
                            <table className="data-table" style={{ fontSize: 12.5 }}>
                              <thead>
                                <tr>
                                  <th>Started</th>
                                  <th>Trigger</th>
                                  <th>Status</th>
                                  <th style={{ textAlign: "right" }}>Rows</th>
                                  <th>Document</th>
                                  <th>Detail</th>
                                </tr>
                              </thead>
                              <tbody>
                                {s.runs.map((r, i) => (
                                  <tr key={i}>
                                    <td className="cell-muted">{(r.started_at || "").replace("T", " ").replace("Z", "")}</td>
                                    <td className="cell-muted">{r.trigger || "-"}</td>
                                    <td>
                                      <span className={`badge ${r.status === "done" ? "badge-success" : r.status === "failed" ? "badge-danger" : "badge-neutral"}`}>{r.status}</span>
                                    </td>
                                    <td className="cell-mono" style={{ textAlign: "right" }}>
                                      {r.rows.toLocaleString()}
                                    </td>
                                    <td className="cell-mono cell-muted" style={{ fontSize: 11.5 }}>
                                      {r.document_id || "-"}
                                    </td>
                                    <td className="cell-muted">{r.error || r.replaced || (r.job_id ? `job ${r.job_id}` : "")}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
