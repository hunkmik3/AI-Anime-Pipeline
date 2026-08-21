import { useEffect, useRef } from "react";

/**
 * Keep a view live without a manual F5.
 *
 * Re-runs `fn` when the user comes back to the tab/window (`focus` +
 * `visibilitychange`) and, optionally, on a light interval WHILE the tab is
 * visible (a hidden tab shouldn't poll). Lifted verbatim from the pattern that
 * was hand-copied into AdminPage / useActivityFeed / the provider badges, so
 * every screen refreshes the same way from one place.
 *
 * `fn` is kept in a ref, so callers can pass an inline closure without
 * re-subscribing the listeners on every render.
 *
 *   useRevalidate(() => void load(), { intervalMs: 20000 });
 */
export function useRevalidate(
  fn: () => void | Promise<void>,
  opts: { intervalMs?: number; enabled?: boolean } = {},
): void {
  const { intervalMs = 0, enabled = true } = opts;
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return;
    const tick = () => {
      void fnRef.current();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") tick();
    };
    window.addEventListener("focus", tick);
    document.addEventListener("visibilitychange", onVisible);
    let id: number | undefined;
    if (intervalMs > 0) {
      id = window.setInterval(() => {
        // Don't burn requests on a backgrounded tab; focus will catch it up.
        if (document.visibilityState === "visible") tick();
      }, intervalMs);
    }
    return () => {
      window.removeEventListener("focus", tick);
      document.removeEventListener("visibilitychange", onVisible);
      if (id !== undefined) window.clearInterval(id);
    };
  }, [enabled, intervalMs]);
}
