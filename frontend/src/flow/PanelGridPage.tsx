import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  downloadPanel,
  exportBatch,
  getBatch,
  listPanels,
  thumbUrl,
  type Panel,
  type PanelBatch,
  type PanelStatus,
} from "../api/client";
import { PanelHero } from "./PanelHero";
import { toast } from "../store/toast";

/**
 * The panel grid — what replaces the Miro board.
 *
 * Miro worked because it was a TABLE: one row per panel, original beside result,
 * notes at the end. This keeps that pairing but as cards, because at 200-300
 * panels the questions people actually ask are "which are waiting on me", "which
 * did the PM send back", "how many has Quân got left" — and those are answered by
 * filtering a dense grid, not by panning a canvas.
 */

const STATUS_LABEL: Record<PanelStatus, string> = {
  todo: "Not started",
  in_progress: "In progress",
  submitted: "In review",
  changes_requested: "Sent back",
  approved: "Approved",
};

const STATUS_ORDER: PanelStatus[] = [
  "todo",
  "in_progress",
  "submitted",
  "changes_requested",
  "approved",
];

export function PanelGridPage() {
  const { batchId } = useParams();
  const bid = Number(batchId);
  const [panels, setPanels] = useState<Panel[] | null>(null);
  const [batch, setBatch] = useState<PanelBatch | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<PanelStatus | "all">("all");

  const load = useCallback(async () => {
    try {
      const [rows, b] = await Promise.all([listPanels(bid), getBatch(bid)]);
      setPanels(rows);
      setBatch(b);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [bid]);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const p of panels ?? []) c[p.status] = (c[p.status] ?? 0) + 1;
    return c;
  }, [panels]);

  const shown = useMemo(
    () => (panels ?? []).filter((p) => status === "all" || p.status === status),
    [panels, status],
  );

  return (
    <div className="shellpage pn__wide pn__page">
      <PanelHero
        crumb={
          batch ? (
            <Link to={`/giantflow/${batch.project_id}`}>← Batches</Link>
          ) : (
            <Link to="/giantflow">← Project</Link>
          )
        }
        title={batch?.name || "Panels"}
        thumbMediaId={batch?.thumb_media_id}
        counts={counts}
        total={panels?.length ?? 0}
        actions={
            <button
              className="btn2"
              disabled={!counts.approved}
              title={
                counts.approved
                  ? `Download ${counts.approved} approved panel(s) as a zip`
                  : "Nothing approved yet — approved panels are what gets exported"
              }
              onClick={async () => {
                try {
                  const r = await exportBatch(bid);
                  toast(
                    `${r.written} approved panel(s) downloaded.` +
                      (r.skipped ? ` ${r.skipped} could not be read.` : ""),
                  );
                } catch (e) {
                  toast(e instanceof Error ? e.message : "Export failed");
                }
              }}
            >
              ↓ Export approved{counts.approved ? ` (${counts.approved})` : ""}
            </button>
        }
        facts={[
          `${panels?.length ?? 0} panel${(panels?.length ?? 0) === 1 ? "" : "s"}`,
          batch?.assignee_name ?? "unassigned",
          ...(batch?.open_notes ? [`${batch.open_notes} open notes`] : []),
        ]}
      />

      {error ? <p className="inbox__err">{error}</p> : null}
      {panels === null ? <p className="rfoot">Loading…</p> : null}

      {panels !== null && panels.length === 0 ? (
        <div className="inbox__empty">
          <b>No panels in this batch.</b>
          Import this artist's folder from the batch list.
        </div>
      ) : null}

      {panels !== null && panels.length > 0 ? (
        <>
          <div className="pn__filters">
            <div className="seg">
              <button
                className={`seg__btn${status === "all" ? " is-on" : ""}`}
                onClick={() => setStatus("all")}
              >
                All {panels.length}
              </button>
              {STATUS_ORDER.map((s) => (
                <button
                  key={s}
                  className={`seg__btn${status === s ? " is-on" : ""}`}
                  onClick={() => setStatus(s)}
                  disabled={!counts[s]}
                >
                  {STATUS_LABEL[s]} {counts[s] ?? 0}
                </button>
              ))}
            </div>

          </div>

          <ul className="pn__grid">
            {shown.map((p) => (
              <PanelCard key={p.id} panel={p} />
            ))}
          </ul>
          {shown.length === 0 ? (
            <p className="rfoot">No panels match that filter.</p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function PanelCard({ panel }: { panel: Panel }) {
  return (
    <li className={`pn__card pn__card--${panel.status}`}>
      {/* Original and result side by side — the pairing every PM note is about
          ("BG bị lệch màu so với truyện gốc" only means something next to the
          original). */}
      <Link to={`/giantflow/panel/${panel.id}`} className="pn__card-shots">
        <span className="pn__shot">
          {panel.raw_media_id ? (
            <img src={thumbUrl(panel.raw_media_id, 520)} alt="" loading="lazy" />
          ) : null}
          <em className="pn__shot-tag">raw{panel.raw_count > 1 ? ` ×${panel.raw_count}` : ""}</em>
        </span>
        <span className="pn__shot">
          {/* What the panel DELIVERS, not its most recent attempt: a PM opening
              this grid must see the image that was actually submitted. */}
          {panel.delivered_media_id ? (
            <img src={thumbUrl(panel.delivered_media_id, 520)} alt="" loading="lazy" />
          ) : (
            <em className="pn__shot-empty">not generated</em>
          )}
          {panel.version_count > 0 ? (
            <em className="pn__shot-tag">
              v{panel.delivered_version}
              {panel.final_media_id ? " ✓" : ""}
            </em>
          ) : null}
        </span>
      </Link>

      <div className="pn__card-meta">
        <b>{panel.code}</b>
        {/* One panel, on its own: the common case is "the PM wants THIS one
            now", long before the batch is finished. */}
        {panel.delivered_media_id ? (
          <button
            type="button"
            className="pn__dl"
            title="Download this panel's delivered version"
            onClick={async (e) => {
              e.preventDefault();
              try {
                await downloadPanel(panel.id);
              } catch (err) {
                toast(err instanceof Error ? err.message : "Download failed");
              }
            }}
          >
            ↓
          </button>
        ) : null}
        <span className="pn__who">v{panel.version_count || 0}</span>
        {panel.unresolved_notes > 0 ? (
          <span className="pn__notes" title="Unresolved notes">
            {panel.unresolved_notes} note{panel.unresolved_notes === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>
    </li>
  );
}
