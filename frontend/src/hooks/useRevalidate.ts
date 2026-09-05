import { useEffect, useRef } from "react";

// Opening a native file-open dialog blurs the window; closing it re-focuses —
// and, on some platforms, also fires `visibilitychange`. That burst would
// otherwise trigger a spurious revalidate that reloads the whole view
// mid-upload — the screen flashes, the node remounts, and the "uploading…"
// state (plus the freshly-picked file) is lost before the upload persists.
// A file-input activation is the one focus/visibility change we must NOT treat
// as "user came back to the tab". We arm a guard on the input click; the first
// revalidate event after (focus OR visibility) disarms it AND opens a short
// cooldown, so the whole close-burst is swallowed while a genuine tab return
// later still refreshes. Registered once at module load; a hidden input opened
// via ref.click() still dispatches a bubbling click a capture listener sees.
let _fileDialogArmed = false;
let _revalidateCooldownUntil = 0;
if (typeof document !== "undefined") {
  document.addEventListener(
    "click",
    (e) => {
      const el = e.target;
      if (el instanceof HTMLInputElement && el.type === "file") {
        _fileDialogArmed = true;
      }
    },
    true,
  );
}

/** True when the current focus/visibility event is the file-dialog close burst
 *  (or its brief aftermath) and should NOT revalidate. */
function _suppressedByFileDialog(): boolean {
  const now = Date.now();
  if (_fileDialogArmed) {
    _fileDialogArmed = false;
    _revalidateCooldownUntil = now + 1500;
    return true;
  }
  return now < _revalidateCooldownUntil;
}

/**
 * Keep a view live without a manual F5.
 *
 * Re-runs `fn` when the user comes back to the tab/window (`focus` +
 * `visibilitychange`) and, optionally, on a light interval WHILE the tab is
 * visible (a hidden tab shouldn't poll). Lifted verbatim from the pattern that
 * was hand-copied into AdminPage / useActivityFeed / the provider badges, so
 * every screen refreshes the same way from one place.
 *
 * `fn` is kept in a ref, so callers can pass an inline closure without
 * re-subscribing the listeners on every render.
 *
 *   useRevalidate(() => void load(), { intervalMs: 20000 });
 */
export function useRevalidate(
  fn: () => void | Promise<void>,
  opts: { intervalMs?: number; enabled?: boolean } = {},
): void {
  const { intervalMs = 0, enabled = true } = opts;
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return;
    const tick = () => {
      void fnRef.current();
    };
    // Both handlers defer to the file-dialog guard, so picking a file no longer
    // reloads the view and eats the upload. A genuine tab/app return (outside
    // the guard window) still revalidates.
    const onFocus = () => {
      if (_suppressedByFileDialog()) return;
      tick();
    };
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (_suppressedByFileDialog()) return;
      tick();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    let id: number | undefined;
    if (intervalMs > 0) {
      id = window.setInterval(() => {
        // Don't burn requests on a backgrounded tab; focus will catch it up.
        if (document.visibilityState === "visible") tick();
      }, intervalMs);
    }
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
      if (id !== undefined) window.clearInterval(id);
    };
  }, [enabled, intervalMs]);
}
