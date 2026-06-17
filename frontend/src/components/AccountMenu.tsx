import { Link } from "react-router-dom";

import { useAuthStore } from "../store/auth";

/** Top-right account widget: who's logged in, an admin link, and logout. */
export function AccountMenu() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  if (!user) return null;
  return (
    <div className="account-menu">
      {user.role === "admin" ? (
        <Link className="account-menu__link" to="/admin">
          Quản lý tài khoản
        </Link>
      ) : null}
      <span className="account-menu__name" title={user.username}>
        {user.display_name || user.username}
        {user.role === "admin" ? <span className="account-menu__badge">admin</span> : null}
      </span>
      <button className="account-menu__logout" onClick={() => logout()}>
        Đăng xuất
      </button>
    </div>
  );
}
