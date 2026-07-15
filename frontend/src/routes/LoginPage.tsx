import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAuthStore } from "../store/auth";
import { PasswordInput } from "../components/PasswordInput";

export function LoginPage() {
  const login = useAuthStore((s) => s.login);
  const error = useAuthStore((s) => s.error);
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [ssoError, setSsoError] = useState<string | null>(null);

  // Already signed in → bounce to the app.
  useEffect(() => {
    if (user) navigate("/projects", { replace: true });
  }, [user, navigate]);

  // Surface an SSO failure passed back in the URL fragment (#sso_error=...).
  useEffect(() => {
    const m = window.location.hash.match(/sso_error=([^&]+)/);
    if (!m) return;
    const msgs: Record<string, string> = {
      domain_not_allowed: "Email is not in an allowed organization.",
      bad_state: "Login session expired — try again.",
      exchange_failed: "Google sign-in failed — try again.",
      google_denied: "You cancelled Google sign-in.",
      account_disabled: "This account is suspended.",
    };
    setSsoError(msgs[m[1]] ?? "Google sign-in failed.");
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }, []);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      await login(username.trim(), password);
      navigate("/projects", { replace: true });
    } catch {
      /* error surfaced via the store */
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={onSubmit}>
        <img className="login-logo" src="/giantstudio-512.png" alt="Giant Studio" />
        <h1 className="login-title">Giant Studio</h1>
        <p className="login-sub">Sign in to continue</p>

        <label className="login-field">
          <span>Username</span>
          <input
            autoFocus
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={busy}
          />
        </label>
        <label className="login-field">
          <span>Password</span>
          <PasswordInput
            autoComplete="current-password"
            value={password}
            onChange={setPassword}
            disabled={busy}
          />
        </label>

        {error ? <div className="login-error">{error}</div> : null}
        {ssoError ? <div className="login-error">{ssoError}</div> : null}

        <button className="login-btn" type="submit" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <div className="login-divider"><span>or</span></div>

        {/* Full-page navigation (server-side OAuth redirect flow), not a fetch. */}
        <a className="login-btn login-btn--google" href="/api/account/sso/google/start">
          Sign in with Google
        </a>
      </form>
    </div>
  );
}
