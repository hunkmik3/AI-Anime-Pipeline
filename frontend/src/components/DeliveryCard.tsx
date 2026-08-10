import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { DeliverableSeriesDTO, SubmissionDTO } from "../api/client";

/**
 * One SERIES' delivery, as it appears in Work and Review.
 *
 * Shared by both because the two pages describe the same thing from opposite ends,
 * and when each had its own markup the same row showed different facts on each
 * screen.
 *
 * The row was an episode until the deliverable moved up a tier: a twelve-episode
 * series meant twelve identical cards, each repeating the same series header and
 * each offering to hand in a twelfth of one job.
 *
 * Leads with **where** before **what**, then the four facts a reviewer or an
 * assignee actually needs — who has it, who handed it in, and when each side last
 * acted — because "waiting on review" means something different after one day than
 * after two weeks.
 */

export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** "3 days ago" — the part that tells you whether something is stuck. */
function ago(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  const months = Math.round(days / 30);
  return months === 1 ? "a month ago" : `${months} months ago`;
}

/** How long the reviewer took — or, if nobody has answered, how long it has sat. */
function span(from: string | null | undefined, to: string | null | undefined): string {
  if (!from) return "";
  const a = new Date(from).getTime();
  const b = to ? new Date(to).getTime() : Date.now();
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return "";
  const mins = Math.round((b - a) / 60_000);
  if (mins < 1) return "under a minute";
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"}`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"}`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"}`;
}

function Leg({
  kind,
  label,
  who,
  when,
  meta,
  note,
  noteTone,
}: {
  kind: string;
  label: string;
  who: string;
  when?: string | null;
  meta?: string;
  note?: string | null;
  noteTone?: "back" | "ok";
}) {
  return (
    <div className="dlv__leg">
      <span className={`dlv__legDot dlv__legDot--${kind}`} />
      <span className={`dlv__legLabel dlv__legLabel--${kind}`}>{label}</span>
      <span className="dlv__legWho">{who}</span>
      <span className="dlv__legWhen">
        {when ? fmtWhen(when) : <em>—</em>}
        {when ? <em> · {ago(when)}</em> : null}
        {meta ? <em className="dlv__legMeta">{meta}</em> : null}
      </span>
      {note ? (
        <p className={`dlv__said${noteTone ? ` dlv__said--${noteTone}` : ""}`}>
          {note}
        </p>
      ) : null}
    </div>
  );
}

/**
 * Every attempt at this series, newest first.
 *
 * The card used to show only the current attempt, so "sent back" arrived with no
 * memory: an artist could not see that the same note had been given twice, and a
 * reviewer deciding on v3 could not see what they had asked for in v1. Both sides
 * were then reconstructing the thread from Discord, which is the habit this app
 * exists to end.
 *
 * Each attempt is TWO events — handed in, then answered — because the gap between
 * them is the thing people are actually looking for. A v2 that sat for nine days
 * before anyone watched it is a different story from one refused in an hour, and
 * only the two timestamps side by side tell you which.
 */
function Attempts({ rows }: { rows: SubmissionDTO[] }) {
  return (
    <ol className="dlv__hist">
      {rows.map((s) => {
        const answered = s.status === "approved" || s.status === "rejected";
        const took = span(s.submitted_at, s.reviewed_at);
        return (
          <li key={s.id} className={`dlv__att is-${s.status}`}>
            <div className="dlv__attHead">
              <b className="dlv__ver">v{s.version}</b>
              <span className={`dlv__verdict dlv__verdict--${s.status}`}>
                {s.status === "approved"
                  ? "Approved"
                  : s.status === "rejected"
                    ? "Sent back"
                    : "Waiting on review"}
              </span>
              {s.drive_url ? (
                <a
                  className="dlv__file"
                  href={s.drive_url}
                  target="_blank"
                  rel="noreferrer"
                  title="Open the cut that was handed in for this attempt"
                >
                  the cut ↗
                </a>
              ) : null}
            </div>

            <Leg
              kind="in"
              label="Handed in"
              who={s.submitted_by_name ?? "—"}
              when={s.submitted_at}
              note={s.note}
            />

            <Leg
              kind={answered ? s.status : "open"}
              label={
                s.status === "approved"
                  ? "Approved"
                  : s.status === "rejected"
                    ? "Sent back"
                    : "Waiting"
              }
              who={
                answered
                  ? (s.reviewed_by_name ?? "—")
                  : (s.approver_name ?? "a reviewer")
              }
              when={answered ? s.reviewed_at : null}
              /* The turnaround is the whole reason both timestamps are here: a cut
                 refused in an hour and one that sat nine days are different
                 problems, and neither date alone says which. While nothing has
                 been answered the same number is how long it has been waiting. */
              meta={
                answered
                  ? took
                    ? ` · took ${took}`
                    : undefined
                  : `waiting ${span(s.submitted_at, null)}`
              }
              note={s.review_note}
              noteTone={s.status === "rejected" ? "back" : "ok"}
            />
          </li>
        );
      })}
    </ol>
  );
}

/**
 * Always open. It was behind a fold, which put the one thing somebody opens this
 * card for — what was said last time — one click further away than the status
 * chip they already knew.
 */
function Thread({ rows }: { rows: SubmissionDTO[] }) {
  return (
    <div className="dlv__thread">
      <div className="dlv__threadHead">
        History · {rows.length} {rows.length === 1 ? "attempt" : "attempts"}
      </div>
      <Attempts rows={rows} />
    </div>
  );
}

function Fact({
  label,
  value,
  sub,
  numeric,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  numeric?: boolean;
}) {
  const empty = value === null || value === undefined || value === "";
  return (
    <div className="inbox__fact">
      <div className="inbox__factLabel">{label}</div>
      <div
        className={`inbox__factValue${numeric ? " inbox__factValue--num" : ""}${
          empty ? " inbox__factValue--none" : ""
        }`}
      >
        {empty ? "—" : value}
      </div>
      {sub ? <div className="inbox__factSub">{sub}</div> : null}
    </div>
  );
}

export function DeliveryCard({
  series: sr,
  submission: s,
  history = [],
  tone,
  status,
  statusTone,
  actions,
  children,
}: {
  series: DeliverableSeriesDTO | null;
  submission: SubmissionDTO | null;
  /** Every attempt, newest first — the thread under the facts. */
  history?: SubmissionDTO[];
  tone: "todo" | "waiting" | "done";
  status: string;
  statusTone: "muted" | "warn" | "good" | "bad" | "info";
  /** The button(s) this inbox exists for. */
  actions?: ReactNode;
  /** The expanded panel: the submit form, or the player and verdict. */
  children?: ReactNode;
}) {
  return (
    <li className={`inbox__item inbox__item--${tone}`}>
      {/* Where it sits, before what it is. */}
      <div className="inbox__where">
        <span className="inbox__project">{sr?.project_name ?? "—"}</span>
        {sr ? (
          <>
            <span className="inbox__sep">›</span>
            <span className="inbox__epCount">
              {sr.episode_count} {sr.episode_count === 1 ? "episode" : "episodes"}
            </span>
          </>
        ) : null}
      </div>

      <div className="inbox__row">
        <span className="inbox__id">
          {sr?.code ? <span className="inbox__code">{sr.code}</span> : null}
          {sr ? (
            /* Straight to the first episode's canvas: the series has no page of
               its own, and "open the work" is what this link is for. */
            <Link
              className="inbox__name"
              to={
                sr.episodes[0]
                  ? `/projects/${sr.project_id}/scenes/${sr.episodes[0].id}`
                  : `/projects/${sr.project_id}`
              }
              title="Open the work"
            >
              {sr.name}
            </Link>
          ) : (
            <span className="inbox__name">Series</span>
          )}
        </span>
        <span className="inbox__right">
          <span className={`ep__status ep__status--${statusTone}`}>{status}</span>
          {actions}
        </span>
      </div>

      <div className="inbox__facts">
        <Fact label="Assignee" value={sr?.assignee_name} />
        <Fact
          label="Handed in by"
          value={s?.submitted_by_name}
          sub={s ? `version ${s.version}` : undefined}
        />
        <Fact
          label="Submitted"
          value={s?.submitted_at ? fmtWhen(s.submitted_at) : null}
          sub={s?.submitted_at ? ago(s.submitted_at) : undefined}
          numeric
        />
        <Fact
          label={s?.status === "rejected" ? "Sent back" : "Reviewed"}
          value={s?.reviewed_at ? fmtWhen(s.reviewed_at) : null}
          sub={
            s?.reviewed_at
              ? `${ago(s.reviewed_at)}${s.reviewed_by_name ? ` · ${s.reviewed_by_name}` : ""}`
              : s
                ? `waiting on ${s.approver_name ?? "a reviewer"}`
                : undefined
          }
          numeric
        />
      </div>

      {/* The whole thread, not just the last word — and never behind a fold: what
          was said last time is the reason somebody opens this card at all. */}
      {history.length > 0 ? <Thread rows={history} /> : null}

      {children}
    </li>
  );
}
