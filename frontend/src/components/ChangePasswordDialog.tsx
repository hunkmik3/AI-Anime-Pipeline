import { useState } from "react";

import { useAuthStore } from "../store/auth";

/**
 * Self-service password change. Two modes:
 *  - forced: shown by RequireAuth when the account has must_change_password
 *    (admin-provisioned temp password) — no cancel, blocks the app until done.
 *  - normal: opened from the account menu — dismissable.
 * On success the backend revokes other sessions and returns a fresh token
 * (handled in the store), and the must_change_password flag clears so the
 * forced gate lifts automatically.
 */
export function ChangePasswordDialog({
  forced = false,
  onClose,
}: {
  forced?: boolean;
  onClose?: () => void;
}) {
  const changePassword = useAuthStore((s) => s.changePassword);
  const logout = useAuthStore((s) => s.logout);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (next.length < 8) {
      setErr("Mật khẩu mới tối thiểu 8 ký tự");
      return;
    }
    if (next !== confirm) {
      setErr("Xác nhận mật khẩu không khớp");
      return;
    }
    setBusy(true);
    try {
      await changePassword(current, next);
      if (!forced) onClose?.();
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "Đổi mật khẩu lỗi");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="cpw-backdrop" role="dialog" aria-modal="true" aria-label="Đổi mật khẩu">
      <form className="login-card" onSubmit={submit}>
        <h1 className="login-title">Đổi mật khẩu</h1>
        <p className="login-sub">
          {forced
            ? "Đây là mật khẩu tạm do admin cấp — hãy đặt mật khẩu mới để tiếp tục."
            : "Cập nhật mật khẩu của bạn."}
        </p>
        <label className="login-field">
          Mật khẩu hiện tại
          <input
            type="password"
            value={current}
            autoFocus
            onChange={(e) => setCurrent(e.target.value)}
            required
          />
        </label>
        <label className="login-field">
          Mật khẩu mới (≥ 8 ký tự)
          <input
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            required
          />
        </label>
        <label className="login-field">
          Xác nhận mật khẩu mới
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
          />
        </label>
        {err && <div className="login-error">{err}</div>}
        <button className="login-btn" type="submit" disabled={busy}>
          {busy ? "Đang lưu…" : "Đổi mật khẩu"}
        </button>
        {forced ? (
          <button
            type="button"
            className="login-btn login-btn--ghost"
            onClick={() => logout()}
            disabled={busy}
          >
            Đăng xuất
          </button>
        ) : (
          onClose && (
            <button
              type="button"
              className="login-btn login-btn--ghost"
              onClick={onClose}
              disabled={busy}
            >
              Huỷ
            </button>
          )
        )}
      </form>
    </div>
  );
}
