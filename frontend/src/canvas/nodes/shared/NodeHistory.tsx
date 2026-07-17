import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { mediaUrl } from "../../../api/client";

/**
 * Per-node generation history — every attempt ever run on this node, newest
 * first: when, what it cost, whether it failed, and the clip it produced.
 *
 * Deliberately includes failed and in-flight takes: "why did that one break?"
 * is the main reason to open this. A missing cost means the attempt was never
 * billed (failed → refunded, or still running), which is different from $0.
 */

export interface HistoryRow {
  request_id: number;
  status: string; // queued | running | done | failed
  error: string | null;
  created_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  model: string | null;
  resolution: string | null;
  duration_seconds: number | null;
  prompt: string | null;
  cost_usd: number | null;
  ledger_status: string | null;
  user_name: string | null;
  media_ids: string[];
  kept: boolean;
}

const usd = (v: number | null) => (v != null ? `$${v.toFixed(2)}` : "—");

function fmtWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

function fmtTook(ms: number | null): string {
  if (!ms || ms < 0) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export function NodeHistoryModal({
  rfId,
  title,
  onClose,
}: {
  rfId: string;
  title?: string;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<HistoryRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch(`/api/nodes/${rfId}/history`);
        if (!res.ok) throw new Error(`history failed (${res.status})`);
        const data = await res.json();
        if (alive) setRows(data);
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : "load failed");
      }
    })();
    return () => {
      alive = false;
    };
  }, [rfId]);

  const spent = (rows ?? []).reduce((s, r) => s + (r.cost_usd ?? 0), 0);

  return createPortal(
    <div
      className="cpw-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Generation history"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="nhist">
        <div className="nhist__head">
          <div>
            <h2 className="nhist__title">Generation history</h2>
            <p className="nhist__sub">
              {title ? `${title} · ` : ""}
              {rows === null
                ? "loading…"
                : `${rows.length} attempt${rows.length === 1 ? "" : "s"} · ${usd(spent)} spent`}
            </p>
          </div>
          <button className="nhist__close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {err ? <div className="admin-error">{err}</div> : null}

        {rows === null ? (
          <div className="nhist__empty">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="nhist__empty">Nothing generated on this node yet.</div>
        ) : (
          <div className="nhist__list">
            {rows.map((r) => (
              <div
                key={r.request_id}
                className={`nhist__row${r.kept ? " nhist__row--kept" : ""}`}
              >
                <div className="nhist__thumb">
                  {r.media_ids[0] ? (
                    <video
                      src={mediaUrl(r.media_ids[0])}
                      preload="metadata"
                      muted
                      onMouseEnter={(e) => void e.currentTarget.play().catch(() => {})}
                      onMouseLeave={(e) => {
                        e.currentTarget.pause();
                        e.currentTarget.currentTime = 0;
                      }}
                    />
                  ) : (
                    <span className="nhist__thumb-empty" aria-hidden>
                      {r.status === "failed" ? "⚠" : "…"}
                    </span>
                  )}
                </div>

                <div className="nhist__meta">
                  <div className="nhist__line1">
                    <span className={`nhist__status nhist__status--${r.status}`}>
                      {r.status}
                    </span>
                    {r.kept ? <span className="nhist__kept-tag">on the node</span> : null}
                    <span className="nhist__when">{fmtWhen(r.created_at)}</span>
                    {fmtTook(r.duration_ms) ? (
                      <span className="nhist__took">took {fmtTook(r.duration_ms)}</span>
                    ) : null}
                  </div>
                  <div className="nhist__line2">
                    {r.model ?? "—"}
                    {r.resolution ? ` · ${r.resolution}` : ""}
                    {r.duration_seconds ? ` · ${r.duration_seconds}s` : ""}
                    {r.user_name ? ` · ${r.user_name}` : ""}
                  </div>
                  {r.error ? <div className="nhist__err">{r.error}</div> : null}
                  {r.prompt ? (
                    <div className="nhist__prompt" title={r.prompt}>
                      {r.prompt}
                    </div>
                  ) : null}
                </div>

                <div className="nhist__cost">
                  <b>{usd(r.cost_usd)}</b>
                  {/* A blank cost is not $0 — say why nothing was charged. */}
                  {r.cost_usd == null ? (
                    <span className="nhist__cost-note">
                      {r.ledger_status === "reserved" ? "on hold" : "not charged"}
                    </span>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

/** Small header button that opens the history for a node. */
export function NodeHistoryButton({ rfId, title }: { rfId: string; title?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className="node-header__btn"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
        aria-label="Generation history"
        title="Generation history"
        tabIndex={0}
      >
        ⏱
      </button>
      {open ? (
        <NodeHistoryModal rfId={rfId} title={title} onClose={() => setOpen(false)} />
      ) : null}
    </>
  );
}
