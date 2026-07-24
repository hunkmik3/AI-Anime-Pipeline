import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";

/**
 * Header notification bell. Shows one entry per release ("Update vX.Y.Z");
 * clicking an entry expands its changelog. The feed (GET
 * /api/account/notifications) is role-filtered server-side, so a non-admin
 * never sees admin-only change lines. Unread count is tracked client-side via a
 * last-seen release date in localStorage.
 */

interface Release {
  version: string;
  date: string;
  title: string;
  changes: string[];
}

const SEEN_KEY = "flowboard_notif_seen";

function readSeen(): string {
  try {
    return localStorage.getItem(SEEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function relDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  if (isNaN(d.getTime())) return iso;
  const days = Math.floor((Date.now() - d.getTime()) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function NotificationBell() {
  const [releases, setReleases] = useState<Release[]>([]);
  const [open, setOpen] = useState(false);
  const [seen, setSeen] = useState<string>(readSeen);
  const [expanded, setExpanded] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    api<{ releases: Release[] }>("/api/account/notifications")
      .then((d) => {
        if (!alive) return;
        const list = d.releases ?? [];
        setReleases(list);
        if (list.length) setExpanded(list[0].version); // newest expanded by default
      })
      .catch(() => {
        /* silent — a missing feed shouldn't break the header */
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const unread = releases.filter((r) => r.date > seen).length;

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && releases.length) {
      const newest = releases.reduce((acc, r) => (r.date > acc ? r.date : acc), "");
      setSeen(newest);
      try {
        localStorage.setItem(SEEN_KEY, newest);
      } catch {
        /* storage unavailable — badge just won't persist */
      }
    }
  }

  return (
    <div className="notif" ref={ref}>
      <button
        className={`notif__btn${unread > 0 ? " notif__btn--alert" : ""}`}
        onClick={toggle}
        aria-label={unread > 0 ? `Notifications (${unread} new)` : "Notifications"}
        title="Notifications"
      >
        <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor"
             strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
        {unread > 0 ? <span className="notif__badge">{unread > 9 ? "9+" : unread}</span> : null}
      </button>
      {open ? (
        <div className="notif__panel" role="menu" aria-label="Notifications">
          <div className="notif__head">
            <span className="notif__head-title">What's new</span>
            {unread > 0 ? <span className="notif__head-count">{unread} new</span> : null}
          </div>
          {releases.length === 0 ? (
            <div className="notif__empty">
              <span className="notif__empty-emoji" aria-hidden>🎉</span>
              You're all caught up.
            </div>
          ) : (
            <ul className="notif__list">
              {releases.map((r) => {
                const isOpen = expanded === r.version;
                const isNew = r.date > seen;
                return (
                  <li
                    key={r.version}
                    className={`notif__rel${isNew ? " is-new" : ""}${isOpen ? " is-open" : ""}`}
                  >
                    <button
                      className="notif__rel-head"
                      onClick={() => setExpanded(isOpen ? null : r.version)}
                      aria-expanded={isOpen}
                    >
                      <span className="notif__rel-main">
                        <span className="notif__rel-label">Update</span>
                        <span className="notif__rel-ver">{r.version}</span>
                        {isNew ? <span className="notif__rel-new">New</span> : null}
                      </span>
                      <span className="notif__rel-side">
                        <span className="notif__rel-date">{relDate(r.date)}</span>
                        <svg className="notif__rel-chev" viewBox="0 0 24 24" width="14" height="14"
                             fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                             strokeLinejoin="round" aria-hidden>
                          <path d="M6 9l6 6 6-6" />
                        </svg>
                      </span>
                    </button>
                    <div className="notif__rel-body" hidden={!isOpen}>
                      <ul className="notif__changes">
                        {r.changes.map((c, i) => (
                          <li key={i}>{c}</li>
                        ))}
                      </ul>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
