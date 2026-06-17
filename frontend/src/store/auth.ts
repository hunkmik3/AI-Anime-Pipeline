import { create } from "zustand";

import { getToken, setToken } from "../api/authFetch";

export interface AuthUser {
  id: string;
  username: string;
  role: string;          // "admin" | "user"
  status: string;
  display_name?: string | null;
  budget_usd?: number;
  spent_usd?: number;
  available_usd?: number;
}

interface AuthState {
  user: AuthUser | null;
  ready: boolean;        // finished the boot-time token validation
  error: string | null;
  isAdmin: () => boolean;
  login(username: string, password: string): Promise<void>;
  logout(): void;
  loadMe(): Promise<void>;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  ready: false,
  error: null,
  isAdmin: () => get().user?.role === "admin",

  async login(username, password) {
    set({ error: null });
    const res = await fetch("/api/account/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const msg = res.status === 401 ? "Sai tài khoản hoặc mật khẩu" : `Đăng nhập lỗi (${res.status})`;
      set({ error: msg });
      throw new Error(msg);
    }
    const data = (await res.json()) as { token: string; user: AuthUser };
    setToken(data.token);
    set({ user: data.user, error: null, ready: true });
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
