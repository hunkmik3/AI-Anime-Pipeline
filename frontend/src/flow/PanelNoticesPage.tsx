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
  const [status, setStatus] = useState<string>("all");
  //: Chronological, or gathered by status. Two different questions — "what
  //: happened while I was away" reads forwards in time; "how much is sitting in
  //: send-back" needs the same statuses next to each other, and no amount of
  //: scrolling a date-ordered list answers it.
  const [order, setOrder] = useState<"day" | "status">("day");

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

  const allTodo = data?.todo ?? [];
  const allFeed = data?.feed ?? [];
  const seen = data?.seen_at ? new Date(data.seen_at).getTime() : 0;
  const isNew = (n: Notice) =>
    !n.mine && (!seen || (n.at ? new Date(n.at).getTime() > seen : false));

  // Which statuses are actually present, so the strip never offers a filter
  // that leads to an empty page.
  const present = statusesIn([...allTodo, ...allFeed]);
  const keep = (n: Notice) => status === "all" || STATUS_OF[n.kind] === status;
  const todo = allTodo.filter(keep);
  const feed = allFeed.filter(keep);
  const urgent = allTodo.filter((n) => TONE[n.kind] === "bad").length;

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

      {present.length > 1 ? (
        <div className="pn__filters">
          <div className="seg">
            <button
              className={`seg__btn${status === "all" ? " is-on" : ""}`}
              onClick={() => setStatus("all")}
            >
              All {allTodo.length + allFeed.length}
            </button>
            {present.map(([key, count]) => (
              <button
                key={key}
                className={`seg__btn${status === key ? " is-on" : ""}`}
                onClick={() => setStatus(key)}
              >
                <i className={`pn__nseg-dot is-${STATUS_TONE[key]}`} aria-hidden="true" />
                {STATUS_LABEL[key]} {count}
              </button>
            ))}
          </div>
          <div className="seg">
            <button
              className={`seg__btn${order === "day" ? " is-on" : ""}`}
              title="History reads forwards in time"
              onClick={() => setOrder("day")}
            >
              By day
            </button>
            <button
              className={`seg__btn${order === "status" ? " is-on" : ""}`}
              title="Gather the same statuses together"
              onClick={() => setOrder("status")}
            >
              By status
            </button>
          </div>
        </div>
      ) : null}

      {data !== null && allTodo.length === 0 && allFeed.length === 0 ? (
        <div className="inbox__empty">
          <b>All clear.</b>
          Nothing is waiting on you and nothing has changed in the last month.
        </div>
      ) : null}

      {data !== null && status !== "all" && todo.length === 0 && feed.length === 0 ? (
        <div className="inbox__empty">
          <b>Nothing under {STATUS_LABEL[status] ?? status}.</b>
          <button className="btn2" onClick={() => setStatus("all")}>
            Show everything
          </button>
        </div>
      ) : null}

      {todo.length > 0 ? (
        <section className="pn__nsec">
          <h2 className="pn__nsec-h is-todo">
            <span className="pn__nsec-label">To do</span>
            {/* While a filter is on, the section count and the count in the page
                header describe different sets. Say so rather than leave two
                numbers disagreeing. */}
            <span className="pn__nsec-n">
              {status === "all" ? todo.length : `${todo.length} of ${allTodo.length}`}
            </span>
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
            <span className="pn__nsec-n">
              {status === "all" ? feed.length : `${feed.length} of ${allFeed.length}`}
            </span>
            <span className="pn__nsec-rule" />
          </h2>
          {/* Grouped by day by default: thirty-five rows with a relative time on
              each is a wall you have to read to navigate, and a date heading lets
              you skip to the morning you were away. Switched to status, the same
              rows gather under their status instead — which is the only way to
              see how much is sitting in one state without counting. */}
          {(order === "day" ? groupByDay(feed) : groupByStatus(feed)).map(([label, rows]) => (
            <div key={label} className="pn__nday">
              <h3 className="pn__nday-h">
                {label}
                {order === "status" ? <span>{rows.length}</span> : null}
              </h3>
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

/**
 * Statuses, in the order a working day cares about them.
 *
 * Coarser than `kind` on purpose. `sent_back` on a to-do and
 * `changes_requested` in the feed are the same fact seen from the two ends of
 * the handover, and a filter that separated them would make you click twice to
 * see one thing. The map is what collapses them.
 */
const STATUS_ORDER = [
  "sent_back",
  "waiting",
  "deadline",
  "todo",
  "approved",
  "reopened",
] as const;

const STATUS_OF: Record<string, string> = {
  sent_back: "sent_back",
  changes_requested: "sent_back",
  to_review: "waiting",
  submitted: "waiting",
  overdue: "deadline",
  due_soon: "deadline",
  not_started: "todo",
  in_progress: "todo",
  unassigned: "todo",
  approved: "approved",
  reopened: "reopened",
};

const STATUS_LABEL: Record<string, string> = {
  sent_back: "Sent back",
  waiting: "Waiting",
  deadline: "Deadlines",
  todo: "Not started",
  approved: "Approved",
  reopened: "Reopened",
};

const STATUS_TONE: Record<string, string> = {
  sent_back: "bad",
  waiting: "warn",
  deadline: "bad",
  todo: "flat",
  approved: "good",
  reopened: "flat",
};

/** Which statuses these rows actually contain, with counts, in working order. */
function statusesIn(rows: Notice[]): [string, number][] {
  const seen = new Map<string, number>();
  for (const n of rows) {
    const s = STATUS_OF[n.kind];
    if (s) seen.set(s, (seen.get(s) ?? 0) + 1);
  }
  return STATUS_ORDER.filter((s) => seen.has(s)).map((s) => [s, seen.get(s)!]);
}

/** The same rows gathered under their status, still newest first inside each. */
function groupByStatus(rows: Notice[]): [string, Notice[]][] {
  const out = new Map<string, Notice[]>();
  for (const n of rows) {
    const key = STATUS_OF[n.kind] ?? "other";
    const bucket = out.get(key);
    if (bucket) bucket.push(n);
    else out.set(key, [n]);
  }
  return [...STATUS_ORDER, "other"]
    .filter((s) => out.has(s))
    .map((s) => [STATUS_LABEL[s] ?? "Other", out.get(s)!]);
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
