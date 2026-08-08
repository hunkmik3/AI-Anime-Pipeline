import { NavLink } from "react-router-dom";

import { useNoticeCount } from "../store/giantflowNotices";
import { useGiantflowRole } from "../store/giantflowRole";
import { ViewAsBar } from "./ViewAsBar";

/**
 * The doors into giantflow, always in the same spot, and the role preview.
 *
 * Project is where work is organised, Review is the PM's pile, My work is what
 * came back to the artist. They are separate pages on purpose — a reviewer asks
 * "what is waiting on me, across everyone" and an artist asks "what came back to
 * me, and why", and neither question is answered by browsing the project tree.
 *
 * Notifications sits first because it is the only tab that answers "what should
 * I be doing" without knowing where to look — the other four all assume you
 * already know which pile your work is in.
 *
 * Review is hidden from anyone who cannot rule on a panel. Hiding it is not the
 * protection — the server is — but a tab leading to a page of buttons you may
 * not press is worse than no tab.
 */
export function GiantflowNav() {
  const { can } = useGiantflowRole();
  const { unread, todo } = useNoticeCount();
  // Unread beats to-do on the badge: one is news, the other is a standing
  // workload. Showing "12" forever because twelve panels are unstarted trains
  // people to ignore the number, and then the new one goes unseen too.
  const badge = unread || todo;

  return (
    <div className="pn__navbar">
      <nav className="pn__nav">
        <NavLink
          to="/giantflow/notices"
          className={({ isActive }) => `pn__nav-tab${isActive ? " is-on" : ""}`}
        >
          Notifications
          {badge > 0 ? (
            <span className={`pn__nav-badge${unread ? " is-new" : ""}`}>
              {badge > 99 ? "99+" : badge}
            </span>
          ) : null}
        </NavLink>
        <NavLink end to="/giantflow" className={({ isActive }) => `pn__nav-tab${isActive ? " is-on" : ""}`}>
          Projects
        </NavLink>
        <NavLink to="/giantflow/panels" className={({ isActive }) => `pn__nav-tab${isActive ? " is-on" : ""}`}>
          All panels
        </NavLink>
        {can("panel.review") ? (
          <NavLink to="/giantflow/review" className={({ isActive }) => `pn__nav-tab${isActive ? " is-on" : ""}`}>
            Review
          </NavLink>
        ) : null}
        <NavLink to="/giantflow/my-work" className={({ isActive }) => `pn__nav-tab${isActive ? " is-on" : ""}`}>
          My work
        </NavLink>
      </nav>
      <ViewAsBar />
    </div>
  );
}
