import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { usePoll } from "../hooks/usePoll";
import type { TopologyResponse } from "../api/types";
import { SankeyDiagram } from "../components/topology/SankeyDiagram";
import { HeatmapMatrix } from "../components/topology/HeatmapMatrix";

// The diagram fills whatever vertical room is left below the page header,
// down to the bottom of the viewport - measured directly (rather than via
// CSS height:100% chains through .app-shell/.main, which don't establish a
// definite height for a page to stretch into) so it stays correct across
// window resizes without relying on any particular parent layout.
const MIN_CARD_HEIGHT = 420;
const BOTTOM_GAP = 32;

type View = "sankey" | "matrix";

export function TopologyPage() {
  const cardRef = useRef<HTMLDivElement>(null);
  const [cardHeight, setCardHeight] = useState<number>(MIN_CARD_HEIGHT);
  const [view, setView] = useState<View>("sankey");
  const [data, setData] = useState<TopologyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    function measure() {
      const top = cardRef.current?.getBoundingClientRect().top ?? 0;
      const available = window.innerHeight - top - BOTTOM_GAP;
      setCardHeight(Math.max(MIN_CARD_HEIGHT, available));
    }
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  const load = useCallback(async () => {
    try {
      const res = await api.topology(24);
      setData(res);
      setError(null);
    } catch (e: any) {
      setError(e.message || "Failed to load topology");
    }
  }, []);

  usePoll(load, 30000);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Live Topology</h1>
          <p className="page-subtitle">Agent requests to Couchbase Agent Operations Manager for MCP tools and LLM Providers</p>
        </div>
        <div className="flex-row" style={{ gap: 8 }}>
          <button className={`btn btn-sm ${view === "sankey" ? "btn-primary" : "btn-secondary"}`} onClick={() => setView("sankey")}>
            Sankey
          </button>
          <button className={`btn btn-sm ${view === "matrix" ? "btn-primary" : "btn-secondary"}`} onClick={() => setView("matrix")}>
            Matrix
          </button>
        </div>
      </div>

      {error && <div className="error-note">{error}</div>}

      <div className="card" ref={cardRef} style={{ height: cardHeight }}>
        {!data && !error && <div className="loading-note">Loading topology...</div>}
        {data && (view === "sankey" ? <SankeyDiagram data={data} /> : <HeatmapMatrix data={data} />)}
      </div>
    </div>
  );
}
