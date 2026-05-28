import { useRef, useState } from "react";

import { mediaUrl, patchNode, uploadAudio, uploadImage, uploadVideo } from "../api/client";
import { useGenerationStore, resolvePrimaryMediaId } from "../store/generation";
import { useShotWorkflowStore } from "../store/shotWorkflow";

/**
 * Phase 8.1.5 — video dialog References (r2v) panel.
 *
 * Lists ONE row per upstream Character / VisualAsset / MasterShot ref node
 * (its PRIMARY variant), not one row per variant — fixing the old
 * "shows every variant → gen N videos" confusion. Each row shows the
 * primary thumb + @image label + description. Multi-variant refs expand to
 * a variant strip with a "★ primary" toggle per variant (global per node,
 * persisted). A standalone "+ Add custom image" row uploads an image as a
 * ref for THIS gen only (not persisted to a node).
 */

const R2V_REF_TYPES = new Set(["character", "visual_asset", "master_shot"]);

export type CustomRefKind = "image" | "audio" | "video";

export interface CustomRef {
  mediaId: string;
  label: string;
  description: string;
  kind: CustomRefKind;
  filename: string;
}

function fmtDuration(sec: number): string {
  if (!Number.isFinite(sec) || sec <= 0) return "";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

interface Props {
  rfId: string; // the video node
  customRefs: CustomRef[];
  onCustomRefsChange: (next: CustomRef[]) => void;
}

const thumb = (id: string) => (/^https?:\/\//.test(id) ? id : mediaUrl(id));

export function VideoRefsPanel({ rfId, customRefs, onCustomRefsChange }: Props) {
  const nodes = useShotWorkflowStore((s) => s.nodes);
  const edges = useShotWorkflowStore((s) => s.edges);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  // mediaId → duration seconds, read from the <video>/<audio> metadata once loaded.
  const [durations, setDurations] = useState<Record<string, number>>({});
  // mediaId → server-verify state. "ok" only when the media element actually
  // fetched + decoded the bytes from /media/{id} (genuine server confirmation,
  // not optimistic UI); "error" when that fetch fails. Absent = verifying.
  const [verified, setVerified] = useState<Record<string, "ok" | "error">>({});
  const imageRef = useRef<HTMLInputElement>(null);
  const audioRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLInputElement>(null);

  // Upstream r2v ref nodes feeding this video node, in edge order.
  const refNodes = edges
    .filter((e) => e.target === rfId)
    .map((e) => nodes.find((n) => n.id === e.source))
    .filter((n): n is NonNullable<typeof n> => !!n && R2V_REF_TYPES.has(n.data.type));

  function setPrimary(refNodeId: string, variantId: string) {
    useShotWorkflowStore.getState().updateNodeData(refNodeId, {
      primary_variant_id: variantId,
    });
    const dbId = parseInt(refNodeId, 10);
    if (!isNaN(dbId)) {
      patchNode(dbId, { data: { primary_variant_id: variantId } }).catch(() => {});
    }
  }

  async function addCustom(file: File, kind: CustomRefKind) {
    setError(null);
    setUploading(true);
    setMenuOpen(false);
    try {
      const projectId = await useGenerationStore.getState().ensureProjectId();
      if (!projectId) {
        setError("no project");
        return;
      }
      const up =
        kind === "audio" ? uploadAudio : kind === "video" ? uploadVideo : uploadImage;
      const resp = await up(file, projectId, undefined);
      onCustomRefsChange([
        ...customRefs,
        { mediaId: resp.media_id, label: "", description: "", kind, filename: file.name },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "upload failed");
    } finally {
      setUploading(false);
    }
  }

  const totalRefs = refNodes.length + customRefs.length;

  return (
    <div className="video-refs-panel">
      <div className="gen-dialog__label-row">
        <span className="gen-dialog__label">References (r2v)</span>
        <span className="video-refs-panel__count">
          {totalRefs} ref{totalRefs === 1 ? "" : "s"} · order = @image1…N
        </span>
      </div>

      {refNodes.length === 0 && customRefs.length === 0 && (
        <p className="gen-dialog__hint">
          Nối Character / Visual asset node vào Video node, hoặc thêm custom
          image bên dưới.
        </p>
      )}

      {refNodes.map((n) => {
        const variants = (Array.isArray(n.data.mediaIds) ? n.data.mediaIds : [])
          .filter((m): m is string => typeof m === "string" && m.length > 0);
        const primary = resolvePrimaryMediaId(n.data);
        const isOpen = expanded === n.id;
        const canExpand = variants.length >= 2;
        return (
          <div key={n.id} className="video-ref-row">
            <button
              type="button"
              className="video-ref-row__thumb-btn"
              onClick={() => canExpand && setExpanded(isOpen ? null : n.id)}
              title={canExpand ? "Show variants → set primary" : undefined}
            >
              {primary ? (
                <img className="video-ref-row__thumb" src={thumb(primary)} alt={n.data.title} />
              ) : (
                <span className="video-ref-row__thumb video-ref-row__thumb--empty">?</span>
              )}
              {canExpand && (
                <span className="video-ref-row__variant-count">{variants.length}</span>
              )}
            </button>
            <div className="video-ref-row__meta">
              <span className="video-ref-row__label">
                {(typeof n.data.reference_label === "string" && n.data.reference_label) || "(no label)"}
                <span className="video-ref-row__id"> #{n.data.shortId}</span>
              </span>
              {typeof n.data.reference_description === "string" && n.data.reference_description && (
                <span className="video-ref-row__desc">{n.data.reference_description}</span>
              )}
            </div>
            {isOpen && (
              <div className="video-ref-row__variants">
                {variants.map((v, i) => {
                  const isPrimary = v === primary;
                  return (
                    <button
                      key={v}
                      type="button"
                      className={`video-ref-row__variant${isPrimary ? " video-ref-row__variant--primary" : ""}`}
                      onClick={() => setPrimary(n.id, v)}
                      title={isPrimary ? "Primary" : "Set as primary"}
                      aria-pressed={isPrimary}
                    >
                      <img src={thumb(v)} alt={`Variant ${i + 1}`} />
                      <span className="video-ref-row__variant-badge">{isPrimary ? "★" : i + 1}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}

      {/* Standalone custom refs (this gen only) — real preview per kind. */}
      {customRefs.map((c, idx) => {
        const icon = c.kind === "audio" ? "🔊" : c.kind === "video" ? "🎬" : "🖼️";
        const dur = fmtDuration(durations[c.mediaId] ?? 0);
        const vstate = verified[c.mediaId]; // undefined = verifying
        // Real server confirmation: fires only when the browser fetched +
        // decoded the bytes from /media/{id}.
        const markOk = (e?: React.SyntheticEvent<HTMLMediaElement>) => {
          if (e) {
            const d = e.currentTarget.duration;
            setDurations((prev) => (prev[c.mediaId] ? prev : { ...prev, [c.mediaId]: d }));
          }
          setVerified((prev) => (prev[c.mediaId] ? prev : { ...prev, [c.mediaId]: "ok" }));
        };
        const markErr = () =>
          setVerified((prev) => ({ ...prev, [c.mediaId]: "error" }));
        const badge =
          vstate === "ok" ? (
            <span className="video-ref-row__verify video-ref-row__verify--ok">✓ uploaded</span>
          ) : vstate === "error" ? (
            <span className="video-ref-row__verify video-ref-row__verify--err">✕ not on server</span>
          ) : (
            <span className="video-ref-row__verify">⏳ verifying…</span>
          );
        return (
          <div key={c.mediaId} className="video-ref-row video-ref-row--custom">
            {c.kind === "image" && (
              <img
                className="video-ref-row__thumb"
                src={thumb(c.mediaId)}
                alt={c.filename || "custom ref"}
                onLoad={() => markOk()}
                onError={markErr}
              />
            )}
            {c.kind === "video" && (
              <span className="video-ref-row__thumb-wrap">
                <video
                  className="video-ref-row__thumb"
                  src={thumb(c.mediaId)}
                  preload="metadata"
                  muted
                  onLoadedMetadata={markOk}
                  onError={markErr}
                />
                <span className="video-ref-row__play" aria-hidden>▶</span>
              </span>
            )}
            {c.kind === "audio" && (
              <span className="video-ref-row__thumb video-ref-row__thumb--empty" aria-hidden>
                {icon}
                <audio
                  src={thumb(c.mediaId)}
                  preload="metadata"
                  onLoadedMetadata={markOk}
                  onError={markErr}
                  style={{ display: "none" }}
                />
              </span>
            )}
            <div className="video-ref-row__meta">
              <span className="video-ref-row__label" title={c.filename}>
                {c.filename || `${c.kind} ref`}
              </span>
              <span className="video-ref-row__custom-tag">
                {icon} {c.kind}{dur ? ` · ${dur}` : ""} · this gen only
              </span>
              {badge}
              <input
                className="ref-label-fields__label"
                type="text"
                value={c.label}
                placeholder={c.kind === "image" ? "@image1" : "@label (manual)"}
                maxLength={40}
                onChange={(e) => {
                  const next = [...customRefs];
                  next[idx] = { ...c, label: e.target.value };
                  onCustomRefsChange(next);
                }}
                aria-label="Custom ref label"
              />
              {c.kind !== "image" && (
                <span className="video-ref-row__manual-hint">
                  Manual: tự gõ nhãn này vào prompt — {c.kind} ref chưa có @-binding tự động
                </span>
              )}
            </div>
            <button
              type="button"
              className="video-ref-row__remove"
              onClick={() => onCustomRefsChange(customRefs.filter((_, i) => i !== idx))}
              aria-label="Remove custom ref"
            >
              ✕
            </button>
          </div>
        );
      })}

      {/* Add-custom dropdown: image / audio / video (Phase 8.1.5d). */}
      <div className="video-refs-panel__add-wrap">
        <button
          type="button"
          className="video-refs-panel__add"
          onClick={() => setMenuOpen((o) => !o)}
          disabled={uploading}
          aria-expanded={menuOpen}
        >
          {uploading ? "Uploading…" : "+ Add custom ▾"}
        </button>
        {menuOpen && !uploading && (
          <div className="video-refs-panel__menu" role="menu">
            <button type="button" role="menuitem" onClick={() => imageRef.current?.click()}>🖼️ Image</button>
            <button type="button" role="menuitem" onClick={() => audioRef.current?.click()}>🔊 Audio</button>
            <button type="button" role="menuitem" onClick={() => videoRef.current?.click()}>🎬 Video</button>
          </div>
        )}
      </div>
      <input
        ref={imageRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        style={{ display: "none" }}
        onChange={(e) => { const f = e.target.files?.[0]; if (f) void addCustom(f, "image"); e.target.value = ""; }}
      />
      <input
        ref={audioRef}
        type="file"
        accept="audio/mpeg,audio/wav,audio/x-wav,audio/mp4,audio/aac,audio/ogg"
        style={{ display: "none" }}
        onChange={(e) => { const f = e.target.files?.[0]; if (f) void addCustom(f, "audio"); e.target.value = ""; }}
      />
      <input
        ref={videoRef}
        type="file"
        accept="video/mp4,video/quicktime,video/webm"
        style={{ display: "none" }}
        onChange={(e) => { const f = e.target.files?.[0]; if (f) void addCustom(f, "video"); e.target.value = ""; }}
      />
      {error && <p className="gen-dialog__hint" style={{ color: "#ef4444" }}>{error}</p>}
    </div>
  );
}
