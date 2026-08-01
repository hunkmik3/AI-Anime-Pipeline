/**
 * A series' tier, coloured the way the studio's own Series_Master sheet colours it.
 *
 * The mapping and the colours live here, in one place, because tier is rendered on
 * two different boards. When each screen carried its own copy, one showed a plain
 * grey pill and the other plain text — the same value reading as two different
 * things, which is how people stop trusting either screen.
 *
 * The colours themselves are in styles.css (`.crm-chip--tier-*`): solid fill with
 * dark ink, matching the sheet, because these hues are all too light to carry
 * white text.
 */

/** The tiers, in the order the sheet lists them. */
export const TIERS: readonly { key: string; genre: string }[] = [
  { key: "S", genre: "Big-scale action" },
  { key: "A", genre: "Fantasy" },
  { key: "B", genre: "Action" },
  { key: "C", genre: "Thriller / horror" },
  { key: "D", genre: "Drama" },
];

export function TierChip({ tier }: { tier?: string | number | null }) {
  const raw = tier == null ? "" : String(tier).trim();
  if (!raw) return <span className="admin2__muted">—</span>;

  const key = raw.toUpperCase();
  const match = TIERS.find((t) => t.key === key);
  return (
    <span
      // An unrecognised value still renders in the neutral pill: the field is free
      // text, and quietly hiding a typo would make it harder to spot, not easier.
      className={`crm-chip crm-chip--tier${match ? ` crm-chip--tier-${key.toLowerCase()}` : ""}`}
      title={match ? `${key} — ${match.genre}` : `Tier ${raw}`}
    >
      {raw}
    </span>
  );
}


/**
 * Pick a tier.
 *
 * Replaces a `<datalist>`, which was the wrong control for a closed five-value
 * scale: it is a free-text box with suggestions, so it *filtered as you typed* —
 * type "D" and the list showed only D, making it look like the other tiers had
 * gone. It also accepted anything, so a typo became a new tier.
 *
 * Five swatches shown at once solves both: the whole scale is always visible in
 * the same colours as the sheet, and there is nothing to mistype. Clicking the
 * selected one clears it, since tier is optional.
 */
export function TierPicker({
  value,
  onChange,
  disabled,
}: {
  value?: string | null;
  onChange: (tier: string) => void;
  disabled?: boolean;
}) {
  const current = (value ?? "").trim().toUpperCase();
  return (
    <div className="tierpick" role="group" aria-label="Tier">
      {TIERS.map((t) => {
        const on = current === t.key;
        return (
          <button
            key={t.key}
            type="button"
            disabled={disabled}
            aria-pressed={on}
            title={`${t.key} — ${t.genre}`}
            className={`tierpick__opt tierpick__opt--${t.key.toLowerCase()}${
              on ? " is-on" : ""
            }`}
            // Clicking the active one clears it: tier is optional, and without
            // this there would be no way back to "not graded yet".
            onClick={() => onChange(on ? "" : t.key)}
          >
            <b>{t.key}</b>
            <span>{t.genre}</span>
          </button>
        );
      })}
    </div>
  );
}
