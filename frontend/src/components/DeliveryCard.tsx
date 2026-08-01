import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { DeliverableEpisodeDTO, SubmissionDTO } from "../api/client";

/**
 * One episode's delivery, as it appears in Work and Review.
 *
 * Shared by both because the two pages describe the same thing from opposite ends,
 * and when each had its own markup the same episode showed different facts on each
 * screen.
 *
 * Leads with **where** before **what**: a bare "Episode 9" is ambiguous the moment
 * two series each have one, which the demo data has. Then the four facts a reviewer
 * or an assignee actually needs — who owns it, who handed it in, and when each side
 * last acted — because "waiting on review" means something different after one day
 * than after two weeks.
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
  episode: ep,
  submission: s,
  tone,
  status,
  statusTone,
  actions,
  children,
}: {
  episode: DeliverableEpisodeDTO | null;
  submission: SubmissionDTO | null;
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
        <span className="inbox__project">{ep?.project_name ?? "—"}</span>
        {ep?.series_name ? (
          <>
            <span className="inbox__sep">›</span>
            {ep.series_code ? (
              <span className="inbox__seriesCode">{ep.series_code}</span>
            ) : null}
            <span>{ep.series_name}</span>
          </>
        ) : null}
      </div>

      <div className="inbox__row">
        <span className="inbox__id">
          {ep?.code ? <span className="inbox__code">{ep.code}</span> : null}
          {ep ? (
            <Link
              className="inbox__name"
              to={`/projects/${ep.project_id}/episodes/${ep.id}`}
              title="Everything about this episode"
            >
              {ep.name}
            </Link>
          ) : (
            <span className="inbox__name">Episode</span>
          )}
        </span>
        <span className="inbox__right">
          <span className={`ep__status ep__status--${statusTone}`}>{status}</span>
          {actions}
        </span>
      </div>

      <div className="inbox__facts">
        <Fact label="Assignee" value={ep?.assignee_name} />
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

      {s?.note ? (
        <p className="inbox__note">
          <b>From {s.submitted_by_name ?? "the assignee"}:</b> “{s.note}”
        </p>
      ) : null}

      {s?.review_note ? (
        <p
          className={`inbox__note${s.status === "rejected" ? " inbox__note--back" : ""}`}
        >
          <b>{s.status === "rejected" ? "Sent back" : s.reviewed_by_name ?? "Reviewer"}:</b>{" "}
          “{s.review_note}”
        </p>
      ) : null}

      {children}
    </li>
  );
}
