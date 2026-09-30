import { useEffect, useRef } from "react";

/**
 * Run `load` now, then on an interval - but never twice at once.
 *
 * The pages in this dashboard sit open on a wall display or a second
 * monitor and refresh themselves every 8-30 seconds. A bare
 * `setInterval(load, ms)` keeps firing whether or not the previous refresh
 * ever came back, so the moment the appliance slows down - the exact moment
 * you least want more load - each open page starts stacking requests it is
 * still waiting on. Several of these refreshes fan out into multiple N1QL
 * queries server-side, so a page left open through a slow patch can put a
 * backlog on the API that outlives the slow patch that caused it.
 *
 * Skipping a tick while one is still in flight makes the poll
 * self-limiting: a page can never have more than one refresh outstanding,
 * and a slow API simply refreshes less often instead of being asked harder.
 *
 * `load` is read from a ref, so a caller may pass a fresh closure on every
 * render (the usual `useCallback` with dependencies) without restarting the
 * timer - only a change of `intervalMs` or `enabled` does that.
 */
export function usePoll(load: () => void | Promise<unknown>, intervalMs: number, enabled = true) {
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;
    let inFlight = false;

    const tick = async () => {
      // A dashboard left open in a background tab (or on a locked screen)
      // overnight would otherwise keep re-running the heaviest queries in
      // the appliance every few seconds for nobody. Hidden tabs skip their
      // ticks and refresh immediately when they become visible again.
      if (cancelled || inFlight || document.visibilityState === "hidden") return;
      inFlight = true;
      try {
        await loadRef.current();
      } catch {
        // Error state belongs to the caller's own load function; a rejection
        // here must not stop the timer.
      } finally {
        inFlight = false;
      }
    };

    const onVisible = () => {
      if (document.visibilityState === "visible") void tick();
    };

    void tick();
    const id = setInterval(() => void tick(), intervalMs);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [intervalMs, enabled]);
}
