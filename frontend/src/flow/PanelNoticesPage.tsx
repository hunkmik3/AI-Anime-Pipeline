import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  listNotices,
  markNoticesRead,
  thumbUrl,
  type Notice,
  type NoticeSummary,
} from "../api/client";
import { relativeTime } from "../components/activity/activity-meta";
import { PageHeader } from "../components/shell/PageHeader";
import { clearUnread, refreshNoticeCount } from "../store/giantflowNotices";
import { useFlowStudioStore } from "../store/flowStudio";
import { FlowViewer } from "./FlowViewer";
import { GiantflowNav } from "./GiantflowNav";

/**
 * Notifications — what you have to do, then what happened.
 *
 * Two lists, and the split is the whole point of the page.
 *
 * **To do** is derived from live state: panels sent back to you, work waiting on
 * your verdict, batches with nobody on them, deadlines about to pass. These have
 * no read state and cannot be dismissed — a job leaves the list when the work is
 * done, not when you have looked at it. Letting someone tick off "8 panels
 * waiting on your verdict" would hide the work rather than clear it.
 *
 * **What happened** is the event log in scope, newest first, with a watermark so
 * a second visit is not the same wall of text. Your own actions stay in it — a
 * history with your part cut out reads as if it never happened — but never count
 * as unread, because nothing you just did is news to you.
 *
 * Every row carries the picture it is about. The first cut did not, and a page
 * of "PANEL059 came back / PANEL060 came back / PANEL064 came back" is six
 * identical grey bands: the panel code is the one part that differs and it is
 * the part nobody can read at a glance. In a studio that adapts pictures, the
 * picture is the identifier.
 *
 * Everything on both lists is scoped the same way the rest of giantflow is: a PM
 * sees their comics, an artist sees their batches. A notification naming a panel
 * you cannot open would be a leak dressed up as a courtesy.
 */
export function PanelNoticesPage() {
  const [data, setData] = useState<NoticeSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await listNotices());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const onSwitch = () => void load();
    window.addEventListener("flowboard:view-as-changed", onSwitch);
    return () => window.removeEventListener("flowboard:view-as-changed", onSwitch);
  }, [load]);

  async function markRead() {
    setBusy(true);
    try {
      await markNoticesRead();
      clearUnread();
      await load();
      void refreshNoticeCount();
    } finally {
      setBusy(false);
    }
  }

  const todo = data?.todo ?? [];
  const feed = data?.feed ?? [];
  const seen = data?.seen_at ? new Date(data.seen_at).getTime() : 0;
  const isNew = (n: Notice) =>
    !n.mine && (!seen || (n.at ? new Date(n.at).getTime() > seen : false));
  const urgent = todo.filter((n) => TONE[n.kind] === "bad").length;

  return (
    <div className="shellpage pn__full">
      <GiantflowNav />
      <PageHeader
        title="Notifications"
        subtitle={
          data
            ? todo.length
              ? `${todo.length} thing${todo.length === 1 ? "" : "s"} need you${
                  urgent ? ` · ${urgent} blocking` : ""
                }`
              : "Nothing needs you right now."
            : undefined
        }
        actions={
          data && data.unread > 0 ? (
            <button className="btn2" disabled={busy} onClick={() => void markRead()}>
              Mark all read
            </button>
          ) : undefined
        }
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {data === null ? <p className="rfoot">Loading…</p> : null}

      {data !== null && todo.length === 0 && feed.length === 0 ? (
        <div className="inbox__empty">
          <b>All clear.</b>
          Nothing is waiting on you and nothing has changed in the last month.
        </div>
      ) : null}

      {todo.length > 0 ? (
        <section className="pn__nsec">
          <h2 className="pn__nsec-h is-todo">
            <span className="pn__nsec-label">To do</span>
            <span className="pn__nsec-n">{todo.length}</span>
            <span className="pn__nsec-rule" />
            <span className="pn__nsec-note">
              leaves this list when the work is done, not when you read it
            </span>
          </h2>
          <ul className="pn__nlist">
            {todo.map((n) => (
              <NoticeRow key={n.id} notice={n} />
            ))}
          </ul>
        </section>
      ) : null}

      {feed.length > 0 ? (
        <section className="pn__nsec">
          <h2 className="pn__nsec-h is-feed">
            <span className="pn__nsec-label">What happened</span>
            <span className="pn__nsec-n">{feed.length}</span>
            <span className="pn__nsec-rule" />
          </h2>
          {/* Grouped by day. Thirty-five rows with a relative time on each is a
              wall you have to read to navigate; a date heading lets you skip to
              the morning you were away. */}
          {groupByDay(feed).map(([day, rows]) => (
            <div key={day} className="pn__nday">
              <h3 className="pn__nday-h">{day}</h3>
              <ul className="pn__nlist">
                {rows.map((n) => (
                  <NoticeRow key={n.id} notice={n} fresh={isNew(n)} />
                ))}
              </ul>
            </div>
          ))}
        </section>
      ) : null}

      {/* Click a thumbnail to see it properly — the same viewer the review
          queue uses, look-only, because this page is for finding work. */}
      <FlowViewer viewOnly />
    </div>
  );
}

/** Newest first, split into calendar days with a human label on each. */
function groupByDay(rows: Notice[]): [string, Notice[]][] {
  const out = new Map<string, Notice[]>();
  for (const n of rows) {
    const d = n.at ? new Date(n.at) : null;
    const key = d ? dayLabel(d) : "Earlier";
    const bucket = out.get(key);
    if (bucket) bucket.push(n);
    else out.set(key, [n]);
  }
  return [...out.entries()];
}

function dayLabel(d: Date): string {
  const today = new Date();
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(today) - start(d)) / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/** Icon per kind. The word alone makes every row look the same, and this list is
 *  scanned, not read. */
const ICON: Record<string, string> = {
  sent_back: "↩",
  changes_requested: "↩",
  approved: "✓",
  submitted: "↥",
  reopened: "↺",
  to_review: "⚖",
  unassigned: "＋",
  not_started: "○",
  in_progress: "◐",
  due_soon: "⏳",
  overdue: "⏰",
};

/** Which rows are a problem, which are progress. Everything else stays neutral
 *  so the two that matter actually stand out. */
const TONE: Record<string, string> = {
  sent_back: "bad",
  changes_requested: "bad",
  overdue: "bad",
  due_soon: "warn",
  to_review: "warn",
  unassigned: "warn",
  approved: "good",
};

/** What to do about it, on the row, so the list is actionable without a detour
 *  through the panel page to find out where the button is. */
const ACTION: Record<string, string> = {
  sent_back: "Fix it",
  to_review: "Review",
  unassigned: "Assign",
  not_started: "Start",
  in_progress: "Finish",
  due_soon: "Open",
  overdue: "Open",
};

function NoticeRow({ notice: n, fresh }: { notice: Notice; fresh?: boolean }) {
  const tone = TONE[n.kind] ?? "flat";
  const select = useFlowStudioStore((s) => s.select);
  // The title arrives as a whole sentence so anything reading only `title` still
  // makes sense. Here the code is worth its own line — it is what people search
  // for and say out loud — so split it back off when it leads.
  const code = n.code && n.title.startsWith(n.code) ? n.code : null;
  const rest = code ? n.title.slice(code.length).replace(/^[\s—-]+/, "") : n.title;
  const action = ACTION[n.kind];

  return (
    <li className={`pn__nrow is-${tone}${fresh ? " is-fresh" : ""}`}>
      {n.thumb_media_id ? (
        <button
          type="button"
          className="pn__nshot"
          title="Open full size — scroll to zoom, drag to pan"
          onClick={() => select(n.thumb_media_id!)}
        >
          <img src={thumbUrl(n.thumb_media_id, 240)} alt="" loading="lazy" />
        </button>
      ) : (
        // No picture because it is not about one panel — it is a tally. The
        // number takes the slot, which is the thing being reported anyway.
        <div className={`pn__ncount is-${tone}`}>
          {n.count > 1 ? <b>{n.count}</b> : <span aria-hidden="true">{ICON[n.kind] ?? "•"}</span>}
        </div>
      )}

      <Link to={n.href ?? "#"} className="pn__nbody">
        <span className="pn__ntop">
          <span className={`pn__nkind is-${tone}`}>
            <i aria-hidden="true">{ICON[n.kind] ?? "•"}</i>
            {KIND_TEXT[n.kind] ?? n.kind.replace(/_/g, " ")}
          </span>
          {code ? <code className="pn__ncode">{code}</code> : null}
          {fresh ? <i className="pn__ndot" title="New since you last looked" /> : null}
        </span>

        <span className="pn__ntitle">{rest || n.title}</span>

        {/* The PM's reason, set as speech. It is the only part of a send-back
            that tells the artist what to actually change. */}
        {n.body ? <span className="pn__nsaid">{n.body}</span> : null}

        {n.where ? <span className="pn__nwhere">{n.where}</span> : null}
      </Link>

      <div className="pn__nside">
        {n.at ? (
          <time className="pn__nwhen" dateTime={n.at} title={new Date(n.at).toLocaleString()}>
            {relativeTime(n.at)}
          </time>
        ) : null}
        {n.actor_name ? (
          <span className="pn__nwho">{n.mine ? "you" : n.actor_name}</span>
        ) : null}
        {action && n.href ? (
          <Link to={n.href} className="pn__ngo">
            {action} →
          </Link>
        ) : null}
      </div>
    </li>
  );
}

/** A short label for the kind, so the row says what sort of thing it is before
 *  you read the sentence. */
const KIND_TEXT: Record<string, string> = {
  sent_back: "sent back",
  changes_requested: "sent back",
  approved: "approved",
  submitted: "submitted",
  reopened: "reopened",
  to_review: "to review",
  unassigned: "unassigned",
  not_started: "not started",
  in_progress: "in progress",
  due_soon: "due soon",
  overdue: "overdue",
};
