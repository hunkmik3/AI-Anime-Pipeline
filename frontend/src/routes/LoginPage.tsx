import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAuthStore } from "../store/auth";
import { PasswordInput } from "../components/PasswordInput";

type Mode = "signin" | "signup";

export function LoginPage() {
  const login = useAuthStore((s) => s.login);
  const error = useAuthStore((s) => s.error);
  const clearError = useAuthStore((s) => s.clearError);
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const [mode, setMode] = useState<Mode>("signin");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [ssoError, setSsoError] = useState<string | null>(null);

  // Sign-up form
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [note, setNote] = useState("");
  const [signupErr, setSignupErr] = useState<string | null>(null);
  const [signupDone, setSignupDone] = useState(false);

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

  function switchMode(next: Mode) {
    setMode(next);
    setSsoError(null);
    setSignupErr(null);
    clearError?.();
  }

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

  async function onSignup(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setSignupErr(null);
    try {
      const res = await fetch("/api/account/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email.trim(),
          display_name: fullName.trim() || null,
          note: note.trim() || null,
        }),
      });
      if (!res.ok) {
        let detail = `Request failed (${res.status})`;
        try {
          detail = (await res.json()).detail ?? detail;
        } catch {
          /* keep status */
        }
        throw new Error(String(detail));
      }
      setSignupDone(true);
    } catch (err) {
      setSignupErr(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="login-card">
        <img className="login-logo" src="/giantstudio-512.png" alt="Giant Studio" />
        <h1 className="login-title">Giant Studio</h1>

        {signupDone ? (
          // Terminal state: the request is queued, there's nothing more to do here.
          <>
            <p className="login-sub">Request sent</p>
            <div className="login-ok">
              ✓ Thanks! An admin will review your request. If it's approved you'll
              get an email with your username and a temporary password.
            </div>
            <button
              className="login-btn login-btn--ghost"
              onClick={() => {
                setSignupDone(false);
                switchMode("signin");
              }}
            >
              Back to sign in
            </button>
          </>
        ) : (
          <>
            <div className="login-tabs" role="tablist">
              <button
                role="tab"
                aria-selected={mode === "signin"}
                className={`login-tab${mode === "signin" ? " is-active" : ""}`}
                onClick={() => switchMode("signin")}
              >
                Sign in
              </button>
              <button
                role="tab"
                aria-selected={mode === "signup"}
                className={`login-tab${mode === "signup" ? " is-active" : ""}`}
                onClick={() => switchMode("signup")}
              >
                Sign up
              </button>
            </div>

            {mode === "signin" ? (
              <form className="login-form" onSubmit={onSubmit}>
                <label className="login-field">
                  {/* Accounts created via sign-up use the email as the username;
                      admin-provisioned ones may use a plain name. Both land here. */}
                  <span>Username or email</span>
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

                <button
                  className="login-btn"
                  type="submit"
                  disabled={busy || !username || !password}
                >
                  {busy ? "Signing in…" : "Sign in"}
                </button>

                <div className="login-divider"><span>or</span></div>

                {/* Full-page navigation (server-side OAuth redirect flow), not a fetch. */}
                <a className="login-btn login-btn--google" href="/api/account/sso/google/start">
                  Sign in with Google
                </a>
              </form>
            ) : (
              <form className="login-form" onSubmit={onSignup}>
                <p className="login-sub login-sub--tight">
                  Request an account. An admin reviews it, and you'll get an email
                  with your login if it's approved.
                </p>

                <label className="login-field">
                  <span>Email</span>
                  <input
                    autoFocus
                    type="email"
                    autoComplete="email"
                    placeholder="you@studio.com"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    disabled={busy}
                  />
                </label>
                <label className="login-field">
                  <span>Full name</span>
                  <input
                    autoComplete="name"
                    placeholder="Your name"
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    disabled={busy}
                  />
                </label>
                <label className="login-field">
                  <span>Why do you need access? (optional)</span>
                  <textarea
                    className="login-textarea"
                    rows={3}
                    maxLength={500}
                    placeholder="Team, role, what you'll work on…"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    disabled={busy}
                  />
                </label>

                {signupErr ? <div className="login-error">{signupErr}</div> : null}

                <button className="login-btn" type="submit" disabled={busy || !email}>
                  {busy ? "Sending…" : "Request account"}
                </button>
              </form>
            )}
          </>
        )}
      </div>
    </div>
  );
}
