/**
 * Multi-user auth plumbing (Phase 9).
 *
 * One global `window.fetch` interceptor attaches the Bearer token to every
 * same-origin `/api/*` request and, on a 401, clears the token and fires a
 * `flowboard:auth-expired` event (the app redirects to /login). This avoids
 * threading the header through ~20 fetch call sites.
 */
const TOKEN_KEY = "flowboard_token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable — token stays in-memory only via the store */
  }
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function isApiUrl(url: string): boolean {
  return url.startsWith("/api") || url.startsWith(`${window.location.origin}/api`);
}

let installed = false;

export function installAuthFetch(): void {
  if (installed) return;
  installed = true;
  const orig = window.fetch.bind(window);

  window.fetch = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = urlOf(input);
    const api = isApiUrl(url);
    const token = getToken();

    let nextInit = init;
    if (api) {
      const headers = new Headers(init.headers ?? {});
      if (token && !headers.has("Authorization")) {
        headers.set("Authorization", `Bearer ${token}`);
      }
      // Data endpoints must never be served from the browser HTTP cache — a
      // cached list is exactly why an admin edit (e.g. a member's budget) only
      // showed up after F5. Media (thumbnails/clips) load via <img>/<video>
      // element `src`, not fetch(), so this doesn't affect their caching.
      nextInit = { ...init, headers, cache: init.cache ?? "no-store" };
    }

    const res = await orig(input, nextInit);
    if (res.status === 401 && api && !url.includes("/api/account/login")) {
      setToken(null);
      window.dispatchEvent(new Event("flowboard:auth-expired"));
    }
    return res;
  };
}
