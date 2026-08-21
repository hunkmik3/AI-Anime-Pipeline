import { useCallback, useEffect, useState } from "react";

import {
  approveCreditRequest,
  getBudget,
  rejectCreditRequest,
  requestCredit,
  setBudget,
  type BudgetScope,
  type BudgetSummaryDTO,
} from "../api/client";
import { useRevalidate } from "../hooks/useRevalidate";
import { toast } from "../store/toast";

/**
 * Phase 11.1 — credit budget for a Project or a Series.
 *
 * Reading is open to anyone who can see the project (an artist wants to know how
 * much runway is left before generation gets blocked). Setting the ceiling is a
 * BOD/admin act; topping it up is a PM act and always carries a reason, which is
 * why the grant log sits right underneath.
 *
 * Styles are inline on purpose: this panel renders inside both consoles, whose
 * stylesheets are scoped differently, and a half-applied sheet made the labels
 * collide with their values. Inline keeps the layout guaranteed.
 */

const S = {
  panel: {
    margin: "10px 0 14px",
    padding: "16px 18px",
    border: "1px solid #2b3640",
    borderRadius: 12,
    background: "#141b22",
    display: "flex",
    flexDirection: "column",
    gap: 14,
  } as React.CSSProperties,
  head: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  } as React.CSSProperties,
  title: {
    fontSize: "0.7rem",
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    color: "#8a97a3",
  } as React.CSSProperties,
  chip: {
    padding: "4px 12px",
    borderRadius: 999,
    fontSize: "0.82rem",
    fontWeight: 600,
    background: "#232e39",
    color: "#cfd6dd",
    whiteSpace: "nowrap",
  } as React.CSSProperties,
  bar: {
    height: 8,
    borderRadius: 999,
    background: "#232e39",
    overflow: "hidden",
  } as React.CSSProperties,
  meta: { margin: 0, fontSize: "0.78rem", color: "#8a97a3", lineHeight: 1.5 } as React.CSSProperties,
  row: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    flexWrap: "wrap",
  } as React.CSSProperties,
  label: {
    fontSize: "0.78rem",
    color: "#8a97a3",
    minWidth: 118,
    flexShrink: 0,
  } as React.CSSProperties,
  input: {
    background: "#0f141a",
    color: "#e7ecf0",
    border: "1px solid #2b3640",
    borderRadius: 8,
    padding: "8px 11px",
    font: "inherit",
    fontSize: "0.86rem",
    width: 140,
  } as React.CSSProperties,
  btn: {
    padding: "8px 16px",
    borderRadius: 8,
    border: "1px solid transparent",
    background: "#00a76f",
    color: "#fff",
    fontWeight: 600,
    fontSize: "0.84rem",
    cursor: "pointer",
  } as React.CSSProperties,
  btnGhost: {
    padding: "8px 16px",
    borderRadius: 8,
    border: "1px solid #2b3640",
    background: "transparent",
    color: "#e7ecf0",
    fontWeight: 600,
    fontSize: "0.84rem",
    cursor: "pointer",
  } as React.CSSProperties,
  divider: { height: 1, background: "#232e39", border: "none", margin: 0 } as React.CSSProperties,
  grants: {
    listStyle: "none",
    margin: 0,
    padding: 0,
    display: "flex",
    flexDirection: "column",
    gap: 6,
  } as React.CSSProperties,
  grantRow: {
    display: "flex",
    alignItems: "baseline",
    gap: 10,
    fontSize: "0.8rem",
    flexWrap: "wrap",
  } as React.CSSProperties,
};

const CHIP_TONE: Record<string, React.CSSProperties> = {
  ok: { background: "rgba(0,167,111,0.18)", color: "#4bd6a4" },
  warn: { background: "rgba(255,171,0,0.18)", color: "#ffc453" },
  over: { background: "rgba(214,69,69,0.2)", color: "#ff8b8b" },
};
const BAR_TONE: Record<string, string> = { ok: "#00a76f", warn: "#ffab00", over: "#d64545" };

export function BudgetPanel({
  scope,
  scopeId,
  canSetBase = false,
  canGrant = false,
  canDecide = false,
}: {
  scope: BudgetScope;
  scopeId: string;
  /** admin/BOD — may set the base ceiling */
  canSetBase?: boolean;
  /** producer+ — may REQUEST extra credit (pending until approved) */
  canGrant?: boolean;
  /** admin/BOD — may approve or reject those requests */
  canDecide?: boolean;
}) {
  const [b, setB] = useState<BudgetSummaryDTO | null>(null);
  const [baseDraft, setBaseDraft] = useState("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (initial = false) => {
    try {
      const r = await getBudget(scope, scopeId);
      setB(r);
      // Only seed the editable base field on the first load — a background
      // revalidation must not overwrite what the admin is mid-typing.
      if (initial) setBaseDraft(r.base_usd ? String(r.base_usd) : "");
    } catch (e) {
      if (initial) toast(e instanceof Error ? e.message : "could not load the budget", "error");
    }
  }, [scope, scopeId]);

  useEffect(() => {
    void load(true);
  }, [load]);

  // Budget moves under you — generation spend, an admin grant, a PM top-up
  // approved elsewhere. Revalidate on focus + interval so the chip/bar/meta
  // reflect it without a reload.
  useRevalidate(() => void load(false), { intervalMs: 20000 });

  async function saveBase() {
    const v = Math.max(0, parseFloat(baseDraft) || 0);
    setBusy(true);
    try {
      setB(await setBudget(scope, scopeId, v));
      toast(v > 0 ? `Budget set to $${v.toFixed(2)}` : "Budget cleared (unlimited)");
    } catch (e) {
      toast(e instanceof Error ? e.message : "could not save the budget", "error");
    } finally {
      setBusy(false);
    }
  }

  async function askForCredit() {
    const v = parseFloat(amount) || 0;
    if (v <= 0) {
      toast("Enter an amount greater than 0", "error");
      return;
    }
    if (!reason.trim()) {
      toast("A reason is required for a credit request", "error");
      return;
    }
    setBusy(true);
    try {
      const r = await requestCredit(scope, scopeId, { amount_usd: v, reason: reason.trim() });
      setB(r);
      setAmount("");
      setReason("");
      toast(`Requested $${v.toFixed(2)} — waiting on an admin`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "could not send the request", "error");
    } finally {
      setBusy(false);
    }
  }

  /** Admin verdict, straight from the log row. */
  async function decide(id: string, approve: boolean) {
    let note: string | null = null;
    if (!approve) {
      // eslint-disable-next-line no-alert
      note = window.prompt("Why are you rejecting this request?");
      if (note == null || !note.trim()) return;
    }
    setBusy(true);
    try {
      const r = approve
        ? await approveCreditRequest(id)
        : await rejectCreditRequest(id, (note ?? "").trim());
      setB(r);
      toast(approve ? "Approved — budget raised" : "Request rejected");
    } catch (e) {
      toast(e instanceof Error ? e.message : "could not save the decision", "error");
    } finally {
      setBusy(false);
    }
  }

  if (!b) {
    return (
      <div style={{ ...S.panel, color: "#8a97a3", fontSize: "0.85rem" }}>Loading budget…</div>
    );
  }

  const pct = b.used_pct ?? 0;
  // Colour by pressure, so "about to be blocked" reads at a glance.
  const level = pct >= 100 ? "over" : pct >= 80 ? "warn" : "ok";

  return (
    <div style={S.panel}>
      <div style={S.head}>
        <span style={S.title}>Credit budget</span>
        {b.unlimited ? (
          <span style={S.chip}>Unlimited</span>
        ) : (
          <span style={{ ...S.chip, ...CHIP_TONE[level] }}>
            ${b.used_usd.toFixed(2)} / ${b.effective_usd.toFixed(2)}
            {b.remaining_usd !== null ? ` · $${b.remaining_usd.toFixed(2)} left` : ""}
          </span>
        )}
      </div>

      {!b.unlimited && (
        <div style={S.bar}>
          <span
            style={{
              display: "block",
              height: "100%",
              width: `${Math.min(100, pct)}%`,
              background: BAR_TONE[level],
              transition: "width 200ms ease",
            }}
          />
        </div>
      )}

      <p style={S.meta}>
        spent ${b.spent_usd.toFixed(2)} · on hold ${b.reserved_usd.toFixed(2)}
        {b.granted_usd > 0 ? ` · granted +$${b.granted_usd.toFixed(2)}` : ""}
        {b.unlimited ? " · no ceiling set — generation is never blocked" : ""}
      </p>

      {(canSetBase || canGrant) && <hr style={S.divider} />}

      {canSetBase && (
        <div style={S.row}>
          <span style={S.label}>Base budget (USD)</span>
          <input
            type="number"
            min={0}
            step="0.01"
            style={S.input}
            value={baseDraft}
            placeholder="0 = unlimited"
            onChange={(e) => setBaseDraft(e.target.value)}
          />
          <button type="button" style={S.btn} onClick={() => void saveBase()} disabled={busy}>
            Save
          </button>
        </div>
      )}

      {canGrant && (
        <div style={S.row}>
          <span style={S.label}>Request extra</span>
          <input
            type="number"
            min={0}
            step="0.01"
            style={S.input}
            value={amount}
            placeholder="USD"
            onChange={(e) => setAmount(e.target.value)}
          />
          <input
            type="text"
            style={{ ...S.input, width: "auto", flex: "1 1 260px", minWidth: 200 }}
            value={reason}
            placeholder="Reason (required) — sent to an admin for approval"
            onChange={(e) => setReason(e.target.value)}
          />
          <button type="button" style={S.btnGhost} onClick={() => void askForCredit()} disabled={busy}>
            Request
          </button>
        </div>
      )}

      {b.grants && b.grants.length > 0 && (
        <>
          <hr style={S.divider} />
          <span style={S.title}>Credit requests</span>
          <ul style={S.grants}>
            {b.grants.map((g) => {
              const tone =
                g.status === "approved"
                  ? { bg: "rgba(0,167,111,0.18)", fg: "#4bd6a4" }
                  : g.status === "rejected"
                    ? { bg: "rgba(214,69,69,0.2)", fg: "#ff8b8b" }
                    : { bg: "rgba(255,171,0,0.18)", fg: "#ffc453" };
              return (
                <li key={g.id} style={S.grantRow}>
                  <b style={{ color: tone.fg }}>
                    {g.status === "approved" ? "+" : ""}${g.amount_usd.toFixed(2)}
                  </b>
                  <span
                    style={{
                      padding: "1px 8px",
                      borderRadius: 999,
                      fontSize: "0.72rem",
                      fontWeight: 700,
                      background: tone.bg,
                      color: tone.fg,
                      textTransform: "capitalize",
                    }}
                  >
                    {g.status}
                  </span>
                  <span style={{ color: "#cfd6dd" }}>{g.granted_by_name ?? "—"}</span>
                  <span style={{ color: "#8a97a3", fontStyle: "italic" }}>“{g.reason}”</span>
                  {g.decision_note ? (
                    <span style={{ color: "#8a97a3" }}>
                      → {g.decided_by_name ?? "admin"}: “{g.decision_note}”
                    </span>
                  ) : null}
                  {/* Admin verdict, right where the request is listed. */}
                  {canDecide && g.status === "pending" ? (
                    <span style={{ display: "inline-flex", gap: 6, marginLeft: "auto" }}>
                      <button
                        type="button"
                        style={{ ...S.btn, padding: "4px 12px", fontSize: "0.78rem" }}
                        onClick={() => void decide(g.id, true)}
                        disabled={busy}
                      >
                        Approve
                      </button>
                      <button
                        type="button"
                        style={{ ...S.btnGhost, padding: "4px 12px", fontSize: "0.78rem" }}
                        onClick={() => void decide(g.id, false)}
                        disabled={busy}
                      >
                        Reject
                      </button>
                    </span>
                  ) : (
                    <span style={{ color: "#62707c", marginLeft: "auto" }}>
                      {g.created_at ? new Date(g.created_at).toLocaleDateString() : ""}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}

/**
 * One-line budget readout for a table cell: used / effective plus a thin bar.
 * Reads the rollup the list endpoints already return, so adding the column
 * costs no extra requests.
 */
export function BudgetCell({ budget }: { budget?: BudgetSummaryDTO }) {
  if (!budget) return <span style={{ color: "#62707c" }}>—</span>;
  if (budget.unlimited) {
    return (
      <span style={{ color: "#8a97a3", fontSize: "0.8rem" }} title="No ceiling set">
        {budget.spent_usd > 0 ? `$${budget.spent_usd.toFixed(2)} spent` : "—"}
      </span>
    );
  }
  const pct = budget.used_pct ?? 0;
  const level = pct >= 100 ? "over" : pct >= 80 ? "warn" : "ok";
  return (
    <span
      style={{ display: "inline-flex", flexDirection: "column", gap: 4, minWidth: 108 }}
      title={`spent $${budget.spent_usd.toFixed(2)} · on hold $${budget.reserved_usd.toFixed(
        2,
      )}${budget.granted_usd ? ` · granted +$${budget.granted_usd.toFixed(2)}` : ""}`}
    >
      <span style={{ fontSize: "0.82rem", color: CHIP_TONE[level].color as string, fontWeight: 600 }}>
        ${budget.used_usd.toFixed(2)} / ${budget.effective_usd.toFixed(2)}
      </span>
      <span style={{ height: 4, borderRadius: 999, background: "#232e39", overflow: "hidden" }}>
        <span
          style={{
            display: "block",
            height: "100%",
            width: `${Math.min(100, pct)}%`,
            background: BAR_TONE[level],
          }}
        />
      </span>
    </span>
  );
}
