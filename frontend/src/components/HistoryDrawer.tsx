import { useEffect, useState } from "react";

import {
  getObjectHistory,
  type HistoryEntryDTO,
  type HistoryObjectType,
} from "../api/client";

/**
 * The change trail for one object.
 *
 * This is the readable half of leaving Google Sheets behind. Recording who
 * reassigned an episode or raised a budget only helps if someone can look it up
 * next to the thing itself — otherwise the record exists but the question
 * ("who changed this, and when?") still goes unanswered in Discord.
 *
 */

function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function HistoryDrawer({
  objectType,
  objectId,
  label,
  onClose,
}: {
  objectType: HistoryObjectType;
  objectId: string;
  label: string;
  onClose: () => void;
}) {
  const [entries, setEntries] = useState<HistoryEntryDTO[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const out = await getObjectHistory(objectType, objectId);
        if (alive) setEntries(out.entries);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [objectType, objectId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="drawer"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="drawer__panel" role="dialog" aria-label={`History — ${label}`}>
        <div className="drawer__head">
          <div>
            <h3 className="drawer__title">History — {label}</h3>
            <p className="drawer__sub">
              Every recorded change, newest first. Assignments, budgets, renames and
              deletions all land here.
            </p>
          </div>
          <button className="drawer__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <div className="drawer__body">
          {error ? <p className="rfoot">Couldn’t load the history: {error}</p> : null}
          {!error && entries === null ? <p className="rfoot">Loading…</p> : null}
          {entries !== null && entries.length === 0 ? (
            <p className="rfoot">
              Nothing recorded yet — changes made from here on will show up.
            </p>
          ) : null}

          {/* Same markup as the episode and series history tabs, so one trail is
              one trail wherever it is read. */}
          <ol className="ep__hist">
            {(entries ?? []).map((e) => (
              <li key={e.id}>
                <span className="ep__hist-when">{when(e.created_at)}</span>
                <span className="ep__hist-action">{e.action.replace(/[._]/g, " ")}</span>
                <span className="ep__hist-who">{e.actor ?? "system"}</span>
                {e.detail ? <span className="ep__hist-detail">{e.detail}</span> : null}
              </li>
            ))}
          </ol>
        </div>
      </div>
    </div>
  );
}
