import { create } from "zustand";

import { getToken, setToken } from "../api/authFetch";

export interface AuthUser {
  id: string;
  username: string;
  role: string;          // "admin" | "user"
  status: string;
  display_name?: string | null;
  email?: string | null;
  must_change_password?: boolean;
  has_password?: boolean;   // false = Google-SSO account (no password to change)
  budget_usd?: number;
  spent_usd?: number;
  available_usd?: number;
  /** Which of the two products this account belongs to.
   *
   *  They are separate places of work, not two views of one: a panel artist has
   *  no business in the production tree and a video editor has none in somebody's
   *  comic. The server enforces it per request; this is what stops the header
   *  offering a door that leads to a 404.
   *
   *  `flow_manual` is the free-form image workspace at /giantflow/studio — what
   *  Giantflow was before panel production was built on it. It holds no comic, so
   *  anyone signed in may use it. */
  products?: { studio: boolean; flow: boolean; flow_manual: boolean };
}

interface AuthState {
  user: AuthUser | null;
  ready: boolean;        // finished the boot-time token validation
  error: string | null;
  isAdmin: () => boolean;
  clearError(): void;
  login(username: string, password: string): Promise<void>;
  changePassword(currentPassword: string, newPassword: string): Promise<void>;
  logout(): void;
  loadMe(): Promise<void>;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  ready: false,
  error: null,
  isAdmin: () => get().user?.role === "admin",
  clearError: () => set({ error: null }),

  async login(username, password) {
    set({ error: null });
    const res = await fetch("/api/account/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const msg = res.status === 401 ? "Wrong username or password" : `Login failed ()`;
      set({ error: msg });
      throw new Error(msg);
    }
    const data = (await res.json()) as { token: string; user: AuthUser };
    setToken(data.token);
    set({ user: data.user, error: null, ready: true });
  },

  // Self-service password change. On success the backend revokes other
  // sessions and returns a FRESH token for this one, which we store.
  async changePassword(currentPassword, newPassword) {
    const res = await fetch("/api/account/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
    if (!res.ok) {
      let msg = `Failed to change password ()`;
      try {
        const j = await res.json();
        if (j?.detail) msg = String(j.detail);
      } catch {
        /* ignore */
      }
      throw new Error(msg);
    }
    const data = (await res.json()) as { token: string; user: AuthUser };
    setToken(data.token);
    set({ user: data.user, error: null });
  },

  logout() {
    setToken(null);
    set({ user: null, error: null });
  },

  // Validate a persisted token on boot. The fetch interceptor adds the Bearer
  // header; a 401 clears it. Sets `ready` once resolved either way.
  async loadMe() {
    if (!getToken()) {
      set({ ready: true, user: null });
      return;
    }
    try {
      const res = await fetch("/api/account/me");
      if (res.ok) {
        set({ user: (await res.json()) as AuthUser, ready: true });
      } else {
        setToken(null);
        set({ user: null, ready: true });
      }
    } catch {
      set({ ready: true }); // network hiccup — keep token, retry on next nav
    }
  },
}));

// A 401 anywhere fires this — drop the user so the route guard sends to /login.
if (typeof window !== "undefined") {
  window.addEventListener("flowboard:auth-expired", () => {
    useAuthStore.setState({ user: null });
  });
}
