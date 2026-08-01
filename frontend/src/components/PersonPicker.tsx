import { useState } from "react";

/**
 * Assign one person to something — a Series Producer, an episode's Employee.
 *
 * This exists because the two APIs behind it (`setSeriesProducer`,
 * `setEpisodeAssignee`) shipped with no caller, and the consequence was not
 * cosmetic: only an episode's assignee may submit a deliverable, so with no way to
 * set one, nothing could ever be delivered. The approver chain had the same problem
 * from the other end — with the producer always unset, every submission fell
 * straight through to the PM.
 */
export function PersonPicker({
  label,
  value,
  people,
  disabled,
  allowNone = true,
  noneLabel = "— unassigned —",
  onChange,
}: {
  label: string;
  value: string | null | undefined;
  people: { user_id: string; name: string }[];
  disabled?: boolean;
  allowNone?: boolean;
  noneLabel?: string;
  onChange: (userId: string | null) => Promise<void>;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Someone who has since left the project must still render, or the control would
  // silently show "unassigned" for work that is in fact assigned.
  const known = people.some((p) => p.user_id === value);

  async function pick(next: string) {
    const id = next || null;
    if (id === (value ?? null)) return;
    setSaving(true);
    setError(null);
    try {
      await onChange(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <span className="picker">
      {label ? <span className="picker__label">{label}</span> : null}
      <select
        className={`picker__select${value ? "" : " picker__select--unset"}`}
        value={value ?? ""}
        disabled={disabled || saving}
        aria-label={label || "Assign a person"}
        onChange={(e) => void pick(e.target.value)}
      >
        {allowNone ? <option value="">{noneLabel}</option> : null}
        {!known && value ? (
          <option value={value}>(no longer on this project)</option>
        ) : null}
        {people.map((p) => (
          <option key={p.user_id} value={p.user_id}>
            {p.name}
          </option>
        ))}
      </select>
      {saving ? <span className="picker__saving">saving…</span> : null}
      {error ? <span className="picker__error">{error}</span> : null}
    </span>
  );
}
