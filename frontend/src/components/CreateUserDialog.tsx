import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

export interface NewUser {
  username: string;
  password: string;
  email?: string;
  role: string;
}

/** "Add member" modal — replaces the bare inline create form. */
export function CreateUserDialog({
  busy,
  onSubmit,
  onClose,
}: {
  busy?: boolean;
  onSubmit: (u: NewUser) => void;
  onClose: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("user");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!username.trim()) {
      setErr("Enter a username");
      return;
    }
    if (password.length < 8) {
      setErr("Temporary password must be at least 8 characters");
      return;
    }
    onSubmit({
      username: username.trim(),
      password,
      email: email.trim() || undefined,
      role,
    });
  }

  return createPortal(
    <div
      className="cpw-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Add member"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <form className="modal-card" onSubmit={submit}>
        <h2 className="modal-card__title">Add member</h2>
        <p className="modal-card__msg">
          They'll be asked to change this temporary password on their first login.
        </p>
        <label className="login-field">
          <span>Username</span>
          <input value={username} autoFocus onChange={(e) => setUsername(e.target.value)} />
        </label>
        <label className="login-field">
          <span>Temporary password (≥ 8 characters)</span>
          <input type="text" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        <label className="login-field">
          <span>Email (optional)</span>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label className="login-field">
          <span>Role</span>
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
        </label>
        {err ? <div className="login-error">{err}</div> : null}
        <div className="modal-card__actions">
          <button type="button" className="modal-btn modal-btn--ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="modal-btn" disabled={busy}>
            {busy ? "Creating…" : "Create account"}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  );
}
