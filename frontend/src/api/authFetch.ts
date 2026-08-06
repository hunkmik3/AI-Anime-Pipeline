/**
 * Multi-user auth plumbing (Phase 9).
 *
 * One global `window.fetch` interceptor attaches the Bearer token to every
 * same-origin `/api/*` request and, on a 401, clears the token and fires a
 * `flowboard:auth-expired` event (the app redirects to /login). This avoids
 * threading the header through ~20 fetch call sites.
 */
const TOKEN_KEY = "flowboard_token";
//: Same key `store/giantflowRole.ts` writes; read here rather than imported
//: because this module installs before React mounts.
const VIEW_AS_KEY = "flowboard.giantflow.viewAs";

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

/** Read straight from storage rather than importing the role store: this module
 *  is installed before React mounts, and an import cycle here would be silent. */
function readViewAs(): string | null {
  try {
    return localStorage.getItem(VIEW_AS_KEY);
  } catch {
    return null;
  }
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
        // The giantflow role preview. Sent on every /api call so the SERVER
        // answers as that role — filtered lists, real 403s — instead of the
        // browser merely hiding buttons over the admin's own data. The backend
        // can only LOWER a caller's role with it, never raise one, which is
        // what makes trusting a header here safe.
        const viewAs = readViewAs();
        if (viewAs) headers.set("X-Giantflow-View-As", viewAs);
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
