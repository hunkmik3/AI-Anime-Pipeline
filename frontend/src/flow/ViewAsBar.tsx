import {
  GIANTFLOW_ROLES,
  setViewAs,
  useGiantflowRole,
  type GiantflowRole,
} from "../store/giantflowRole";

/**
 * Switch which role's view is drawn.
 *
 * Offered to admins only. It is a **preview**: the server still authorises the
 * real signed-in user, so this cannot grant anything — it exists to check what a
 * PM, artist or viewer actually sees without keeping four accounts logged in.
 * The banner says so, because a control that looks like it changes permissions
 * and doesn't is worse than no control.
 */
export function ViewAsBar() {
  const { realRole, viewAs, role } = useGiantflowRole();
  if (realRole !== "admin") return null;

  return (
    <div className={`pn__viewas${viewAs ? " is-on" : ""}`}>
      <span className="pn__viewas-label">View as</span>
      <div className="seg">
        {GIANTFLOW_ROLES.map((r) => (
          <button
            key={r.id}
            className={`seg__btn${role === r.id ? " is-on" : ""}`}
            onClick={() => setViewAs(r.id === realRole ? null : (r.id as GiantflowRole))}
          >
            {r.label}
          </button>
        ))}
      </div>
      {viewAs ? (
        <>
          <span className="pn__viewas-note">
            Preview only — the server still sees you as {realRole}.
          </span>
          <button className="btn2" onClick={() => setViewAs(null)}>
            Exit
          </button>
        </>
      ) : null}
    </div>
  );
}
