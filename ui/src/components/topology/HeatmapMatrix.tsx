import type { CSSProperties } from "react";
import type { TopologyLlmProvider, TopologyResponse, TopologyServer } from "../../api/types";
import { relativeTime, truncate } from "./topologyUtils";

// Rows = agents, columns = MCP servers + LLM providers, cell = call volume
// between that pair in the window. A matrix scales better than a node-link
// or Sankey view once there are many destinations (labels don't get
// squeezed and truncated), and it's the easiest layout to scan for "who is
// NOT reaching what" - an RBAC gap shows up as an entire dim row or column
// rather than something you have to trace through crossing lines to spot.
type RightEntry =
  | { kind: "server"; id: string; label: string; data: TopologyServer }
  | { kind: "llm"; id: string; label: string; data: TopologyLlmProvider };

export function HeatmapMatrix({ data }: { data: TopologyResponse }) {
  const agents = data.agents;
  const rightEntries: RightEntry[] = [
    ...data.servers.map((s): RightEntry => ({ kind: "server", id: s.server_id, label: s.label, data: s })),
    ...data.llm_providers.map((p): RightEntry => ({ kind: "llm", id: p.provider, label: p.label, data: p })),
  ];

  const cellValue = new Map<string, { count: number; last_at: string | null }>();
  for (const e of data.edges.agent_server) {
    if (e.server_id) cellValue.set(`${e.role} ${e.server_id}`, { count: e.count, last_at: e.last_at });
  }
  for (const e of data.edges.agent_llm) {
    if (e.provider) cellValue.set(`${e.role} ${e.provider}`, { count: e.count, last_at: e.last_at });
  }

  const maxCount = Math.max(1, ...Array.from(cellValue.values()).map((v) => v.count));

  function intensity(count: number): { bg: string; text: string } {
    if (count <= 0) return { bg: "transparent", text: "var(--text-faint)" };
    // Square-root scale so a handful of mid-volume cells don't all read as
    // near-white next to one very hot cell - matches the log-ish scaling
    // the other topology views use for line/ribbon thickness.
    const frac = Math.sqrt(count / maxCount);
    const alpha = 0.12 + frac * 0.68;
    return { bg: `rgba(45, 212, 200, ${alpha.toFixed(3)})`, text: frac > 0.55 ? "var(--text)" : "var(--text-muted)" };
  }

  return (
    <div style={{ height: "100%", overflow: "auto" }}>
      <table style={{ borderCollapse: "collapse", width: "100%", minWidth: 640 }}>
        <thead>
          <tr>
            <th style={headerCellStyle("left")}>Agent \ Destination</th>
            {rightEntries.map((entry) => (
              <th key={`${entry.kind}-${entry.id}`} style={headerCellStyle("center")}>
                <div style={{ fontWeight: 700 }}>{truncate(entry.label, 16)}</div>
                <div style={{ fontWeight: 400, color: "var(--text-faint)", fontSize: 10 }}>
                  {entry.kind === "server" ? "MCP server" : "LLM provider"}
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {agents.map((a) => (
            <tr key={a.role}>
              <th style={rowHeaderStyle}>
                <div style={{ fontWeight: 700 }}>{a.role}</div>
                <div style={{ fontWeight: 400, color: "var(--text-faint)", fontSize: 10 }}>
                  {relativeTime(a.last_active_at)}
                </div>
              </th>
              {rightEntries.map((entry) => {
                const cell = cellValue.get(`${a.role} ${entry.id}`);
                const count = cell?.count ?? 0;
                const { bg, text } = intensity(count);
                const tooltip = cell
                  ? `${a.role} → ${entry.label}\n${count} call${count === 1 ? "" : "s"} (${data.window_hours}h)\n${relativeTime(
                      cell.last_at
                    )}`
                  : `${a.role} → ${entry.label}\nNo calls in the last ${data.window_hours}h`;
                return (
                  <td
                    key={`${a.role}-${entry.kind}-${entry.id}`}
                    style={{ ...cellStyle, background: bg, color: text }}
                    title={tooltip}
                  >
                    {count > 0 ? count.toLocaleString() : "–"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>

      <div
        className="flex-row"
        style={{ gap: 18, marginTop: 12, flexWrap: "wrap", fontSize: 11.5, color: "var(--text-muted)" }}
      >
        <span className="flex-row" style={{ gap: 6, alignItems: "center" }}>
          <span
            style={{
              display: "inline-block",
              width: 60,
              height: 10,
              borderRadius: 3,
              background: "linear-gradient(90deg, rgba(45,212,200,0.12), rgba(45,212,200,0.8))",
            }}
          />
          Cell shade = call volume in the last {data.window_hours}h (– = no calls)
        </span>
      </div>
    </div>
  );
}

const cellStyle: CSSProperties = {
  textAlign: "center",
  padding: "10px 12px",
  border: "1px solid var(--border-soft)",
  fontSize: 12.5,
  fontVariantNumeric: "tabular-nums",
  minWidth: 90,
};

const rowHeaderStyle: CSSProperties = {
  textAlign: "left",
  padding: "8px 14px 8px 4px",
  border: "1px solid var(--border-soft)",
  background: "var(--card-bg-alt)",
  fontSize: 12.5,
  whiteSpace: "nowrap",
  position: "sticky",
  left: 0,
};

function headerCellStyle(align: "left" | "center"): CSSProperties {
  return {
    textAlign: align,
    padding: "8px 12px",
    border: "1px solid var(--border-soft)",
    background: "var(--card-bg-alt)",
    fontSize: 12,
    whiteSpace: "nowrap",
    position: align === "left" ? "sticky" : undefined,
    left: align === "left" ? 0 : undefined,
    zIndex: align === "left" ? 1 : undefined,
  };
}
