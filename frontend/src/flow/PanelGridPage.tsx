import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useRevalidate } from "../hooks/useRevalidate";

import {
  createPanel,
  deletePanel,
  passThroughPanel,
  downloadPanel,
  exportBatch,
  getBatch,
  importPanelFolder,
  listPanels,
  thumbUrl,
  type Panel,
  type PanelBatch,
  type PanelStatus,
} from "../api/client";
import { GiantflowNav } from "./GiantflowNav";
import { PanelHero } from "./PanelHero";
import { ReassignControl } from "./ReassignControl";
import { toast } from "../store/toast";
import { useGiantflowRole } from "../store/giantflowRole";

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
  // Bulk-upload a whole folder of raw art as new panels (same import path the
  // batch card uses), so a PM can top up a batch that was already imported.
  const [importing, setImporting] = useState<string | null>(null);
  const folderRef = useRef<HTMLInputElement>(null);

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

  // A submit/review done in the Workspace, or switching the previewed role,
  // changes this grid's statuses/version counts — refetch on role-switch, focus
  // and a light interval so it never needs a reload.
  useEffect(() => {
    const onSwitch = () => void load();
    window.addEventListener("flowboard:view-as-changed", onSwitch);
    return () => window.removeEventListener("flowboard:view-as-changed", onSwitch);
  }, [load]);
  useRevalidate(() => void load(), { intervalMs: 15000 });

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const p of panels ?? []) c[p.status] = (c[p.status] ?? 0) + 1;
    return c;
  }, [panels]);

  const shown = useMemo(
    () => (panels ?? []).filter((p) => status === "all" || p.status === status),
    [panels, status],
  );

  // Add/delete a panel by hand is a PM job (batch.manage).
  const { can } = useGiantflowRole();
  const canManage = can("batch.manage");
  const canImport = can("batch.import");

  async function addPanel() {
    const last = (panels ?? [])[(panels ?? []).length - 1]?.code ?? "";
    // eslint-disable-next-line no-alert
    const code = window.prompt(
      "Mã panel mới (sửa số cuối; chèn “…_P035-2” để nằm ngay sau P035):",
      last,
    );
    if (!code || !code.trim()) return;
    try {
      await createPanel(bid, code.trim());
      await load();
      toast(`Đã thêm panel “${code.trim()}”`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Thêm panel lỗi", "error");
    }
  }

  async function onPickFolder(files: FileList | null) {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    setImporting(`Uploading 0/${list.length}…`);
    try {
      const r = await importPanelFolder(
        bid,
        list,
        (sent, total) => {
          setImporting(sent >= total ? "Saving…" : `Uploading ${sent}/${total}…`);
        },
        true, // append: batch already has panels — add new ones, skip existing codes
      );
      await load();
      toast(
        `Đã thêm ${r.panels.length} panel.` +
          (r.skipped_count ? ` ${r.skipped_count} file bị bỏ (không phải ảnh).` : ""),
      );
    } catch (e) {
      toast(e instanceof Error ? e.message : "Upload folder lỗi", "error");
    } finally {
      setImporting(null);
      if (folderRef.current) folderRef.current.value = "";
    }
  }

  async function removePanel(p: Panel) {
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Xóa panel “${p.code}”? Không hoàn tác.`)) return;
    try {
      await deletePanel(p.id);
      await load();
      toast(`Đã xóa panel “${p.code}”`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Xóa panel lỗi", "error");
    }
  }

  async function passPanel(p: Panel) {
    // eslint-disable-next-line no-alert
    if (!window.confirm(`“${p.code}” không cần xử lý — dùng ảnh raw, đánh dấu xong & ném thẳng sang GS?`))
      return;
    try {
      await passThroughPanel(p.id);
      await load();
      toast(`“${p.code}” đã xong (raw → GS)`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Lỗi", "error");
    }
  }

  return (
    <div className="shellpage pn__wide pn__page">
      <GiantflowNav />
      <PanelHero
        crumb={
          batch ? (
            <Link to={`/giantflow/c/${batch.chapter_id}`}>← Batches</Link>
          ) : (
            <Link to="/giantflow">← Project</Link>
          )
        }
        title={batch?.name || "Panels"}
        thumbMediaId={batch?.thumb_media_id}
        counts={counts}
        total={panels?.length ?? 0}
        actions={
          <>
            {canManage ? (
              <button
                className="btn2"
                title="Thêm 1 panel thủ công (vd chèn …_P035-2 để nằm sau P035)"
                onClick={() => void addPanel()}
              >
                + Add panel
              </button>
            ) : null}
            {canImport ? (
              <>
                <input
                  ref={folderRef}
                  type="file"
                  // Non-standard, but the only way to pick a FOLDER; each file's
                  // path/name inside it is what names the panel.
                  {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
                  multiple
                  hidden
                  onChange={(e) => void onPickFolder(e.target.files)}
                />
                <button
                  className="btn2"
                  disabled={!!importing}
                  title="Upload cả folder ảnh raw thành nhiều panel mới (thêm vào batch đã import)"
                  onClick={() => folderRef.current?.click()}
                >
                  {importing ?? "⬆ Upload folder"}
                </button>
              </>
            ) : null}
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
                  await exportBatch(bid);
                  toast(`Đang tải ${counts.approved} panel approved… (kiểm tra thư mục Downloads)`);
                } catch (e) {
                  toast(e instanceof Error ? e.message : "Export failed");
                }
              }}
            >
              ↓ Export approved{counts.approved ? ` (${counts.approved})` : ""}
            </button>
          </>
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
              <PanelCard
                key={p.id}
                panel={p}
                onDelete={canManage ? () => void removePanel(p) : undefined}
                onPassThrough={canManage ? () => void passPanel(p) : undefined}
                onReassigned={
                  canManage
                    ? (up) =>
                        setPanels((prev) =>
                          prev ? prev.map((x) => (x.id === up.id ? up : x)) : prev,
                        )
                    : undefined
                }
              />
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

function PanelCard({
  panel,
  onDelete,
  onPassThrough,
  onReassigned,
}: {
  panel: Panel;
  onDelete?: () => void;
  onPassThrough?: () => void;
  /** Provided (PM/admin) → the per-panel transfer control shows on the card. */
  onReassigned?: (p: Panel) => void;
}) {
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
        {onReassigned ? (
          <ReassignControl panel={panel} onChanged={onReassigned} compact />
        ) : null}
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
        {panel.reassigned ? (
          <span className="pn__card-reassigned" title={`Đã chuyển riêng cho ${panel.assignee_name ?? ""}`}>
            → {panel.assignee_name}
          </span>
        ) : null}
        {panel.unresolved_notes > 0 ? (
          <span className="pn__notes" title="Unresolved notes">
            {panel.unresolved_notes} note{panel.unresolved_notes === 1 ? "" : "s"}
          </span>
        ) : null}
        {onPassThrough && panel.status !== "approved" && panel.raw_media_id ? (
          <button
            type="button"
            className="pn__dl"
            title="Không xử lý — dùng ảnh raw, đánh dấu xong & ném thẳng sang GS"
            style={{ marginLeft: "auto", color: "#4bd6a4" }}
            onClick={(e) => {
              e.preventDefault();
              onPassThrough();
            }}
          >
            ⏭
          </button>
        ) : null}
        {onDelete ? (
          <button
            type="button"
            className="pn__dl"
            title="Xóa panel này"
            style={{ marginLeft: "auto", color: "#ff6b6b" }}
            onClick={(e) => {
              e.preventDefault();
              onDelete();
            }}
          >
            ✕
          </button>
        ) : null}
      </div>
    </li>
  );
}
