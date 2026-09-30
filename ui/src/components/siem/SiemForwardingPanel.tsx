import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { SiemConfigResponse, SiemDestinationConfig } from "../../api/types";

type FieldType = "text" | "password" | "number" | "checkbox";

interface FieldSpec {
  key: string;
  label: string;
  type: FieldType;
  placeholder?: string;
  hint?: string;
}

// Vendor-specific field list, in display order. Keys/shape must match
// operations-manager/app/siem_forwarding.py's DEFAULT_DESTINATIONS +
// SECRET_FIELDS exactly - this is the client-side mirror of that contract.
const VENDOR_FIELDS: Record<string, FieldSpec[]> = {
  splunk: [
    { key: "hec_url", label: "HEC URL", type: "text", placeholder: "https://splunk.example.com:8088" },
    { key: "hec_token", label: "HEC token", type: "password" },
    { key: "index", label: "Index (optional)", type: "text" },
    { key: "sourcetype", label: "Source type", type: "text" },
    { key: "verify_tls", label: "Verify TLS certificate", type: "checkbox" },
  ],
  elastic: [
    {
      key: "elasticsearch_url",
      label: "Elasticsearch URL",
      type: "text",
      placeholder: "https://elastic.example.com:9200",
    },
    { key: "api_key", label: "API key", type: "password" },
    { key: "data_stream", label: "Data stream", type: "text" },
    { key: "verify_tls", label: "Verify TLS certificate", type: "checkbox" },
  ],
  sumologic: [
    {
      key: "http_source_url",
      label: "HTTP Source collector URL",
      type: "password",
      hint: "The URL itself is the credential for a Sumo Logic HTTP Source, so it's stored encrypted like the other vendors' tokens.",
    },
    { key: "source_name", label: "Source name", type: "text" },
    { key: "source_category", label: "Source category (optional)", type: "text" },
  ],
  sentinel: [
    { key: "tenant_id", label: "Azure AD tenant ID", type: "text" },
    { key: "client_id", label: "App registration client ID", type: "text" },
    { key: "client_secret", label: "Client secret", type: "password" },
    {
      key: "dce_endpoint",
      label: "Data Collection Endpoint",
      type: "text",
      placeholder: "https://xxxx.ingest.monitor.azure.com",
    },
    { key: "dcr_immutable_id", label: "DCR immutable ID", type: "text", placeholder: "dcr-xxxxxxxxxxxxxxxx" },
    { key: "stream_name", label: "Stream name", type: "text" },
  ],
  chronicle: [
    { key: "customer_id", label: "Chronicle customer ID", type: "text" },
    { key: "region", label: "Region", type: "text", placeholder: "us" },
    { key: "log_type", label: "Log type", type: "text" },
    {
      key: "service_account_json",
      label: "Service account JSON key",
      type: "password",
      hint: "Paste the full contents of a service account JSON key file with ingestion access.",
    },
  ],
  crowdstrike: [
    {
      key: "ingest_url",
      label: "Falcon Next-Gen SIEM ingest URL",
      type: "text",
      placeholder: "https://your-cid.cloud.crowdstrike.com",
    },
    { key: "api_token", label: "Ingest API token", type: "password" },
    { key: "tag_source", label: "Source tag", type: "text" },
  ],
};

const SECRET_FIELDS: Record<string, string[]> = {
  splunk: ["hec_token"],
  elastic: ["api_key"],
  sumologic: ["http_source_url"],
  sentinel: ["client_secret"],
  chronicle: ["service_account_json"],
  crowdstrike: ["api_token"],
};

const VENDOR_ORDER = ["splunk", "elastic", "sumologic", "sentinel", "chronicle", "crowdstrike"];

type FormValue = string | number | boolean;
type VendorForm = Record<string, FormValue>;

function buildFormFromConfig(vendor: string, cfg: SiemDestinationConfig | undefined): VendorForm {
  const form: VendorForm = { enabled: !!cfg?.enabled };
  for (const field of VENDOR_FIELDS[vendor] || []) {
    if (SECRET_FIELDS[vendor]?.includes(field.key)) {
      form[field.key] = ""; // blank means "leave the stored secret unchanged"
      continue;
    }
    const raw = cfg?.[field.key];
    form[field.key] = field.type === "checkbox" ? !!raw : raw ?? (field.type === "number" ? 0 : "");
  }
  return form;
}

function buildPayload(vendor: string, form: VendorForm): Record<string, FormValue> {
  const payload: Record<string, FormValue> = { enabled: !!form.enabled };
  for (const field of VENDOR_FIELDS[vendor] || []) {
    const isSecret = SECRET_FIELDS[vendor]?.includes(field.key);
    const value = form[field.key];
    if (isSecret) {
      if (typeof value === "string" && value.trim() !== "") payload[field.key] = value;
      continue; // blank secret field -> omit entirely, backend keeps what's on file
    }
    payload[field.key] = value;
  }
  return payload;
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function SiemForwardingPanel() {
  const [data, setData] = useState<SiemConfigResponse | null>(null);
  const [forms, setForms] = useState<Record<string, VendorForm>>({});
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(true);
  const [expandedVendor, setExpandedVendor] = useState<string | null>(null);
  const [savingVendor, setSavingVendor] = useState<string | null>(null);
  const [testingVendor, setTestingVendor] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { success: boolean; detail: string }>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await api.siemConfig();
      setData(res);
      const nextForms: Record<string, VendorForm> = {};
      for (const vendor of VENDOR_ORDER) nextForms[vendor] = buildFormFromConfig(vendor, res.config[vendor]);
      setForms(nextForms);
    } catch (e: any) {
      setError(e.message || "Failed to load SIEM forwarding configuration");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function setField(vendor: string, key: string, value: FormValue) {
    setForms((f) => ({ ...f, [vendor]: { ...f[vendor], [key]: value } }));
  }

  async function handleSave(vendor: string) {
    setSavingVendor(vendor);
    setError(null);
    try {
      const payload = buildPayload(vendor, forms[vendor] || {});
      const res = await api.saveSiemConfig(vendor, payload);
      setData((d) => (d ? { ...d, config: res.config } : d));
      setForms((f) => ({ ...f, [vendor]: buildFormFromConfig(vendor, res.config[vendor]) }));
    } catch (e: any) {
      setError(e.message || `Failed to save ${vendor} configuration`);
    } finally {
      setSavingVendor(null);
    }
  }

  async function handleTest(vendor: string) {
    setTestingVendor(vendor);
    setError(null);
    try {
      const payload = buildPayload(vendor, forms[vendor] || {});
      const res = await api.testSiemConfig(vendor, payload);
      setTestResults((r) => ({ ...r, [vendor]: res }));
      // Refresh so the "last delivery" pill reflects this test immediately.
      const refreshed = await api.siemConfig();
      setData(refreshed);
    } catch (e: any) {
      setTestResults((r) => ({ ...r, [vendor]: { success: false, detail: e.message || "Test failed" } }));
    } finally {
      setTestingVendor(null);
    }
  }

  const enabledCount = data ? Object.values(data.config).filter((c) => c.enabled).length : 0;

  return (
    <section className="card catalog-group" style={{ marginBottom: 20 }}>
      <header className="catalog-group-header">
        <button
          type="button"
          className="catalog-group-toggle"
          aria-expanded={!collapsed}
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "▸" : "▾"}
        </button>
        <h2 className="catalog-group-title">Forwarding destinations</h2>
        <span className="catalog-group-count">
          {enabledCount} of {VENDOR_ORDER.length} enabled
        </span>
      </header>

      {!collapsed && (
        <>
          <div className="catalog-group-meta">
            Forward every audit log entry - discover and invoke decisions, plus authentication events - to an
            external SIEM in real time, as it's written. Each destination is independent: a slow or unreachable
            endpoint never delays the request that produced the entry, and one destination's failure never affects
            another's.
          </div>

          {error && <div className="error-note">{error}</div>}

          {VENDOR_ORDER.map((vendor) => {
            const label = data?.vendors[vendor] || vendor;
            const cfg = data?.config[vendor];
            const status = data?.status[vendor];
            const form = forms[vendor] || { enabled: false };
            const isExpanded = expandedVendor === vendor;
            const testResult = testResults[vendor];

            return (
              <div
                key={vendor}
                className="panel"
                style={{ marginLeft: 0, marginBottom: 12, cursor: isExpanded ? "default" : "pointer" }}
              >
                <div
                  className="flex-row"
                  style={{ justifyContent: "space-between", alignItems: "center" }}
                  onClick={() => setExpandedVendor(isExpanded ? null : vendor)}
                >
                  <div className="flex-row" style={{ gap: 10, alignItems: "center" }}>
                    <span style={{ fontWeight: 600, fontSize: 14 }}>{label}</span>
                    {cfg?.enabled ? (
                      <span className="badge badge-success">Enabled</span>
                    ) : (
                      <span className="badge badge-neutral">Disabled</span>
                    )}
                    {status && (
                      <span
                        className={status.status === "ok" ? "badge badge-success" : "badge badge-danger"}
                        title={status.detail}
                      >
                        {status.status === "ok" ? "Last delivery OK" : "Last delivery failed"} · {relativeTime(status.at)}
                      </span>
                    )}
                  </div>
                  <button type="button" className="catalog-group-toggle" title={isExpanded ? "Collapse" : "Expand"}>
                    {isExpanded ? "▾" : "▸"}
                  </button>
                </div>

                {isExpanded && (
                  <div style={{ marginTop: 14 }} onClick={(e) => e.stopPropagation()}>
                    <div className="checkbox-row">
                      <input
                        type="checkbox"
                        id={`siem-enabled-${vendor}`}
                        checked={!!form.enabled}
                        onChange={(e) => setField(vendor, "enabled", e.target.checked)}
                      />
                      <label htmlFor={`siem-enabled-${vendor}`} style={{ margin: 0, fontWeight: 600, color: "var(--text)" }}>
                        Enable forwarding to {label}
                      </label>
                    </div>

                    <div className="two-col">
                      {(VENDOR_FIELDS[vendor] || []).map((field) => {
                        const isSecret = SECRET_FIELDS[vendor]?.includes(field.key);
                        const isSet = isSecret && (cfg as any)?.[`${field.key}_set`];
                        if (field.type === "checkbox") {
                          return (
                            <div className="checkbox-row" key={field.key}>
                              <input
                                type="checkbox"
                                id={`siem-${vendor}-${field.key}`}
                                checked={!!form[field.key]}
                                onChange={(e) => setField(vendor, field.key, e.target.checked)}
                              />
                              <label
                                htmlFor={`siem-${vendor}-${field.key}`}
                                style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}
                              >
                                {field.label}
                              </label>
                            </div>
                          );
                        }
                        return (
                          <div className="field" key={field.key}>
                            <label>
                              {field.label}
                              {isSecret ? (isSet ? " (configured - leave blank to keep it)" : "") : ""}
                            </label>
                            <input
                              type={field.type === "password" ? "password" : field.type === "number" ? "number" : "text"}
                              placeholder={isSecret && isSet ? "••••••••" : field.placeholder}
                              value={form[field.key] as string | number}
                              onChange={(e) =>
                                setField(
                                  vendor,
                                  field.key,
                                  field.type === "number" ? Number(e.target.value) || 0 : e.target.value
                                )
                              }
                            />
                            {field.hint && <div className="field-hint">{field.hint}</div>}
                          </div>
                        );
                      })}
                    </div>

                    <div className="flex-row" style={{ gap: 10, marginTop: 6 }}>
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={savingVendor === vendor}
                        onClick={() => handleSave(vendor)}
                      >
                        {savingVendor === vendor ? "Saving..." : "Save"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={testingVendor === vendor}
                        onClick={() => handleTest(vendor)}
                      >
                        {testingVendor === vendor ? "Sending test event..." : "Send test event"}
                      </button>
                    </div>

                    {testResult && (
                      <div className={testResult.success ? "loading-note" : "error-note"} style={{ marginTop: 10 }}>
                        {testResult.success ? "Test event accepted." : testResult.detail}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}
    </section>
  );
}
