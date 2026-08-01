import { Link, useLocation } from "react-router-dom";

/**
 * The way back to wherever this page was opened from.
 *
 * Object pages (project, series, episode) live in the app shell, so opening one
 * from the admin console drops you out of admin entirely — a different sidebar, a
 * different nav, and nothing saying how to get back to the tab you were on. The
 * navigation was right (a series belongs on the series page); the missing piece was
 * a way home.
 *
 * Renders nothing when the page was reached normally, so the crumb only appears
 * when there is somewhere non-obvious to return to.
 */
export function BackTo() {
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from;
  if (from !== "admin") return null;
  return (
    <Link to="/admin" className="backto">
      ← Back to admin
    </Link>
  );
}
