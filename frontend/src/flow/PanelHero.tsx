import type { ReactNode } from "react";

import { thumbUrl } from "../api/client";

/** The five panel states, in the order work moves through them. */
export const STAGES = [
  { key: "todo", label: "to do" },
  { key: "in_progress", label: "in progress" },
  { key: "submitted", label: "in review" },
  { key: "changes_requested", label: "sent back" },
  { key: "approved", label: "approved" },
] as const;

export type StatusCounts = Record<string, number>;

/** Sum several batches' (or panels') status counts into one. */
export function sumCounts(all: StatusCounts[]): StatusCounts {
  const out: StatusCounts = {};
  for (const c of all) for (const [k, n] of Object.entries(c)) out[k] = (out[k] ?? 0) + n;
  return out;
}

/**
 * The header for a container of panels — a project, or one artist's batch.
 *
 * The generic `PageHeader` gave these pages a title, one line of prose and a
 * button in an otherwise empty band. That line ("0 batches · 0 / 0 panels
 * approved") was doing all the work of answering "how is this going", and it
 * answered it badly: approved-vs-total cannot tell a project nobody has started
 * from one where everything is sitting in review.
 *
 * So the same band carries the cover, the whole five-state spread as a bar, and
 * the counts as chips. Nothing decorative was added — the bar is the fact the
 * sentence was failing to convey.
 */
export function PanelHero({
  crumb,
  title,
  thumbMediaId,
  counts,
  total,
  facts,
  actions,
}: {
  crumb?: ReactNode;
  title: ReactNode;
  thumbMediaId?: string | null;
  counts: StatusCounts;
  total: number;
  /** Short bits of context that are not statuses — "3 batches", an assignee. */
  facts?: ReactNode[];
  actions?: ReactNode;
}) {
  const stages = STAGES.map((s) => ({ ...s, n: counts[s.key] ?? 0 })).filter((s) => s.n > 0);
  const pct = total ? Math.round(((counts.approved ?? 0) / total) * 100) : 0;

  return (
    <header className="pn__hero">
      <div className="pn__hero-cover">
        {thumbMediaId ? (
          <img src={thumbUrl(thumbMediaId, 240)} alt="" loading="lazy" />
        ) : (
          <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor"
               strokeWidth="1.4" aria-hidden="true">
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <path d="M3 15l5-5 4 4 3-3 6 6" />
          </svg>
        )}
      </div>

      <div className="pn__hero-body">
        {crumb ? <div className="pn__hero-crumb">{crumb}</div> : null}
        <div className="pn__hero-top">
          <h1 className="pn__hero-title">{title}</h1>
          {total > 0 ? <span className="pn__hero-pct">{pct}%</span> : null}
          {actions ? <div className="pn__hero-acts">{actions}</div> : null}
        </div>

        {total > 0 ? (
          <div className="pn__stack" role="img"
               aria-label={stages.map((s) => `${s.n} ${s.label}`).join(", ")}>
            {stages.map((s) => (
              <span
                key={s.key}
                className={`pn__stack-seg is-${s.key}`}
                style={{ width: `${(s.n / total) * 100}%` }}
                title={`${s.n} ${s.label}`}
              />
            ))}
          </div>
        ) : null}

        <div className="pn__hero-facts">
          {(facts ?? []).map((f, i) => (
            <span key={i} className="pn__fact">
              {f}
            </span>
          ))}
          {stages.map((s) => (
            <span key={s.key} className={`pn__legend-item is-${s.key}`}>
              <i /> {s.n} {s.label}
            </span>
          ))}
        </div>
      </div>
    </header>
  );
}
