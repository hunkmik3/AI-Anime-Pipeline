import { useState } from "react";
import { Link } from "react-router-dom";

import { useAuthStore } from "../store/auth";
import { useProjectStore } from "../store/project";
import { ChangePasswordDialog } from "./ChangePasswordDialog";
import { NotificationBell } from "./NotificationBell";

/** Top-right account widget: who's logged in, an admin link, and logout. */
export function AccountMenu() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  // Asset library is per-project — link to whichever project is currently open.
  const currentProjectId = useProjectStore((s) => s.currentProjectId);
  const [pwOpen, setPwOpen] = useState(false);
  if (!user) return null;
  const available =
    user.available_usd ??
    (typeof user.budget_usd === "number"
      ? user.budget_usd - (user.spent_usd ?? 0)
      : undefined);
  return (
    <div className="account-menu">
      <NotificationBell />
      {currentProjectId ? (
        <Link
          className="account-menu__lib"
          to={`/projects/${currentProjectId}/library`}
          title="Asset library for the current project"
        >
          <svg
            viewBox="0 0 24 24"
            width="15"
            height="15"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <circle cx="8.5" cy="8.5" r="1.5" />
            <path d="M21 15l-5-5L5 21" />
          </svg>
          Asset library
        </Link>
      ) : null}
      {typeof available === "number" ? (
        <span
          className="account-menu__budget"
          title="Budget remaining"
        >
          ${available.toFixed(2)}
        </span>
      ) : null}
      {user.role === "admin" ? (
        <Link className="account-menu__link" to="/admin">
          Admin console
        </Link>
      ) : null}
      <span className="account-menu__name" title={user.username}>
        {user.display_name || user.username}
        {user.role === "admin" ? <span className="account-menu__badge">admin</span> : null}
        {user.has_password === false ? (
          <span className="account-menu__badge" title="Signed in with Google">
            google
          </span>
        ) : null}
      </span>
      {/* Google-SSO accounts have no password — don't offer to change one. */}
      {user.has_password === false ? null : (
        <button className="account-menu__link" onClick={() => setPwOpen(true)}>
          Change password
        </button>
      )}
      <button className="account-menu__logout" onClick={() => logout()}>
        Sign out
      </button>
      {pwOpen ? <ChangePasswordDialog onClose={() => setPwOpen(false)} /> : null}
    </div>
  );
}
