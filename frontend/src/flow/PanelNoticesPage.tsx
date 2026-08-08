import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  listNotices,
  markNoticesRead,
  type Notice,
  type NoticeSummary,
} from "../api/client";
import { relativeTime } from "../components/activity/activity-meta";
import { PageHeader } from "../components/shell/PageHeader";
import { clearUnread, refreshNoticeCount } from "../store/giantflowNotices";
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

  return (
    <div className="shellpage pn__full">
      <GiantflowNav />
      <PageHeader
        title="Notifications"
        subtitle={
          data
            ? todo.length
              ? `${todo.length} thing${todo.length === 1 ? "" : "s"} need you`
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
            To do <span>{todo.length}</span>
          </h2>
          <p className="pn__nsec-hint">
            Derived from the work itself — each line leaves this list when the
            work is done, not when you read it.
          </p>
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
            What happened <span>{feed.length}</span>
          </h2>
          <ul className="pn__nlist">
            {feed.map((n) => (
              <NoticeRow key={n.id} notice={n} fresh={isNew(n)} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

/** Icon and colour per kind. The word alone makes every row look the same, and
 *  this list is scanned, not read. */
const ICON: Record<string, string> = {
  sent_back: "↩",
  changes_requested: "↩",
  approved: "✓",
  submitted: "↥",
  reopened: "↺",
  to_review: "⚖",
  unassigned: "👤",
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

function NoticeRow({ notice: n, fresh }: { notice: Notice; fresh?: boolean }) {
  const tone = TONE[n.kind] ?? "flat";
  const inner = (
    <>
      <span className={`pn__nicon is-${tone}`} aria-hidden="true">
        {ICON[n.kind] ?? "•"}
      </span>
      <span className="pn__nbody">
        <span className="pn__ntitle">
          {n.title}
          {fresh ? <i className="pn__ndot" title="New since you last looked" /> : null}
        </span>
        {n.body ? <span className="pn__ntext">{n.body}</span> : null}
        <span className="pn__nmeta">
          {n.actor_name ? <b>{n.mine ? "you" : n.actor_name}</b> : null}
          {n.where ? <span>{n.where}</span> : null}
          {n.at ? (
            <time dateTime={n.at} title={new Date(n.at).toLocaleString()}>
              {relativeTime(n.at)}
            </time>
          ) : null}
        </span>
      </span>
    </>
  );

  return (
    <li className={`pn__nrow is-${tone}${fresh ? " is-fresh" : ""}`}>
      {n.href ? (
        <Link to={n.href} className="pn__nlink">
          {inner}
        </Link>
      ) : (
        <div className="pn__nlink">{inner}</div>
      )}
    </li>
  );
}
