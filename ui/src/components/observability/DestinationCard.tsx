import React, { useState } from "react";
import { SECRET_MASK, type DeliveryTestResult, type DestinationConfig, type Vendor, type VendorField } from "../../api/types";

/** Relative time, e.g. "12 s ago" - for delivery pills and status tables. */
export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  if (isNaN(ms)) return iso;
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

export function fmt(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString();
}

export interface DeliveryPill {
  tone: "success" | "danger" | "warning" | "neutral";
  text: string;
  title?: string;
}

const PILL_CLASS: Record<DeliveryPill["tone"], string> = {
  success: "badge-success",
  danger: "badge-danger",
  warning: "badge-medium",
  neutral: "badge-neutral",
};

/** The settings form of one vendor, generated from the catalog the
 *  operations manager serves (see app/log_forwarding.VENDORS and
 *  app/monitoring.EXPORTERS) - a field is defined once, server-side. */
export function VendorForm({
  vendor,
  value,
  onChange,
  disabled,
}: {
  vendor: Vendor;
  value: DestinationConfig;
  onChange: (fields: Record<string, string>) => void;
  disabled?: boolean;
}) {
  const get = (f: VendorField) => value.fields[f.name] ?? "";
  const set = (f: VendorField, v: string) => onChange({ ...value.fields, [f.name]: v });
  return (
    <div className="two-col">
      {vendor.fields.map((f) => {
        const label = f.required ? `${f.label} *` : f.label;
        if (f.input === "bool") {
          const v = get(f) === "" ? f.default === "true" : get(f) === "true";
          return (
            <div className="field" key={f.name}>
              <div className="checkbox-row" style={{ marginTop: 22 }}>
                <input
                  type="checkbox"
                  id={`${vendor.id}-${f.name}`}
                  checked={v}
                  disabled={disabled}
                  onChange={(e) => set(f, String(e.target.checked))}
                />
                <label htmlFor={`${vendor.id}-${f.name}`} style={{ margin: 0, fontWeight: 400, color: "var(--text)" }}>
                  {f.label}
                </label>
              </div>
              {f.hint && <div className="field-hint">{f.hint}</div>}
            </div>
          );
        }
        if (f.input === "select") {
          return (
            <div className="field" key={f.name}>
              <label>{label}</label>
              <select value={get(f) || f.default} disabled={disabled} onChange={(e) => set(f, e.target.value)}>
                {f.options.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
              {f.hint && <div className="field-hint">{f.hint}</div>}
            </div>
          );
        }
        const secretStored = (f.input === "secret" || f.input === "secret_text") && get(f) === SECRET_MASK;
        const hint = secretStored ? "Stored. Leave the asterisks to keep it." : f.hint;
        if (f.input === "secret_text") {
          // Multi-line secret (PEM private key, service-account JSON): a
          // single-line password box would strip the newlines. Masked with
          // CSS; the value never leaves the form unencrypted anyway.
          return (
            <div className="field" key={f.name}>
              <label>{label}</label>
              <textarea
                rows={secretStored ? 1 : 5}
                className="cell-mono"
                value={get(f)}
                placeholder={f.placeholder || (f.name.endsWith("_json") ? "{ ... }" : "-----BEGIN PRIVATE KEY-----")}
                disabled={disabled}
                autoComplete="off"
                spellCheck={false}
                style={{ WebkitTextSecurity: "disc", fontSize: 12 } as React.CSSProperties}
                onFocus={(e) => {
                  if (get(f) === SECRET_MASK) set(f, "");
                  e.currentTarget.rows = 5;
                }}
                onChange={(e) => set(f, e.target.value)}
              />
              {hint && <div className="field-hint">{hint}</div>}
            </div>
          );
        }
        return (
          <div className="field" key={f.name}>
            <label>{label}</label>
            <input
              type={f.input === "secret" ? "password" : f.input === "number" ? "number" : "text"}
              value={get(f)}
              placeholder={f.placeholder || f.default || ""}
              disabled={disabled}
              autoComplete={f.input === "secret" ? "new-password" : "off"}
              onChange={(e) => set(f, e.target.value)}
            />
            {hint && <div className="field-hint">{hint}</div>}
          </div>
        );
      })}
    </div>
  );
}

/** One product as an expandable card: name, transport, an enable switch
 *  and a delivery pill in the header; the settings form and a "Send test"
 *  button inside. */
export function DestinationCard({
  vendor,
  value,
  onChange,
  onTest,
  pill,
  disabled,
  defaultOpen,
  testLabel = "Send test",
}: {
  vendor: Vendor;
  value: DestinationConfig;
  onChange: (d: DestinationConfig) => void;
  onTest: (d: DestinationConfig) => Promise<DeliveryTestResult>;
  pill?: DeliveryPill | null;
  disabled?: boolean;
  defaultOpen?: boolean;
  testLabel?: string;
}) {
  const [open, setOpen] = useState(!!defaultOpen);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<DeliveryTestResult | null>(null);

  async function test() {
    setTesting(true);
    setResult(null);
    try {
      setResult(await onTest(value));
    } catch (e: any) {
      setResult({ success: false, detail: e.message || "Test failed" });
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 10 }}>
      <div className="flex-between" style={{ gap: 10, alignItems: "center" }}>
        <button
          type="button"
          className="catalog-group-toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          title={open ? "Collapse" : "Expand"}
        >
          {open ? "▾" : "▸"}
        </button>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="flex-row" style={{ gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
            <span style={{ fontWeight: 600 }}>{vendor.label}</span>
            {vendor.openSource && <span className="badge badge-neutral">open source</span>}
            <span className="cell-muted" style={{ fontSize: 12 }}>
              {vendor.product}
            </span>
            {pill && (
              <span className={`badge ${PILL_CLASS[pill.tone]}`} title={pill.title}>
                {pill.text}
              </span>
            )}
          </div>
          <div className="cell-muted" style={{ fontSize: 12 }}>
            {vendor.transport}
          </div>
        </div>
        <label className="checkbox-row" style={{ margin: 0, fontWeight: 500, whiteSpace: "nowrap" }}>
          <input
            type="checkbox"
            checked={value.enabled}
            disabled={disabled}
            onChange={(e) => {
              onChange({ ...value, enabled: e.target.checked });
              if (e.target.checked) setOpen(true);
            }}
          />{" "}
          {value.enabled ? "on" : "off"}
        </label>
      </div>
      {open && (
        <div style={{ marginTop: 14 }}>
          <div className="cell-muted" style={{ fontSize: 12.5, marginBottom: 10 }}>
            {vendor.description}
          </div>
          <VendorForm vendor={vendor} value={value} onChange={(fields) => onChange({ ...value, fields })} disabled={disabled} />
          <div className="flex-row" style={{ gap: 10, alignItems: "center" }}>
            <button type="button" className="btn btn-secondary btn-sm" disabled={disabled || testing} onClick={test}>
              {testing ? "Sending..." : testLabel}
            </button>
            {result && (
              <span className={`badge ${result.success ? "badge-success" : "badge-danger"}`} style={{ whiteSpace: "normal" }}>
                {result.detail}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
