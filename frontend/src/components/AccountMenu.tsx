import { useState } from "react";
import { Link } from "react-router-dom";

import { useAuthStore } from "../store/auth";
import { ChangePasswordDialog } from "./ChangePasswordDialog";

/** Top-right account widget: who's logged in, an admin link, and logout. */
export function AccountMenu() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const [pwOpen, setPwOpen] = useState(false);
  if (!user) return null;
  const available =
    user.available_usd ??
    (typeof user.budget_usd === "number"
      ? user.budget_usd - (user.spent_usd ?? 0)
      : undefined);
  return (
    <div className="account-menu">
      {typeof available === "number" ? (
        <span
          className="account-menu__budget"
          title="Ngân sách còn lại"
        >
          ${available.toFixed(2)}
        </span>
      ) : null}
      {user.role === "admin" ? (
        <Link className="account-menu__link" to="/admin">
          Quản lý tài khoản
        </Link>
      ) : null}
      <span className="account-menu__name" title={user.username}>
        {user.display_name || user.username}
        {user.role === "admin" ? <span className="account-menu__badge">admin</span> : null}
        {user.has_password === false ? (
          <span className="account-menu__badge" title="Đăng nhập bằng Google">
            google
          </span>
        ) : null}
      </span>
      {/* Google-SSO accounts have no password — don't offer to change one. */}
      {user.has_password === false ? null : (
        <button className="account-menu__link" onClick={() => setPwOpen(true)}>
          Đổi mật khẩu
        </button>
      )}
      <button className="account-menu__logout" onClick={() => logout()}>
        Đăng xuất
      </button>
      {pwOpen ? <ChangePasswordDialog onClose={() => setPwOpen(false)} /> : null}
    </div>
  );
}
