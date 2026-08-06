import { relativeTime } from "../components/activity/activity-meta";

/**
 * Who set a series or chapter up, when, and when it is due.
 *
 * The three questions asked first about anything on a slate run by several PMs,
 * and until now the answers lived nowhere on screen — `created_at` existed and
 * was never shown, `created_by` existed only on series, and a deadline existed
 * on neither.
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
  return (
    <dl className="pn__meta">
      <div>
        <dt>Created</dt>
        <dd>
          {createdBy ? <b>{createdBy}</b> : <span className="pn__muted">—</span>}
          {createdAt ? (
            <time dateTime={createdAt} title={new Date(createdAt).toLocaleString()}>
              {new Date(createdAt).toLocaleDateString()}
            </time>
          ) : null}
        </dd>
      </div>
      <div>
        <dt>Due</dt>
        <dd>
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
            <b>{new Date(dueDate).toLocaleDateString()}</b>
          ) : (
            <span className="pn__muted">not set</span>
          )}
          {dueDate ? <DueBadge dueDate={dueDate} /> : null}
        </dd>
      </div>
    </dl>
  );
}

/** How long is left, and whether it has run out. A date alone makes you do the
 *  subtraction, which is the part people get wrong when scanning a list. */
function DueBadge({ dueDate }: { dueDate: string }) {
  const end = new Date(`${dueDate}T23:59:59`);
  const late = end.getTime() < Date.now();
  return (
    <span className={`pn__due-badge${late ? " is-late" : ""}`}>
      {late ? "overdue" : relativeTime(end.toISOString()).replace(" ago", " left")}
    </span>
  );
}
