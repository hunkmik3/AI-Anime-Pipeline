import { useState } from "react";
import { createPortal } from "react-dom";

import { useAuthStore } from "../store/auth";
import { PasswordInput } from "./PasswordInput";

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
      setErr("New password must be at least 8 characters");
      return;
    }
    if (next !== confirm) {
      setErr("Password confirmation doesn't match");
      return;
    }
    setBusy(true);
    try {
      await changePassword(current, next);
      if (!forced) onClose?.();
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "Failed to change password");
    } finally {
      setBusy(false);
    }
  }

  // Portal to <body>: a transformed ancestor (e.g. the account menu) would make
  // position:fixed resolve against IT instead of the viewport, pinning the
  // modal into that corner instead of centering it full-screen.
  return createPortal(
    <div className="cpw-backdrop" role="dialog" aria-modal="true" aria-label="Change password">
      <form className="login-card" onSubmit={submit}>
        <h1 className="login-title">Change password</h1>
        <p className="login-sub">
          {forced
            ? "This is a temporary password set by an admin — set a new one to continue."
            : "Update your password."}
        </p>
        <label className="login-field">
          Current password
          <PasswordInput
            value={current}
            autoFocus
            autoComplete="current-password"
            onChange={setCurrent}
            required
          />
        </label>
        <label className="login-field">
          New password (≥ 8 characters)
          <PasswordInput
            value={next}
            autoComplete="new-password"
            onChange={setNext}
            required
          />
        </label>
        <label className="login-field">
          Confirm new password
          <PasswordInput
            value={confirm}
            autoComplete="new-password"
            onChange={setConfirm}
            required
          />
        </label>
        {err && <div className="login-error">{err}</div>}
        <button className="login-btn" type="submit" disabled={busy}>
          {busy ? "Saving…" : "Change password"}
        </button>
        {forced ? (
          <button
            type="button"
            className="login-btn login-btn--ghost"
            onClick={() => logout()}
            disabled={busy}
          >
            Sign out
          </button>
        ) : (
          onClose && (
            <button
              type="button"
              className="login-btn login-btn--ghost"
              onClick={onClose}
              disabled={busy}
            >
              Cancel
            </button>
          )
        )}
      </form>
    </div>,
    document.body,
  );
}
