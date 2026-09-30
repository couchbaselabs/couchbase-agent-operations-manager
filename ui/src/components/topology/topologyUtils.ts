import { useCallback, useEffect, useRef, useState } from "react";

// Shared helpers for the topology views (SankeyDiagram, HeatmapMatrix) so
// neither has to re-derive the same small formatting/layout utilities.

// --- User-assignable node colors -------------------------------------
//
// Every AGENTS node and every MCP TOOLS & LLMS node can be given its own
// color (see the swatch on each node in SankeyDiagram), so ribbons read as
// a gradient from the color of the agent that made the call to the color
// of the thing it called - closer to a hand-designed flow diagram than a
// generated chart. Assignments are a per-browser display preference (like
// the theme toggle in store/theme.ts), so they're persisted in
// localStorage rather than sent to the backend.
const NODE_COLOR_STORAGE_KEY = "aom.topology.nodeColors";

// A vivid, varied palette used before the user customizes anything -
// deliberately not tied to the app's muted UI theme tokens, since these
// colors are meant to carry identity at a glance across a lot of nodes.
// Cycled by a node's position within its own column.
const DEFAULT_NODE_PALETTE = [
  "#8b5cf6", // violet
  "#34d399", // mint
  "#38bdf8", // sky
  "#f472b6", // pink
  "#facc15", // yellow
  "#fb923c", // orange
  "#22d3ee", // cyan
  "#a3e635", // lime
  "#c084fc", // purple
  "#fb7185", // rose
];

export type NodeKind = "agent" | "dest";

export function nodeKey(kind: NodeKind, id: string): string {
  return `${kind}:${id}`;
}

// Agents all default to the same red - the app's own brand red, already
// used for the "Sankey" view toggle and other primary actions - rather
// than each agent getting a different hue from the varied palette below.
// MCP servers / LLM providers still cycle that varied palette, since they
// don't share a single existing brand color the way agents now do.
export const DEFAULT_AGENT_COLOR = "#ea2328";

export function defaultColorFor(index: number): string {
  return DEFAULT_NODE_PALETTE[index % DEFAULT_NODE_PALETTE.length];
}

function readStoredColors(): Record<string, string> {
  try {
    const raw = localStorage.getItem(NODE_COLOR_STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function writeStoredColors(colors: Record<string, string>) {
  try {
    localStorage.setItem(NODE_COLOR_STORAGE_KEY, JSON.stringify(colors));
  } catch {
    // Best-effort - a full or blocked localStorage just means assignments
    // don't persist across reloads, not a functional failure.
  }
}

/** Tracks user-assigned colors for Sankey nodes, keyed by nodeKey(). Falls
 * back to the caller-supplied color (DEFAULT_AGENT_COLOR for agents,
 * defaultColorFor(index) for destinations) until the user picks their own
 * color for that node. */
export function useNodeColors() {
  const [colors, setColors] = useState<Record<string, string>>(() => readStoredColors());

  const getColor = useCallback((key: string, fallbackColor: string) => colors[key] || fallbackColor, [colors]);

  const setColor = useCallback((key: string, color: string) => {
    setColors((prev) => {
      const next = { ...prev, [key]: color };
      writeStoredColors(next);
      return next;
    });
  }, []);

  const resetColors = useCallback(() => {
    setColors({});
    writeStoredColors({});
  }, []);

  return { getColor, setColor, resetColors };
}

export function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}

export function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "no activity in this window";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "no activity in this window";
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return `active ${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `active ${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `active ${hours}h ago`;
  return `active ${Math.floor(hours / 24)}d ago`;
}

/** Tracks the rendered pixel size of a DOM element via ResizeObserver, so a
 * view can size itself (an SVG viewBox, a canvas, ...) to exactly fill
 * whatever box its parent gives it instead of being locked to a fixed
 * aspect ratio. Returns a ref to attach to the element plus the last
 * measured {w, h} (defaulting to fallbackW/fallbackH before the first
 * measurement lands, so the initial render has sane numbers to work with
 * rather than 0x0). */
export function useMeasuredBox<T extends HTMLElement>(fallbackW: number, fallbackH: number) {
  const ref = useRef<T>(null);
  const [box, setBox] = useState<{ w: number; h: number }>({ w: fallbackW, h: fallbackH });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setBox({ w: width, h: height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return { ref, box };
}
