import { useEffect, useState } from "react";
import { getHealth, type HealthResponse } from "../api/client";

function formatAge(sec: number | null | undefined): string | null {
  if (sec == null) return null;
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}m`;
}

/**
 * Floating top-left chip showing agent / extension / token / request
 * counters. Chrome (panel bg + border + radius) matches the
 * AddNodePalette so the two overlays read as a family. Dot semantics:
 *   • ok      → --success (green)
 *   • err     → --error  (red)
 *   • unknown → --muted  (grey)
 * Yellow / --warn isn't used yet — reserved for a future "degraded"
 * state (e.g. rate-limit hit, partial WS reconnect).
 */
export function StatusBar() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [agentOk, setAgentOk] = useState(false);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const h = await getHealth();
        if (!alive) return;
        setAgentOk(h.ok);
        setHealth(h);
      } catch {
        if (!alive) return;
        setAgentOk(false);
        setHealth(null);
      }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const extConnected = health?.extension_connected ?? null;
  const stats = health?.ws_stats;
  const tokenAge = formatAge(stats?.token_age_s ?? null);
  const reqCount = stats?.request_count ?? 0;
  const okCount = stats?.success_count ?? 0;
  const failCount = stats?.failed_count ?? 0;

  const agentDotClass = agentOk ? "statusbar__dot--ok" : "statusbar__dot--err";
  const extDotClass =
    extConnected === null
      ? "statusbar__dot--unknown"
      : extConnected
        ? "statusbar__dot--ok"
        : "statusbar__dot--err";

  return (
    <div className="statusbar" role="status" aria-live="polite">
      <span className="statusbar__group">
        <span className={`statusbar__dot ${agentDotClass}`} aria-hidden="true" />
        <span className="statusbar__group-label">agent</span>
      </span>

      <span className="statusbar__divider" aria-hidden="true" />

      <span className="statusbar__group">
        <span className={`statusbar__dot ${extDotClass}`} aria-hidden="true" />
        <span className="statusbar__group-label">extension</span>
      </span>

      {extConnected && stats?.flow_key_present && tokenAge && (
        <>
          <span className="statusbar__divider" aria-hidden="true" />
          <span className="statusbar__metric">token {tokenAge}</span>
        </>
      )}

      {extConnected && reqCount > 0 && (
        <>
          <span className="statusbar__divider" aria-hidden="true" />
          <span className="statusbar__metric">
            req {reqCount}
            <span className="statusbar__metric--ok"> · ✓{okCount}</span>
            {failCount > 0 && (
              <span className="statusbar__metric--err"> · ✗{failCount}</span>
            )}
          </span>
        </>
      )}
    </div>
  );
}
