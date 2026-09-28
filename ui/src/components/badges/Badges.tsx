type Severity = "critical" | "high" | "medium" | "low" | "info" | string;
type Decision = "ALLOW" | "DENY" | "ERROR" | string;
type Trust = "trusted" | "untrusted" | string;

const KNOWN_SEVERITIES = new Set(["critical", "high", "medium", "low", "info"]);

export function SeverityBadge({
  severity,
  solid = false,
  displayAs,
}: {
  severity: Severity;
  solid?: boolean;
  /** Optional text override per severity value, e.g. {critical: "high"} -
   * relabels the badge's displayed word without changing its underlying
   * value or color, so callers that need a different word for the same
   * severity (tool risk_level vs. finding/hijack severity) don't have to
   * fork the component. */
  displayAs?: Partial<Record<string, string>>;
}) {
  const known = KNOWN_SEVERITIES.has(severity);
  const cls = solid && severity === "critical" ? "badge-solid-critical" : known ? `badge-${severity}` : "badge-neutral";
  const label = displayAs?.[severity] ?? severity;
  return <span className={`badge ${cls}`}>{label}</span>;
}

export function DecisionBadge({ decision }: { decision: Decision }) {
  const cls = decision === "ALLOW" ? "badge-allow" : decision === "DENY" ? "badge-deny" : "badge-error";
  return <span className={`badge ${cls}`}>{decision}</span>;
}

export function TrustBadge({ trust }: { trust: Trust }) {
  const cls = trust === "trusted" ? "badge-trusted" : "badge-untrusted";
  return <span className={`badge ${cls}`}>{trust}</span>;
}

export function TagBadge({ label }: { label: string }) {
  return <span className="badge badge-neutral">{label}</span>;
}
