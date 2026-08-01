import { Link, NavLink } from "react-router-dom";

import { useAuthStore } from "../../store/auth";
import { useInboxStore } from "../../store/inbox";
import { useProjectStore } from "../../store/project";
import { NotificationBell } from "../NotificationBell";
import { Brand } from "./Brand";

/**
 * The app's top bar — one real row in the layout.
 *
 * What it replaces: two `position: fixed` pills hovering over the page (the
 * account widget top-right, a nav pill top-centre). Because they floated, every
 * routed page had to reserve a 40px empty band underneath so its own controls
 * wouldn't end up beneath them — a strip of nothing running across the whole app,
 * and content that never lined up with anything.
 *
 * Sitting in the grid instead means the band goes away, the bar can span the full
 * width, and page content starts where the page starts.
 *
 * Uses the app's existing tokens (--panel, --accent, --border) rather than its own
 * palette, so it reads as the same product as the canvas beside it.
 */

export function TopBar() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const currentProjectId = useProjectStore((s) => s.currentProjectId);
  const { myWork, toReview } = useInboxStore();

  if (!user) return null;

  const isAdmin = user.role === "admin";
  const available =
    user.available_usd ??
    (typeof user.budget_usd === "number"
      ? user.budget_usd - (user.spent_usd ?? 0)
      : undefined);

  return (
    <header className="topbar">
      <Brand />

      {/* Ordered by how often it's opened: the work first, the back office last.
          There is no separate producer console any more — a producer manages a
          project from the project's own pages, so Projects covers them too.

          Every item stays visible even when its count is zero. Hiding the empty
          ones would make the bar rearrange itself underneath someone the moment
          they're handed a piece of work; the badge is what signals "there's
          something here", not the item appearing. */}
      <nav className="topbar__nav" aria-label="Primary">
        <TopLink to="/projects" label="Projects" />
        <TopLink to="/work" label="Work" count={myWork} />
        <TopLink to="/review" label="Review" count={toReview} />
        <TopLink to="/giantflow" label="Giantflow" />
        {isAdmin ? <TopLink to="/admin" label="Admin" /> : null}
      </nav>

      <div className="topbar__right">
        {currentProjectId ? (
          <Link
            to={`/projects/${currentProjectId}/library`}
            className="topbar__chip"
            title="Asset library for the current project"
          >
            Library
          </Link>
        ) : null}
        {typeof available === "number" ? (
          <span className="topbar__budget" title="Budget remaining">
            ${available.toFixed(2)}
          </span>
        ) : null}
        <NotificationBell />
        <span className="topbar__me" title={user.username}>
          {user.display_name || user.username}
          {isAdmin ? <em>admin</em> : null}
        </span>
        <button className="topbar__signout" onClick={() => logout()}>
          Sign out
        </button>
      </div>
    </header>
  );
}

function TopLink({
  to,
  label,
  count,
}: {
  to: string;
  label: string;
  count?: number;
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `topbar__link${isActive ? " is-active" : ""}`
      }
    >
      {label}
      {count ? <span className="topbar__badge">{count > 99 ? "99+" : count}</span> : null}
    </NavLink>
  );
}
