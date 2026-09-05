import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  addPanelVersions,
  getPanel,
  listPanels,
  reopenPanel,
  reviewPanel,
  submitPanel,
  thumbUrl,
  uploadFlowImage,
  type Panel,
} from "../api/client";
import {
  CHAR_PREFIX,
  REF_PREFIX,
  SCENE_PREFIX,
  groupName,
  humanizeGenError,
  resumePanelGens,
  sanitizeErrorDetail,
  useFlowStudioStore,
} from "../store/flowStudio";
import { useGiantflowRole } from "../store/giantflowRole";
import { toast } from "../store/toast";
import { FlowComposer } from "./FlowComposer";
import { ReassignControl } from "./ReassignControl";
import { ViewAsBar } from "./ViewAsBar";
import { FlowViewer } from "./FlowViewer";

/**
 * One panel's workspace — the giantflow studio, scoped to a panel.
 *
 * Deliberately the studio itself and not a variant of it: the same composer, the
 * same grid cards, the same viewer, so every affordance they carry comes along —
 * several references on one generation, @mentions, tagging an image as a
 * character or a scene, pinning, refining, annotating, downloading.
 *
 * Input on the right, output in the middle. The centre grid holds versions and
 * nothing else, because a reference image and a finished version sitting in the
 * same grid read as the same kind of thing and they are not — one is what you
 * are working from, the other is what you made. Material lives in its own column
 * as a two-up grid; a one-per-row column was tried and turned twenty references
 * into a scroll you had to travel past to reach anything.
 *
 * The raw cut is pinned at the top of that column and is not detachable: the
 * backend prepends it to the references on every call, so a ✕ on it would be a
 * control that does nothing. What the artist brings along is detachable.
 */
export function PanelWorkspacePage() {
  const { panelId } = useParams();
  const navigate = useNavigate();
  const pid = Number(panelId);
  const { can } = useGiantflowRole();
  const [panel, setPanel] = useState<Panel | null>(null);
  const [siblings, setSiblings] = useState<Panel[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  const setPanelSink = useFlowStudioStore((s) => s.setPanelSink);
  const loadPanelAssets = useFlowStudioStore((s) => s.loadPanelAssets);
  const assets = useFlowStudioStore((s) => s.assets);
  const genJobs = useFlowStudioStore((s) => s.genJobs);
  const genError = useFlowStudioStore((s) => s.error);
  const notice = useFlowStudioStore((s) => s.notice);
  const clearGenError = useFlowStudioStore((s) => s.clearError);
  const clearNotice = useFlowStudioStore((s) => s.clearNotice);
  const selectedMediaId = useFlowStudioStore((s) => s.selectedMediaId);
  const select = useFlowStudioStore((s) => s.select);
  const tagToPrompt = useFlowStudioStore((s) => s.tagToPrompt);
  const reusePrompt = useFlowStudioStore((s) => s.reusePrompt);
  const uploadAsset = useFlowStudioStore((s) => s.uploadAsset);
  const addRef = useFlowStudioStore((s) => s.addRef);
  const removeAsset = useFlowStudioStore((s) => s.remove);
  const inFlight = genJobs.length;

  // Guards against out-of-order refetches: several gens finishing close together
  // each fire load(), and an earlier (fewer-versions) snapshot resolving LAST
  // would otherwise overwrite a newer one and "swallow" versions on screen. Only
  // the latest-started refetch (always the one with the most versions) applies.
  const loadSeq = useRef(0);
  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const p = await getPanel(pid);
      if (seq === loadSeq.current) setPanel(p);
      return p;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [pid]);

  useEffect(() => {
    void load();
  }, [load]);

  // Point the studio composer at this panel for as long as the page is open, and
  // let go on the way out so the studio grid gets its generations back.
  useEffect(() => {
    setPanelSink(pid);
    void loadPanelAssets(pid);
    resumePanelGens(pid);
    return () => setPanelSink(null);
  }, [pid, setPanelSink, loadPanelAssets]);

  // The rail: the rest of this artist's batch, so moving between panels doesn't
  // mean going back out to the grid every time.
  useEffect(() => {
    if (!panel) return;
    void listPanels(panel.batch_id).then(setSiblings).catch(() => setSiblings([]));
  }, [panel?.batch_id, panel]);

  const prevInFlight = useRef(0);
  useEffect(() => {
    // Refetch every time a generation FINISHES (the in-flight count dropped), so
    // each variant appears the MOMENT it lands — not only once the whole batch is
    // done. Each gen is its own request and files its version as it completes, so
    // a mid-batch refetch shows exactly the variants ready so far.
    if (inFlight < prevInFlight.current) {
      void load();
      void loadPanelAssets(pid);
    }
    prevInFlight.current = inFlight;
  }, [inFlight, load, loadPanelAssets, pid]);

  /** Hand this version over. Submitting IS choosing — that is why the button
   *  lives on a card and not in the header. */
  async function submit(mediaId: string) {
    try {
      setPanel(await submitPanel(pid, mediaId));
      toast("Submitted for review.");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Submit failed");
    }
  }

  /** A file that came back from Photoshop enters as a VERSION, not a reference.
   *  That collapses the external round-trip into the one submit mechanic: an
   *  outside edit is just another version that happens not to be machine-made. */
  async function uploadVersion(files: File[]) {
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    try {
      for (const f of images) {
        const { media_id } = await uploadFlowImage(f);
        // model_used stays null on purpose: it is how a PM tells a retouched
        // file from a generated one.
        await addPanelVersions(pid, [media_id], null);
      }
      await load();
      await loadPanelAssets(pid);
      toast(`${images.length} version(s) added from file.`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Upload failed");
    }
  }

  async function ingest(files: File[]) {
    const images = files.filter((f) => f.type.startsWith("image/"));
    for (const f of images) {
      const id = await uploadAsset(f);
      // Attached straight away: you dropped it here to use it, not to file it.
      if (id) addRef(id);
    }
  }

  if (error) return <p className="inbox__err">{error}</p>;
  if (!panel) return <p className="rfoot">Loading…</p>;

  // Two different reasons the tools are unavailable, and they must stay
  // distinguishable: the work is signed off, versus this role never generates.
  const locked = panel.status === "approved" || !can("panel.generate");
  const versions = [...(panel.versions ?? [])].reverse(); // newest first
  const rawIds = new Set((panel.raw ?? []).map((r) => r.media_id));
  const versionIds = new Set((panel.versions ?? []).map((v) => v.media_id));
  // Input, as opposed to output: whatever is in this panel's library that it did
  // not produce and did not arrive with. The store keeps it newest-first and
  // pushes uploads onto the front, so a dropped image appears without a reload.
  const refs = assets.filter((a) => !rawIds.has(a.mediaId) && !versionIds.has(a.mediaId));

  return (
    <div className={`pn__studio${collapsed ? " is-narrow" : ""}`}>
      {/* ── left rail: the batch, panel by panel ── */}
      <aside className="fn pn__rail">
        <Link to={`/giantflow/batch/${panel.batch_id}`} className="pn__rail-back">
          {collapsed ? "«" : "← All panels"}
        </Link>

        <div className="fn__projects pn__rail-list">
          {siblings.map((p) => (
            <div
              key={p.id}
              className={`fn__prow pn__rail-row${p.id === pid ? " is-on" : ""}`}
              title={p.code}
              onClick={() => p.id !== pid && navigate(`/giantflow/panel/${p.id}`)}
            >
              {p.raw_media_id ? (
                <img className="pn__rail-thumb" src={thumbUrl(p.raw_media_id, 96)} alt="" loading="lazy" />
              ) : (
                <span className="pn__rail-thumb" />
              )}
              {!collapsed && (
                <>
                  <span className="fn__prow-name">{p.code}</span>
                  <span className={`pn__rail-dot is-${p.status}`} title={STATUS_TEXT[p.status]} />
                </>
              )}
            </div>
          ))}
        </div>

        <div className="fn__spacer" />
        <button
          type="button"
          className="fn__ghost"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "»" : "« Collapse"}
        </button>
      </aside>

      {/* ── centre: grid + composer, exactly as the studio has it ── */}
      <main
        className="fc-center pn__center"
        onDragEnter={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragOver={(e) => e.preventDefault()}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          void ingest(Array.from(e.dataTransfer.files ?? []));
        }}
      >
        {dragOver ? (
          <div className="fc-drop">
            <div className="fc-drop__inner">⬇ Drop images to use as references</div>
          </div>
        ) : null}

        <div className="fc-top pn__top">
          <b className="pn__top-code" title={panel.code}>
            {panel.code}
          </b>
          <span className={`pn__top-status is-${panel.status}`}>{STATUS_TEXT[panel.status]}</span>
          <span className="fc-count">
            {versions.length} version{versions.length === 1 ? "" : "s"}
          </span>
          {/* No room for the nav strip in a three-pane studio, but this is the
              page where seeing the artist's view matters most — so the switch
              comes along on its own. */}
          <ViewAsBar />
          {/* The way back in from outside software: download a version, retouch
              it, bring the file here and it becomes the next version. */}
          {!locked && can("panel.submit") ? <UploadVersionButton onFiles={uploadVersion} /> : null}
          {can("batch.manage") ? <ReassignControl panel={panel} onChanged={setPanel} /> : null}
          {can("panel.review") ? <ReviewBar panel={panel} onChanged={setPanel} /> : null}
        </div>

        {/* Two reasons the tools are gone, and they must not share a message: a
            viewer being told the panel is "approved" would be a plain lie. */}
        {panel.status === "approved" ? (
          <div className="fc-banner fc-banner--info">
            ✓ Approved — generation is closed. A PM can reopen this panel.
          </div>
        ) : !can("panel.generate") ? (
          <div className="fc-banner fc-banner--info">
            👁 Read-only — this role does not generate. You can open any image.
          </div>
        ) : null}
        {genError ? (
          <div
            className="fc-banner fc-banner--err"
            onClick={clearGenError}
            role="alert"
            title={sanitizeErrorDetail(genError)}
          >
            ⚠ {humanizeGenError(genError) ?? sanitizeErrorDetail(genError)}
            <span className="fc-banner__x">✕</span>
          </div>
        ) : null}
        {notice ? (
          <div className="fc-banner fc-banner--info" onClick={clearNotice} role="status">
            ℹ {notice}
            <span className="fc-banner__x">✕</span>
          </div>
        ) : null}

        <div className="fc-scroll pn__center-scroll">
          {/* Results only. Material moved to the right column: mixing input and
              output in one grid meant a reference image and a finished version
              looked like the same kind of thing, and they are not. */}
          <div className="fc-grid">
            {genJobs.map((j) => (
              <div key={j.id} className="fc-card pn__card-pending" title={j.prompt}>
                <span>generating…</span>
              </div>
            ))}
            {versions.map((v) => {
              const asset = assets.find((a) => a.mediaId === v.media_id);
              return (
                <div
                  key={v.media_id}
                  role="button"
                  tabIndex={0}
                  className={`fc-card${selectedMediaId === v.media_id ? " is-selected" : ""}`}
                  title={v.model_used ?? undefined}
                  onClick={() => select(v.media_id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      select(v.media_id);
                    }
                  }}
                >
                  <img src={thumbUrl(v.media_id, 400)} alt="" loading="lazy" decoding="async" />
                  <span className="fc-badge fc-badge--ver">v{v.version}</span>
                  {/* A generated version records the model; a file retouched
                      outside and brought back does not. That absence is the only
                      way a PM can tell the two apart, so it is shown. */}
                  {!v.model_used ? (
                    <span className="fc-badge fc-badge--hand" title="Uploaded file, not generated">
                      ✎
                    </span>
                  ) : null}
                  {panel.final_media_id === v.media_id ? (
                    <span className="fc-badge fc-badge--final" title="This is the version submitted">
                      ✓ submitted
                    </span>
                  ) : null}
                  {asset?.pinned ? <span className="fc-badge fc-badge--pin">📌</span> : null}
                  {asset && groupName(asset.tags, CHAR_PREFIX) ? (
                    <span className="fc-badge">👤</span>
                  ) : null}
                  {asset && groupName(asset.tags, SCENE_PREFIX) ? (
                    <span className="fc-badge fc-badge--scene">🎬</span>
                  ) : null}
                  <div className="fc-card__bar">
                    <button
                      type="button"
                      className="fc-card__act"
                      title="Add this image to the prompt as a reference"
                      onClick={(e) => {
                        e.stopPropagation();
                        tagToPrompt(`v${v.version}`, v.media_id, [v.media_id]);
                      }}
                    >
                      @
                    </button>
                    {/* The submit button is HERE, on the version, and nowhere
                        else. A submit button in the header cannot say which of
                        ten tries was meant — which is the whole problem. */}
                    {!locked && can("panel.submit") ? (
                      <button
                        type="button"
                        className={`fc-card__act pn__submit${
                          panel.final_media_id === v.media_id ? " is-on" : ""
                        }`}
                        title={
                          panel.final_media_id === v.media_id
                            ? "This is the version submitted"
                            : "Submit this version for review"
                        }
                        onClick={(e) => {
                          e.stopPropagation();
                          void submit(v.media_id);
                        }}
                      >
                        {panel.final_media_id === v.media_id ? "✓ submitted" : "Submit this"}
                      </button>
                    ) : null}
                    {asset?.prompt ? (
                      <button
                        type="button"
                        className="fc-card__act"
                        title="Reuse this prompt and its references"
                        onClick={(e) => {
                          e.stopPropagation();
                          reusePrompt(
                            asset.prompt ?? "",
                            asset.tags
                              .filter((x) => x.startsWith(REF_PREFIX))
                              .map((x) => x.slice(REF_PREFIX.length)),
                          );
                        }}
                      >
                        ↩
                      </button>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>

          {versions.length === 0 && inFlight === 0 ? (
            <p className="pn__center-hint">
              Nothing generated yet — describe the restyle below. Every generation
              is made against the original, which is pinned on the right.
            </p>
          ) : null}
        </div>

        {locked ? null : <FlowComposer />}
      </main>

      {/* ── right: material — the input this panel works from ──
          A two-up grid, not a single column. The column that was tried on the
          left failed for exactly that reason: one tile per row turned twenty
          references into a scroll you had to travel past to reach anything. */}
      <aside className="pn__mat">
        <h3 className="pn__ws-h">Raw material</h3>
        <p className="pn__mat-hint">Attached to every generation.</p>
        <div className="pn__mat-grid">
          {(panel.raw ?? []).map((r) => (
            <MaterialTile
              key={r.media_id}
              mediaId={r.media_id}
              label="raw"
              onOpen={() => select(r.media_id)}
              onTag={() => tagToPrompt("raw", r.media_id, [r.media_id])}
            />
          ))}
        </div>

        <h3 className="pn__ws-h">
          References
          <UploadButton onFiles={ingest} />
        </h3>
        {refs.length === 0 ? (
          <p className="pn__mat-hint">
            Drop, paste or upload a character sheet, an environment plate, an
            approved neighbouring panel — then <b>@</b> it into the prompt.
          </p>
        ) : (
          <div className="pn__mat-grid">
            {refs.map((a) => {
              const name =
                groupName(a.tags, CHAR_PREFIX) ||
                groupName(a.tags, SCENE_PREFIX) ||
                a.label ||
                "image";
              return (
                <MaterialTile
                  key={a.refId}
                  mediaId={a.mediaId}
                  label={name}
                  pinned={a.pinned}
                  onOpen={() => select(a.mediaId)}
                  onTag={() => tagToPrompt(name, a.mediaId, [a.mediaId])}
                  onRemove={async () => {
                    try {
                      await removeAsset(a.refId);
                    } catch (e) {
                      toast(e instanceof Error ? e.message : "Failed");
                    }
                  }}
                />
              );
            })}
          </div>
        )}
      </aside>

      {/* The studio's own viewer: zoom, annotate, refine, download, and the
          character / scene tagging. Driven by `selectedMediaId`, so mounting it
          is all the wiring it needs. */}
      <FlowViewer />
    </div>
  );
}

const STATUS_TEXT: Record<Panel["status"], string> = {
  todo: "Not started",
  in_progress: "In progress",
  submitted: "In review",
  changes_requested: "Sent back for changes",
  approved: "Approved",
};

/** One picture of material: open it, @ it into the prompt, or detach it. */
function MaterialTile({
  mediaId,
  label,
  pinned,
  onOpen,
  onTag,
  onRemove,
}: {
  mediaId: string;
  label: string;
  pinned?: boolean;
  onOpen: () => void;
  onTag: () => void;
  onRemove?: () => Promise<void>;
}) {
  return (
    <div className="pn__mat-tile" title={label}>
      <button type="button" className="pn__mat-img" onClick={onOpen} title="Open">
        <img src={thumbUrl(mediaId, 240)} alt="" loading="lazy" />
      </button>
      {pinned ? <span className="fc-badge fc-badge--pin">📌</span> : null}
      <div className="pn__mat-foot">
        <span className="pn__mat-label">{label}</span>
        <button type="button" className="fc-card__act" title="Add to the prompt" onClick={onTag}>
          @
        </button>
        {onRemove ? (
          <button
            type="button"
            className="fc-card__act pn__mat-x"
            title="Remove from this panel"
            onClick={() => void onRemove()}
          >
            ✕
          </button>
        ) : null}
      </div>
    </div>
  );
}

function UploadButton({ onFiles }: { onFiles: (files: File[]) => Promise<void> }) {
  const ref = useRef<HTMLInputElement | null>(null);
  return (
    <>
      <input
        ref={ref}
        type="file"
        accept="image/*"
        multiple
        hidden
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = "";
          void onFiles(files);
        }}
      />
      <button
        type="button"
        className="pn__mat-add"
        title="Upload reference images"
        onClick={() => ref.current?.click()}
      >
        ＋
      </button>
    </>
  );
}

/** Bring a file in from outside software as the next VERSION. */
function UploadVersionButton({ onFiles }: { onFiles: (files: File[]) => Promise<void> }) {
  const ref = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <input
        ref={ref}
        type="file"
        accept="image/*"
        multiple
        hidden
        onChange={async (e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = "";
          setBusy(true);
          try {
            await onFiles(files);
          } finally {
            setBusy(false);
          }
        }}
      />
      <button
        type="button"
        className="btn2 pn__upver"
        title="Add a retouched file as the next version"
        disabled={busy}
        onClick={() => ref.current?.click()}
      >
        {busy ? "Uploading…" : "↥ Upload version"}
      </button>
    </>
  );
}

/**
 * The PM's verdict, and the artist's way back out of it.
 *
 * Approve is one click. Sending back is not: it needs a reason, because a
 * rejection with no note is precisely what the Miro board did and what left the
 * artist guessing. The service enforces that too — this is the affordance, not
 * the rule.
 */
function ReviewBar({ panel, onChanged }: { panel: Panel; onChanged: (p: Panel) => void }) {
  const [note, setNote] = useState("");
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<Panel>, ok: string) {
    setBusy(true);
    try {
      onChanged(await fn());
      toast(ok);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  if (panel.status === "approved") {
    return (
      <span className="pn__review">
        <button
          type="button"
          className="btn2"
          disabled={busy}
          onClick={() => void run(() => reopenPanel(panel.id), "Reopened for changes.")}
        >
          Reopen
        </button>
      </span>
    );
  }

  // Nothing to rule on until it has been handed over.
  if (panel.status !== "submitted") return null;

  if (asking) {
    return (
      <span className="pn__review pn__review--ask">
        <input
          className="inbox__input"
          autoFocus
          placeholder="What needs changing?"
          value={note}
          disabled={busy}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && note.trim()) {
              void run(() => reviewPanel(panel.id, false, [note.trim()]), "Sent back.");
              setNote("");
              setAsking(false);
            }
            if (e.key === "Escape") setAsking(false);
          }}
        />
        <button
          type="button"
          className="btn2 btn2--danger"
          disabled={busy || !note.trim()}
          onClick={() => {
            void run(() => reviewPanel(panel.id, false, [note.trim()]), "Sent back.");
            setNote("");
            setAsking(false);
          }}
        >
          Send back
        </button>
        <button type="button" className="btn2" onClick={() => setAsking(false)}>
          Cancel
        </button>
      </span>
    );
  }

  return (
    <span className="pn__review">
      <button
        type="button"
        className="btn2 btn2--primary"
        disabled={busy}
        onClick={() => void run(() => reviewPanel(panel.id, true), "Approved.")}
      >
        ✓ Approve
      </button>
      <button type="button" className="btn2" disabled={busy} onClick={() => setAsking(true)}>
        Send back
      </button>
    </span>
  );
}

