import { useEffect, useSyncExternalStore } from "react";

import { noticeCount } from "../api/client";

/**
 * The number on the Notifications tab.
 *
 * A store rather than a hook with its own fetch: the badge is drawn by the nav
 * on every giantflow page, and the notifications page wants to reset it the
 * moment you read the feed. Two independent fetches would show two different
 * numbers on the same screen.
 *
 * Polled, not pushed. A websocket would be the right answer for a chat app; this
 * is a studio where a panel changes hands a few times an hour, and a poll costs
 * one small query. When there is a reason for realtime — someone waiting on a
 * verdict while watching the screen — this is the seam to replace.
 */
const POLL_MS = 60_000;

type Counts = { unread: number; todo: number };

let counts: Counts = { unread: 0, todo: 0 };
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let subscribers = 0;

function emit() {
  listeners.forEach((fn) => fn());
}

export async function refreshNoticeCount(): Promise<void> {
  try {
    const next = await noticeCount();
    // Same object identity means `useSyncExternalStore` re-renders nothing, so
    // only swap it when a number actually moved.
    if (next.unread !== counts.unread || next.todo !== counts.todo) {
      counts = next;
      emit();
    }
  } catch {
    // Signed out, offline, or the server restarting. A badge is not worth an
    // error state — leave the last known number and try again next tick.
  }
}

/** Called after reading the feed, so the badge clears without a round trip. */
export function clearUnread(): void {
  if (counts.unread === 0) return;
  counts = { ...counts, unread: 0 };
  emit();
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  subscribers += 1;
  if (subscribers === 1) {
    void refreshNoticeCount();
    timer = setInterval(() => void refreshNoticeCount(), POLL_MS);
  }
  return () => {
    listeners.delete(fn);
    subscribers -= 1;
    // Nobody is looking at giantflow any more — stop asking.
    if (subscribers === 0 && timer) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function snapshot(): Counts {
  return counts;
}

export function useNoticeCount(): Counts {
  const value = useSyncExternalStore(subscribe, snapshot, snapshot);
  // Switching the previewed role changes what the server counts.
  useEffect(() => {
    const onSwitch = () => void refreshNoticeCount();
    window.addEventListener("flowboard:view-as-changed", onSwitch);
    return () => window.removeEventListener("flowboard:view-as-changed", onSwitch);
  }, []);
  return value;
}
