import type { CSSProperties } from "react";
import type { TopologyLlmProvider, TopologyResponse, TopologyServer } from "../../api/types";
import { DEFAULT_AGENT_COLOR, clamp, defaultColorFor, nodeKey, relativeTime, truncate, useMeasuredBox, useNodeColors } from "./topologyUtils";

// A two-column (agents -> MCP servers & LLM providers) Sankey: node height
// is proportional to that node's total call volume in the window rather
// than a uniform row height, and ribbons between the columns are
// proportioned on both ends by the specific (agent, destination) edge
// weight - so unlike the plain node-link diagram, this shows not just
// *that* an agent is calling a destination but roughly *how much* of each
// node's total traffic that one connection accounts for.
//
// Every node carries its own user-assignable color (see useNodeColors in
// topologyUtils) rather than a fixed per-category color, and each ribbon
// is a gradient from its source agent's color to its destination's color -
// so the diagram reads as a colorful, hand-authored flow chart rather than
// a generated one, and the palette is whatever the viewer wants it to be.
const LEFT_X = 10;
const RIGHT_PAD = 10;
const TOP_PAD = 34;
const BOTTOM_PAD = 12;
const GAP = 10;
const SWATCH_SIZE = 15;

type RightEntry =
  | { kind: "server"; id: string; label: string; value: number; data: TopologyServer }
  | { kind: "llm"; id: string; label: string; value: number; data: TopologyLlmProvider };

interface Seg {
  top: number;
  height: number;
}

/** A DOM-safe id for the per-edge ribbon gradient - role/ids can contain
 * spaces or punctuation that aren't valid in an SVG element id. */
function gradientId(role: string, targetKind: string, targetId: string): string {
  const safe = (s: string) => s.replace(/[^a-zA-Z0-9_-]/g, "_");
  return `ribbon-grad-${safe(role)}-${targetKind}-${safe(targetId)}`;
}

/** Stacks `values.length` nodes top-to-bottom within `available` px,
 * giving every node - even a zero-value (idle) one - at least a small
 * minimum height so it stays visible, then distributing the remaining
 * space proportionally by value. Falls back to an even split if there
 * isn't even room for the minimums. */
function stackColumn(values: number[], available: number): Seg[] {
  const n = values.length;
  if (n === 0) return [];
  const totalGap = GAP * Math.max(0, n - 1);
  const usable = Math.max(0, available - totalGap);
  const idealMin = clamp(available * 0.05, 22, 40);
  const minH = n * idealMin > usable ? Math.max(2, usable / n) : idealMin;
  const reserved = minH * n;
  const flex = Math.max(0, usable - reserved);
  const totalValue = values.reduce((a, b) => a + b, 0);
  const heights = values.map((v) => minH + (totalValue > 0 ? (v / totalValue) * flex : flex / n));

  const segs: Seg[] = [];
  let y = TOP_PAD;
  for (const h of heights) {
    segs.push({ top: y, height: h });
    y += h + GAP;
  }
  return segs;
}

export function SankeyDiagram({ data }: { data: TopologyResponse }) {
  const { ref: boxRef, box } = useMeasuredBox<HTMLDivElement>(1000, 400);
  const { getColor, setColor, resetColors } = useNodeColors();

  const agents = data.agents;
  const rightEntries: RightEntry[] = [
    ...data.servers.map((s): RightEntry => ({ kind: "server", id: s.server_id, label: s.label, value: s.calls, data: s })),
    ...data.llm_providers.map((p): RightEntry => ({ kind: "llm", id: p.provider, label: p.label, value: p.calls, data: p })),
  ];

  // Destinations cycle the varied default palette; agents all default to
  // DEFAULT_AGENT_COLOR instead (see topologyUtils), so there's no longer
  // an agent-column index to offset destinations past.
  const destColorKey = (entry: RightEntry) => nodeKey("dest", `${entry.kind}:${entry.id}`);
  const destDefaultColor = (i: number) => defaultColorFor(i);

  const nodeW = clamp(box.w * 0.15, 100, 170);
  const rightX = Math.max(nodeW + 40, box.w - RIGHT_PAD - nodeW);
  const available = Math.max(40, box.h - TOP_PAD - BOTTOM_PAD);

  const leftValues = agents.map((a) => a.tool_calls + a.llm_calls);
  const rightValues = rightEntries.map((e) => e.value);
  const leftSegs = stackColumn(leftValues, available);
  const rightSegs = stackColumn(rightValues, available);

  const leftIndex = new Map(agents.map((a, i) => [a.role, i]));
  const rightIndex = new Map(rightEntries.map((e, i) => [e.id, i]));

  type EdgeRef = { role: string; targetId: string; targetKind: "server" | "llm"; count: number; last_at: string | null };
  const allEdges: EdgeRef[] = [
    ...data.edges.agent_server
      .filter((e) => e.server_id)
      .map((e) => ({ role: e.role, targetId: e.server_id as string, targetKind: "server" as const, count: e.count, last_at: e.last_at })),
    ...data.edges.agent_llm
      .filter((e) => e.provider)
      .map((e) => ({ role: e.role, targetId: e.provider as string, targetKind: "llm" as const, count: e.count, last_at: e.last_at })),
  ];

  // Sub-divide each node's height band into one segment per edge touching
  // it, proportioned by that edge's share of the node's total - ordered by
  // the *other* column's position, which keeps ribbons from a shared
  // source/destination roughly aligned instead of crossing more than
  // necessary.
  const leftSegMap = new Map<string, Seg>();
  const rightSegMap = new Map<string, Seg>();
  const key = (role: string, targetId: string) => `${role} ${targetId}`;

  agents.forEach((a, i) => {
    const seg = leftSegs[i];
    const outgoing = allEdges
      .filter((e) => e.role === a.role)
      .slice()
      .sort((x, y) => (rightIndex.get(x.targetId) ?? 0) - (rightIndex.get(y.targetId) ?? 0));
    const total = outgoing.reduce((s, e) => s + e.count, 0);
    let y = seg.top;
    for (const e of outgoing) {
      const h = total > 0 ? (e.count / total) * seg.height : 0;
      leftSegMap.set(key(e.role, e.targetId), { top: y, height: h });
      y += h;
    }
  });

  rightEntries.forEach((entry, i) => {
    const seg = rightSegs[i];
    const incoming = allEdges
      .filter((e) => e.targetId === entry.id)
      .slice()
      .sort((x, y) => (leftIndex.get(x.role) ?? 0) - (leftIndex.get(y.role) ?? 0));
    const total = incoming.reduce((s, e) => s + e.count, 0);
    let y = seg.top;
    for (const e of incoming) {
      const h = total > 0 ? (e.count / total) * seg.height : 0;
      rightSegMap.set(key(e.role, e.targetId), { top: y, height: h });
      y += h;
    }
  });

  const titleFont = clamp(box.h * 0.024, 10, 13.5);
  const subFont = clamp(box.h * 0.019, 8, 11.5);
  const headerFont = clamp(box.h * 0.022, 9, 12.5);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div ref={boxRef} style={{ flex: 1, minHeight: 0 }}>
        <svg viewBox={`0 0 ${box.w} ${box.h}`} width={box.w} height={box.h} style={{ display: "block" }}>
          <text x={LEFT_X} y={20} fontSize={headerFont} fontWeight={700} fill="var(--text-muted)">
            AGENTS
          </text>
          <text x={rightX} y={20} fontSize={headerFont} fontWeight={700} fill="var(--text-muted)">
            MCP TOOLS &amp; LLMS
          </text>

          {/* Each ribbon is a gradient from its source agent's color to its
              destination's color, so a flow visibly connects the two ends
              it links rather than reading as a single flat band. */}
          <defs>
            {allEdges.map((e) => {
              const l = leftSegMap.get(key(e.role, e.targetId));
              const r = rightSegMap.get(key(e.role, e.targetId));
              if (!l || !r || (l.height <= 0 && r.height <= 0)) return null;
              const entryIdx = rightIndex.get(e.targetId) ?? 0;
              const fromColor = getColor(nodeKey("agent", e.role), DEFAULT_AGENT_COLOR);
              const toColor = getColor(nodeKey("dest", `${e.targetKind}:${e.targetId}`), destDefaultColor(entryIdx));
              const gradId = gradientId(e.role, e.targetKind, e.targetId);
              return (
                <linearGradient
                  key={gradId}
                  id={gradId}
                  gradientUnits="userSpaceOnUse"
                  x1={LEFT_X + nodeW}
                  x2={rightX}
                  y1={0}
                  y2={0}
                >
                  <stop offset="0%" stopColor={fromColor} />
                  <stop offset="100%" stopColor={toColor} />
                </linearGradient>
              );
            })}
          </defs>

          {/* ribbons */}
          {allEdges.map((e) => {
            const l = leftSegMap.get(key(e.role, e.targetId));
            const r = rightSegMap.get(key(e.role, e.targetId));
            if (!l || !r || (l.height <= 0 && r.height <= 0)) return null;
            const x1 = LEFT_X + nodeW;
            const x2 = rightX;
            const midX = (x1 + x2) / 2;
            const d =
              `M ${x1} ${l.top} ` +
              `C ${midX} ${l.top}, ${midX} ${r.top}, ${x2} ${r.top} ` +
              `L ${x2} ${r.top + r.height} ` +
              `C ${midX} ${r.top + r.height}, ${midX} ${l.top + l.height}, ${x1} ${l.top + l.height} Z`;
            const gradId = gradientId(e.role, e.targetKind, e.targetId);
            return (
              <path key={`${e.role}->${e.targetKind}:${e.targetId}`} d={d} fill={`url(#${gradId})`} fillOpacity={0.88} stroke="none">
                <title>
                  {e.role} → {e.targetId}
                  {"\n"}
                  {e.count} call{e.count === 1 ? "" : "s"} ({data.window_hours}h)
                  {"\n"}
                  {relativeTime(e.last_at)}
                </title>
              </path>
            );
          })}

          {/* agent nodes */}
          {agents.map((a, i) => {
            const seg = leftSegs[i];
            const total = a.tool_calls + a.llm_calls;
            const showSub = seg.height >= 26;
            const isIdle = total <= 0;
            const colorKey = nodeKey("agent", a.role);
            const color = getColor(colorKey, DEFAULT_AGENT_COLOR);
            return (
              <g key={`agent-${a.role}`}>
                <rect
                  x={LEFT_X}
                  y={seg.top}
                  width={nodeW}
                  height={seg.height}
                  rx={5}
                  fill={isIdle ? "var(--card-bg)" : color}
                  stroke={color}
                  strokeWidth={1.3}
                  strokeDasharray={isIdle ? "4 4" : undefined}
                />
                <text
                  x={LEFT_X + 10}
                  y={showSub ? seg.top + titleFont + 6 : seg.top + seg.height / 2 + titleFont * 0.35}
                  fontSize={titleFont}
                  fontWeight={700}
                  fill={isIdle ? "var(--text)" : "#fff"}
                >
                  {truncate(a.role, 16)}
                </text>
                {showSub && (
                  <text
                    x={LEFT_X + 10}
                    y={seg.top + seg.height - 8}
                    fontSize={subFont}
                    fill={isIdle ? "var(--text-muted)" : "rgba(255,255,255,0.85)"}
                  >
                    {total > 0 ? `${total} call${total === 1 ? "" : "s"} (24h)` : "idle"}
                  </text>
                )}
                <title>
                  {a.description}
                  {"\n"}Tool calls: {a.tool_calls} · LLM calls: {a.llm_calls}
                  {"\n"}
                  {relativeTime(a.last_active_at)}
                </title>
                <foreignObject x={LEFT_X + nodeW - SWATCH_SIZE - 5} y={seg.top + 5} width={SWATCH_SIZE} height={SWATCH_SIZE}>
                  <input
                    type="color"
                    value={color}
                    onChange={(ev) => setColor(colorKey, ev.target.value)}
                    title={`Choose a color for ${a.role}`}
                    style={swatchStyle}
                  />
                </foreignObject>
              </g>
            );
          })}

          {/* right column nodes */}
          {rightEntries.map((entry, i) => {
            const seg = rightSegs[i];
            const showSub = seg.height >= 26;
            const isIdle = entry.value <= 0;
            const colorKey = destColorKey(entry);
            const color = getColor(colorKey, destDefaultColor(i));
            const subtitle =
              entry.kind === "server"
                ? entry.value > 0
                  ? `${entry.value} call${entry.value === 1 ? "" : "s"} (24h) · ${entry.data.tool_count} tools`
                  : `${entry.data.tool_count} tools · idle`
                : !entry.data.configured
                ? `${entry.data.vendor} · no API key`
                : entry.value > 0
                ? `${entry.value} call${entry.value === 1 ? "" : "s"} (24h)`
                : `${entry.data.vendor} · configured, idle`;
            return (
              <g key={`right-${entry.kind}-${entry.id}`}>
                <rect
                  x={rightX}
                  y={seg.top}
                  width={nodeW}
                  height={seg.height}
                  rx={5}
                  fill={isIdle ? "var(--card-bg)" : color}
                  stroke={color}
                  strokeWidth={1.3}
                  strokeDasharray={isIdle ? "4 4" : undefined}
                />
                <text
                  x={rightX + 10}
                  y={showSub ? seg.top + titleFont + 6 : seg.top + seg.height / 2 + titleFont * 0.35}
                  fontSize={titleFont}
                  fontWeight={700}
                  fill={isIdle ? "var(--text)" : "#fff"}
                >
                  {truncate(entry.label, 20)}
                  {entry.kind === "llm" && entry.data.is_default ? " ★" : ""}
                </text>
                {showSub && (
                  <text
                    x={rightX + 10}
                    y={seg.top + seg.height - 8}
                    fontSize={subFont}
                    fill={isIdle ? "var(--text-muted)" : "rgba(255,255,255,0.85)"}
                  >
                    {subtitle}
                  </text>
                )}
                <title>
                  {entry.label}
                  {"\n"}
                  {relativeTime(entry.data.last_active_at)}
                </title>
                <foreignObject x={rightX + nodeW - SWATCH_SIZE - 5} y={seg.top + 5} width={SWATCH_SIZE} height={SWATCH_SIZE}>
                  <input
                    type="color"
                    value={color}
                    onChange={(ev) => setColor(colorKey, ev.target.value)}
                    title={`Choose a color for ${entry.label}`}
                    style={swatchStyle}
                  />
                </foreignObject>
              </g>
            );
          })}
        </svg>
      </div>

      <div
        className="flex-row"
        style={{ gap: 18, marginTop: 6, flexWrap: "wrap", alignItems: "center", fontSize: 11.5, color: "var(--text-muted)", flexShrink: 0 }}
      >
        <span>
          Node height = call volume, ribbon = gradient from the agent's color to the destination's color, ribbon
          thickness = call volume in the last {data.window_hours}h · dashed = no activity in that window. Click a
          node's color swatch to assign its color.
        </span>
        <button className="btn btn-sm btn-secondary" style={{ marginLeft: "auto" }} onClick={resetColors}>
          Reset colors
        </button>
      </div>
    </div>
  );
}

const swatchStyle: CSSProperties = {
  width: SWATCH_SIZE,
  height: SWATCH_SIZE,
  padding: 0,
  border: "1px solid rgba(255,255,255,0.6)",
  borderRadius: 4,
  background: "none",
  cursor: "pointer",
};
