import { useEffect, useState } from "react";

import {
  getBudget,
  setBudget as putBudget,
  type BudgetScope,
  type BudgetSummaryDTO,
} from "../api/client";

/**
 * The credit ceiling on one node of the hierarchy.
 *
 * Diagram 1 has the PM set a quota when creating an Episode; diagram 5 asks "does
 * this **Sequence** have quota left?". Both tiers carry a ceiling, and this is where
 * it gets set.
 *
 * Shows spend against the ceiling rather than just the number, because the decision
 * a PM is actually making — grant more, or tell them to deliver what they have —
 * depends on how much is already gone.
 *
 * 0 means no ceiling, and the control says so rather than showing "$0": a bare zero
 * reads as a budget of nothing, which is the opposite of what it means.
 */

function money(v: number): string {
  return `$${v.toFixed(2)}`;
}

export function QuotaField({
  scope,
  id,
  disabled,
  compact = false,
}: {
  scope: BudgetScope;
  id: string;
  disabled?: boolean;
  compact?: boolean;
}) {
  const [budget, setBudget] = useState<BudgetSummaryDTO | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const b = await getBudget(scope, id);
        if (!alive) return;
        setBudget(b);
        setDraft(b.unlimited ? "" : String(b.base_usd));
      } catch {
        // A budget the caller may not read renders as empty; failing loudly here
        // would take out the whole row it sits in.
      }
    })();
    return () => {
      alive = false;
    };
  }, [scope, id]);

  async function commit() {
    const text = draft.trim();
    const next = text === "" ? 0 : Number(text);
    if (!Number.isFinite(next) || next < 0) {
      setError("enter a number");
      return;
    }
    if (budget && next === budget.base_usd) return;
    setSaving(true);
    setError(null);
    try {
      setBudget(await putBudget(scope, id, next));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  const spent = budget?.used_usd ?? 0;
  const over =
    budget != null && !budget.unlimited && (budget.remaining_usd ?? 0) <= 0 && spent > 0;

  return (
    <span className="quota">
      {!compact ? <span className="quota__label">Quota $</span> : null}
      <input
        className="quota__input"
        type="number"
        min={0}
        step="0.01"
        value={draft}
        placeholder="∞"
        disabled={disabled || saving}
        aria-label={`${scope} credit quota in USD (blank = unlimited)`}
        title="Blank or 0 = no ceiling"
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => void commit()}
        onKeyDown={(e) => {
          if (e.key === "Enter") void commit();
        }}
      />
      {budget ? (
        <span className={`quota__used${over ? " quota__used--over" : ""}`}>
          {budget.unlimited
            ? `${money(spent)} used`
            : `${money(spent)} / ${money(budget.effective_usd)}${over ? " — out" : ""}`}
        </span>
      ) : null}
      {saving ? <span className="quota__saving">saving…</span> : null}
      {error ? <span className="quota__error">{error}</span> : null}
    </span>
  );
}
