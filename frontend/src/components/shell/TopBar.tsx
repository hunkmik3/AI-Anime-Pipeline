import { Link, NavLink, useLocation } from "react-router-dom";

import { useAuthStore } from "../../store/auth";
import { useInboxStore } from "../../store/inbox";
import { useProjectStore } from "../../store/project";
import { useRevalidate } from "../../hooks/useRevalidate";
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
  // Both while /me is still in flight, so the bar does not flicker a door
  // away and back on every load.
  const products = user?.products ?? { studio: true, flow: true, flow_manual: true };
  const logout = useAuthStore((s) => s.logout);
  const currentProjectId = useProjectStore((s) => s.currentProjectId);
  const { myWork, toReview } = useInboxStore();
  const refreshMe = useAuthStore((s) => s.refreshMe);
  const refreshInbox = useInboxStore((s) => s.refresh);
  const { pathname } = useLocation();

  // Keep the always-present header live: the budget readout and the work/review
  // badges came off stores fetched once at boot/mount, so they froze until an
  // F5. Revalidate on focus + a light interval, from the one bar every signed-in
  // page shows — so any spend, grant, submit or approval surfaces app-wide.
  useRevalidate(
    () => {
      void refreshMe();
      void refreshInbox();
    },
    { intervalMs: 20000, enabled: !!user },
  );
  // Everything that is not the other product is this one. Stated as an
  // exclusion rather than a list of studio prefixes, so a route added tomorrow
  // is claimed by default instead of leaving the bar looking unselected.
  const isStudio = !pathname.startsWith("/giantflow") && !pathname.startsWith("/admin");

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

      {/* TWO PRODUCTS, and nothing else.
          It used to read "Projects · Work · Review · Giantflow · Admin" — five
          peers, of which the first three are pages *inside* the thing the fourth
          is a sibling of. So the bar was answering "which page" and "which
          product" at once, in one row, with no way to tell which was which.
          Now it answers only "which product"; each product's own pages are a
          strip on the page (StudioNav / GiantflowNav).

          The counts move with them: an unread badge belongs next to the page it
          is counting, not two levels up where it cannot say what is waiting. The
          product tab still carries the total, so nothing goes unseen from here. */}
      {/* Only the products this account is actually in.
          An account whose whole standing is on the comic side was still offered
          Giant Studio, which then showed an empty project list — a door into a
          room that is not theirs reads as a fault in the app, not as a boundary.

          The Giantflow door leads somewhere different depending on who opens it.
          Somebody in a comic gets panel production. Somebody from Giant Studio
          gets the free-form image workspace — what this tool was before panel
          production was built on top of it — because that is a scratch space and
          holds nobody's comic. */}
      <nav className="topbar__nav" aria-label="Products">
        {products.studio ? (
          <TopLink to="/projects" label="Giant Studio" count={myWork + toReview} owns={isStudio} />
        ) : null}
        {products.flow ? (
          <TopLink to="/giantflow" label="Giantflow" />
        ) : products.flow_manual ? (
          <TopLink to="/giantflow/studio" label="Giantflow" />
        ) : null}
      </nav>

      <div className="topbar__right">
        {/* Admin is not a third product — it is the back office. It sits with
            the account controls, which is what it is about. */}
        {isAdmin ? (
          <Link to="/admin" className="topbar__chip" title="Admin console">
            Admin
          </Link>
        ) : null}
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
  owns,
}: {
  to: string;
  label: string;
  count?: number;
  /**
   * Whether this product owns the current route. `NavLink`'s own matching is
   * path-prefix, which cannot express "Giant Studio is current on /work and
   * /review and /shots/… too" — those share no prefix with /projects. Without
   * it the bar showed nothing selected on half the app.
   */
  owns?: boolean;
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `topbar__link${isActive || owns ? " is-active" : ""}`
      }
    >
      {label}
      {count ? <span className="topbar__badge">{count > 99 ? "99+" : count}</span> : null}
    </NavLink>
  );
}
