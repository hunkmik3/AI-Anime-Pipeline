/**
 * Who set a series or chapter up, when, and when it is due.
 *
 * The three questions asked first about anything on a slate run by several PMs,
 * and until now the answers lived nowhere on screen — `created_at` existed and
 * was never shown, `created_by` existed only on series, and a deadline existed
 * on neither.
 *
 * Written as two sentences rather than a label/value table. The table needed a
 * "Created" column heading to explain a name sitting beside a date; the sentence
 * carries its own grammar, reads at a glance, and gives back the width the
 * headings were eating on a narrow card.
 */
export function PanelMeta({
  createdBy,
  createdAt,
  dueDate,
  onSetDue,
}: {
  createdBy: string | null;
  createdAt: string | null;
  dueDate: string | null;
  /** Omitted when the viewer may not manage this row. */
  onSetDue?: (value: string | null) => void;
}) {
  const made = createdAt ? new Date(createdAt) : null;
  return (
    <div className="pn__meta">
      {createdBy || made ? (
        <p className="pn__meta-line">
          Created{createdBy ? <> by <b>{createdBy}</b></> : null}
          {made ? (
            <>
              {" "}on{" "}
              <time dateTime={createdAt ?? undefined} title={made.toLocaleString()}>
                {made.toLocaleDateString()}
              </time>
            </>
          ) : null}
        </p>
      ) : null}

      <p className="pn__meta-line">
        Due{" "}
        {onSetDue ? (
          <input
            type="date"
            className="pn__due-input"
            value={dueDate ?? ""}
            // Click would otherwise follow the card's link.
            onClick={(e) => e.preventDefault()}
            onChange={(e) => onSetDue(e.target.value || null)}
          />
        ) : dueDate ? (
          <b>{localDay(dueDate)?.toLocaleDateString() ?? dueDate}</b>
        ) : (
          <span className="pn__muted">not set</span>
        )}
        {dueDate ? <DueBadge dueDate={dueDate} /> : null}
      </p>
    </div>
  );
}

/**
 * How long is left, and whether it has run out.
 *
 * Counted in whole DAYS, from today to the due day. Not with `relativeTime`:
 * that measures how long ago something happened, so every future date came back
 * "just now" — the badge had exactly two states, "just now" and "overdue", and
 * said nothing about the twelve days in between.
 */
function DueBadge({ dueDate }: { dueDate: string }) {
  const days = daysUntil(dueDate);
  if (days === null) return null;
  const label =
    days < 0
      ? `${-days}d overdue`
      : days === 0
        ? "due today"
        : days === 1
          ? "tomorrow"
          : days < 14
            ? `${days}d left`
            : `${Math.round(days / 7)}w left`;
  return (
    <span className={`pn__due-badge${days < 0 ? " is-late" : days <= 2 ? " is-soon" : ""}`}>
      {label}
    </span>
  );
}

/**
 * "2026-08-18" as a LOCAL calendar day.
 *
 * `new Date("2026-08-18")` is parsed as UTC midnight, so west of Greenwich it
 * prints — and counts as — the day before. A deadline is a day; it must read the
 * same wherever the studio sits.
 */
function localDay(dueDate: string): Date | null {
  const [y, m, d] = dueDate.split("-").map(Number);
  if (!y || !m || !d) return null;
  return new Date(y, m - 1, d);
}

/** Whole days from today to the due day, both taken as local calendar dates. */
function daysUntil(dueDate: string): number | null {
  const due = localDay(dueDate);
  if (!due) return null;
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.round((due.getTime() - start.getTime()) / 86_400_000);
}
