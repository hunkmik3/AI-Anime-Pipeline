import { NavLink } from "react-router-dom";

import { useInboxStore } from "../../store/inbox";

/**
 * The doors into Giant Studio — the same strip giantflow has, one product over.
 *
 * These three used to sit in the top bar beside Giantflow, which put a product
 * and three of another product's pages on one row: "Projects · Work · Review ·
 * Giantflow" reads as four peers, when the first three are all inside the thing
 * the fourth is a sibling of. The top bar now switches between the two products
 * and this strip navigates within one, so the level you are at is always legible
 * from the chrome.
 *
 * Deliberately the same classes as `GiantflowNav` rather than a parallel set:
 * the two strips must look identical, and two definitions of "identical" drift
 * the first time one is touched. (The `pn__` prefix is giantflow's — the styles
 * were written there first and are the shared ones now.)
 */
export function StudioNav() {
  const { myWork, toReview } = useInboxStore();

  return (
    <div className="pn__navbar">
      <nav className="pn__nav">
        <Tab to="/projects" label="Projects" end />
        <Tab to="/work" label="My work" count={myWork} />
        <Tab to="/review" label="Review" count={toReview} />
      </nav>
    </div>
  );
}

function Tab({
  to,
  label,
  count,
  end,
}: {
  to: string;
  label: string;
  count?: number;
  end?: boolean;
}) {
  return (
    <NavLink
      end={end}
      to={to}
      className={({ isActive }) => `pn__nav-tab${isActive ? " is-on" : ""}`}
    >
      {label}
      {/* Zero is not drawn, but the tab is: hiding an empty tab would rearrange
          the strip under someone the moment they are handed work. */}
      {count ? (
        <span className="pn__nav-badge is-new">{count > 99 ? "99+" : count}</span>
      ) : null}
    </NavLink>
  );
}
