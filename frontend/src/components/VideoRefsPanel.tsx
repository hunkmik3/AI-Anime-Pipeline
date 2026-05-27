import { useRef, useState } from "react";

import { mediaUrl, patchNode, uploadImage } from "../api/client";
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

export interface CustomRef {
  mediaId: string;
  label: string;
  description: string;
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
  const fileRef = useRef<HTMLInputElement>(null);

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

  async function addCustom(file: File) {
    setError(null);
    setUploading(true);
    try {
      const projectId = await useGenerationStore.getState().ensureProjectId();
      if (!projectId) {
        setError("no project");
        return;
      }
      const resp = await uploadImage(file, projectId, undefined);
      onCustomRefsChange([
        ...customRefs,
        { mediaId: resp.media_id, label: "", description: "" },
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

      {/* Standalone custom refs (this gen only) */}
      {customRefs.map((c, idx) => (
        <div key={c.mediaId} className="video-ref-row video-ref-row--custom">
          <img className="video-ref-row__thumb" src={thumb(c.mediaId)} alt="custom ref" />
          <div className="video-ref-row__meta">
            <input
              className="ref-label-fields__label"
              type="text"
              value={c.label}
              placeholder="@image1"
              maxLength={40}
              onChange={(e) => {
                const next = [...customRefs];
                next[idx] = { ...c, label: e.target.value };
                onCustomRefsChange(next);
              }}
              aria-label="Custom ref label"
            />
            <span className="video-ref-row__custom-tag">custom · this gen only</span>
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
      ))}

      <button
        type="button"
        className="video-refs-panel__add"
        onClick={() => fileRef.current?.click()}
        disabled={uploading}
      >
        {uploading ? "Uploading…" : "+ Add custom image"}
      </button>
      <input
        ref={fileRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void addCustom(f);
          e.target.value = "";
        }}
      />
      {error && <p className="gen-dialog__hint" style={{ color: "#ef4444" }}>{error}</p>}
    </div>
  );
}
