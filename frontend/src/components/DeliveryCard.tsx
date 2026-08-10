import { useState, type ReactNode } from "react";
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
                  title="The cut that was handed in for this attempt"
                >
                  the cut ↗
                </a>
              ) : null}
            </div>

            <div className="dlv__leg">
              <span className="dlv__legDot dlv__legDot--in" />
              <span className="dlv__legWho">
                {s.submitted_by_name ?? "Handed in"}
              </span>
              <span className="dlv__legWhen">
                {fmtWhen(s.submitted_at)}
                {s.submitted_at ? <em> · {ago(s.submitted_at)}</em> : null}
              </span>
            </div>
            {s.note ? <p className="dlv__said">“{s.note}”</p> : null}

            <div className="dlv__leg">
              <span
                className={`dlv__legDot dlv__legDot--${answered ? s.status : "open"}`}
              />
              <span className="dlv__legWho">
                {answered
                  ? (s.reviewed_by_name ?? "Reviewer")
                  : `waiting on ${s.approver_name ?? "a reviewer"}`}
              </span>
              <span className="dlv__legWhen">
                {answered ? (
                  <>
                    {fmtWhen(s.reviewed_at)}
                    {s.reviewed_at ? <em> · {ago(s.reviewed_at)}</em> : null}
                  </>
                ) : (
                  /* The number that matters while nothing has happened: how long
                     it has been sitting there. */
                  <em>{s.submitted_at ? `${ago(s.submitted_at)}` : ""}</em>
                )}
              </span>
            </div>
            {s.review_note ? (
              <p
                className={`dlv__said${
                  s.status === "rejected" ? " dlv__said--back" : " dlv__said--ok"
                }`}
              >
                “{s.review_note}”
              </p>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

function Thread({ rows }: { rows: SubmissionDTO[] }) {
  const [open, setOpen] = useState(rows.length > 1);
  return (
    <div className="dlv__thread">
      <button
        type="button"
        className="dlv__histToggle"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "▾" : "▸"} History ({rows.length}{" "}
        {rows.length === 1 ? "attempt" : "attempts"})
      </button>
      {open ? <Attempts rows={rows} /> : null}
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

      {/* The whole thread, not just the last word. Open by default once there has
          been more than one attempt — that is exactly when somebody needs to see
          what was asked for last time — and foldable so a first hand-in does not
          make a wall of one entry. */}
      {history.length > 0 ? <Thread rows={history} /> : null}

      {children}
    </li>
  );
}
