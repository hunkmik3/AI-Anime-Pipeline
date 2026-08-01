import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  approveCreditRequest,
  listPendingCreditRequests,
  listReviewQueue,
  rejectCreditRequest,
  type CreditGrantDTO,
  type DeliverableEpisodeDTO,
  type SubmissionDTO,
} from "../../api/client";
import { toast } from "../../store/toast";
import { RegistrationsTab } from "./AdminTabs";

/**
 * Approvals — one inbox for everything in the app that is waiting on a decision.
 *
 * Before this, each kind of request lived on its own screen (sign-ups here,
 * credit top-ups inside a project panel, deliverables on /review), so "what
 * needs me?" had no single answer. This page aggregates them; each section
 * still hands off to the surface that owns the detail.
 */

const S = {
  section: {
    border: "1px solid #2b3640",
    borderRadius: 12,
    background: "#141b22",
    marginBottom: 18,
    overflow: "hidden",
  } as React.CSSProperties,
  head: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "14px 18px",
    borderBottom: "1px solid #232e39",
  } as React.CSSProperties,
  title: { fontSize: "0.95rem", fontWeight: 700, color: "#e7ecf0" } as React.CSSProperties,
  count: {
    padding: "2px 10px",
    borderRadius: 999,
    fontSize: "0.75rem",
    fontWeight: 700,
    background: "rgba(255,171,0,0.18)",
    color: "#ffc453",
  } as React.CSSProperties,
  countZero: {
    padding: "2px 10px",
    borderRadius: 999,
    fontSize: "0.75rem",
    fontWeight: 700,
    background: "#232e39",
    color: "#8a97a3",
  } as React.CSSProperties,
  body: { padding: "14px 18px" } as React.CSSProperties,
  empty: { color: "#8a97a3", fontSize: "0.85rem" } as React.CSSProperties,
  row: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "12px 0",
    borderTop: "1px solid #232e39",
    flexWrap: "wrap",
  } as React.CSSProperties,
  btn: {
    padding: "6px 14px",
    borderRadius: 8,
    border: "1px solid transparent",
    background: "#00a76f",
    color: "#fff",
    fontWeight: 600,
    fontSize: "0.8rem",
    cursor: "pointer",
  } as React.CSSProperties,
  btnGhost: {
    padding: "6px 14px",
    borderRadius: 8,
    border: "1px solid #2b3640",
    background: "transparent",
    color: "#e7ecf0",
    fontWeight: 600,
    fontSize: "0.8rem",
    cursor: "pointer",
  } as React.CSSProperties,
};

function Section({
  title,
  hint,
  count,
  children,
}: {
  title: string;
  hint?: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section style={S.section}>
      <header style={S.head}>
        <span style={S.title}>{title}</span>
        <span style={count > 0 ? S.count : S.countZero}>{count}</span>
        {hint ? (
          <span style={{ color: "#8a97a3", fontSize: "0.8rem", marginLeft: "auto" }}>{hint}</span>
        ) : null}
      </header>
      <div style={S.body}>{children}</div>
    </section>
  );
}

// ── credit top-up requests ───────────────────────────────────────────────────

function CreditRequests({ onCount }: { onCount: (n: number) => void }) {
  const [rows, setRows] = useState<CreditGrantDTO[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await listPendingCreditRequests();
      setRows(r.requests);
      onCount(r.requests.length);
    } catch {
      setRows([]);
      onCount(0);
    }
  }, [onCount]);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(g: CreditGrantDTO, approve: boolean) {
    let note: string | null = null;
    if (!approve) {
      // eslint-disable-next-line no-alert
      note = window.prompt("Why are you rejecting this request?");
      if (note == null || !note.trim()) return;
    }
    setBusy(g.id);
    try {
      if (approve) await approveCreditRequest(g.id);
      else await rejectCreditRequest(g.id, (note ?? "").trim());
      toast(approve ? `Approved $${g.amount_usd.toFixed(2)}` : "Request rejected");
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : "could not save the decision", "error");
    } finally {
      setBusy(null);
    }
  }

  if (rows === null) return <div style={S.empty}>Loading…</div>;
  if (rows.length === 0) return <div style={S.empty}>No credit requests waiting.</div>;

  return (
    <div>
      {rows.map((g) => (
        <div key={g.id} style={{ ...S.row, alignItems: "flex-start" }}>
          {/* amount + the budget it lands in */}
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 132 }}>
            <b style={{ color: "#ffc453", fontSize: "1.05rem" }}>+${g.amount_usd.toFixed(2)}</b>
            {g.budget ? (
              <span style={{ color: "#62707c", fontSize: "0.74rem" }}>
                {g.budget.unlimited
                  ? "no ceiling set"
                  : `now $${g.budget.used_usd.toFixed(2)} / $${g.budget.effective_usd.toFixed(2)}`}
              </span>
            ) : null}
          </span>

          {/* exactly what this is for: project → series */}
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 210 }}>
            <span style={{ color: "#e7ecf0", fontWeight: 600 }}>
              {g.project_name ?? "(project)"}
            </span>
            <span style={{ color: "#8a97a3", fontSize: "0.78rem" }}>
              {g.scope === "series" ? (
                <>
                  {g.series_code ? (
                    <b style={{ color: "#4bd6a4", marginRight: 6 }}>{g.series_code}</b>
                  ) : null}
                  {g.series_name ?? "series"}
                </>
              ) : (
                "whole project"
              )}
            </span>
          </span>

          {/* who asked */}
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 150 }}>
            <span style={{ color: "#cfd6dd" }}>{g.granted_by_name ?? "—"}</span>
            <span style={{ color: "#62707c", fontSize: "0.74rem" }}>
              {g.requested_by_username ? `@${g.requested_by_username}` : ""}
              {g.created_at ? ` · ${new Date(g.created_at).toLocaleString()}` : ""}
            </span>
          </span>

          <span style={{ color: "#8a97a3", fontStyle: "italic", flex: 1, minWidth: 200 }}>
            “{g.reason}”
          </span>

          <span style={{ display: "inline-flex", gap: 8 }}>
            <button
              type="button"
              style={S.btn}
              disabled={busy === g.id}
              onClick={() => void decide(g, true)}
            >
              Approve
            </button>
            <button
              type="button"
              style={S.btnGhost}
              disabled={busy === g.id}
              onClick={() => void decide(g, false)}
            >
              Reject
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}

// ── deliverables waiting on a review ────────────────────────────────────────

function DeliverableQueue({ onCount }: { onCount: (n: number) => void }) {
  const [items, setItems] = useState<
    { submission: SubmissionDTO; episode: DeliverableEpisodeDTO | null }[] | null
  >(null);

  useEffect(() => {
    let alive = true;
    void listReviewQueue()
      .then((r) => {
        if (!alive) return;
        setItems(r.items);
        onCount(r.items.length);
      })
      .catch(() => {
        if (!alive) return;
        setItems([]);
        onCount(0);
      });
    return () => {
      alive = false;
    };
  }, [onCount]);

  if (items === null) return <div style={S.empty}>Loading…</div>;
  if (items.length === 0) return <div style={S.empty}>Nothing waiting on a review.</div>;

  return (
    <div>
      {items.map(({ submission: s, episode: ep }) => (
        <div key={s.id} style={{ ...S.row, alignItems: "flex-start" }}>
          <b style={{ color: "#4bd6a4", minWidth: 100 }}>{ep?.code || "—"}</b>

          {/* what it belongs to: project → series → episode */}
          <span style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1, minWidth: 220 }}>
            <span style={{ color: "#e7ecf0", fontWeight: 600 }}>{ep?.name ?? "Episode"}</span>
            <span style={{ color: "#8a97a3", fontSize: "0.78rem" }}>
              {ep?.project_name ?? "—"}
              {ep?.series_name ? ` · ${ep.series_name}` : ""}
            </span>
          </span>

          {/* who submitted, and who it's assigned to */}
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 170 }}>
            <span style={{ color: "#cfd6dd" }}>
              v{s.version} · {s.submitted_by_name ?? "—"}
            </span>
            <span style={{ color: "#62707c", fontSize: "0.74rem" }}>
              {ep?.assignee_name ? `assigned to ${ep.assignee_name}` : "unassigned"}
              {s.submitted_at ? ` · ${new Date(s.submitted_at).toLocaleString()}` : ""}
            </span>
          </span>
          {/* Watching the cut is the whole job, so hand off to the review page. */}
          <Link to="/review" style={{ ...S.btn, textDecoration: "none" }}>
            Watch &amp; decide
          </Link>
        </div>
      ))}
    </div>
  );
}

// ── the page ────────────────────────────────────────────────────────────────

export function ApprovalsTab({ onChanged }: { onChanged?: () => void }) {
  const [credits, setCredits] = useState(0);
  const [deliverables, setDeliverables] = useState(0);
  const [signups, setSignups] = useState(0);

  // The sign-up count comes from the same endpoint the sidebar badge uses.
  useEffect(() => {
    void fetch("/api/admin/registrations/pending-count")
      .then((r) => (r.ok ? r.json() : { count: 0 }))
      .then((d) => setSignups(d.count ?? 0))
      .catch(() => setSignups(0));
  }, []);

  return (
    <>
      <Section
        title="Credit requests"
        hint="A PM asked for more budget — approving raises the ceiling"
        count={credits}
      >
        <CreditRequests onCount={setCredits} />
      </Section>

      <Section
        title="Deliverables to review"
        hint="Submitted cuts waiting on approve / send-back"
        count={deliverables}
      >
        <DeliverableQueue onCount={setDeliverables} />
      </Section>

      <Section
        title="Sign-ups"
        hint="People asking for an account"
        count={signups}
      >
        <RegistrationsTab
          onChanged={() => {
            void fetch("/api/admin/registrations/pending-count")
              .then((r) => (r.ok ? r.json() : { count: 0 }))
              .then((d) => setSignups(d.count ?? 0))
              .catch(() => {});
            onChanged?.();
          }}
        />
      </Section>
    </>
  );
}
