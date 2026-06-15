import { useRef, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import {
  mediaUrl,
  patchNode,
  uploadImage,
  uploadImageFromUrl,
} from "../../api/client";
import { requestAutoBrief } from "../../api/autoBrief";
import { resolvePrimaryMediaId, useGenerationStore } from "../../store/generation";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";
import { BriefHint } from "./shared/BriefHint";
import { RefLabelFields } from "./shared/RefLabelFields";
import { saveTileToLibrary } from "./shared/saveTileToLibrary";
import { uploadVariantToNode } from "./shared/uploadVariant";

function VisualAssetBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const mediaId = data.mediaId;
  // Phase 8.1.5b: show the PRIMARY variant (primary_variant_id ?? mediaId ??
  // mediaIds[0]) so a user-chosen primary drives what's displayed/saved.
  const displayId = resolvePrimaryMediaId(data) ?? mediaId;
  const isProcessing = data.status === "queued" || data.status === "running";
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refineOpen, setRefineOpen] = useState(false);
  const [refinePrompt, setRefinePrompt] = useState("");
  const [refRefreshKey, setRefRefreshKey] = useState(0);
  const [refMediaId, setRefMediaId] = useState<string | null>(null);
  const [linkMode, setLinkMode] = useState(false);
  const [linkValue, setLinkValue] = useState("");
  // Phase 8.4 — "use as continuity" target-shot picker (extracted frames only).
  const [continuityOpen, setContinuityOpen] = useState(false);
  const isExtractedFrame = data.source_type === "extracted_frame";
  // Select the stable array reference, then filter in render — a selector that
  // returns a fresh `.filter()` array each call breaks useSyncExternalStore
  // (infinite "getSnapshot should be cached" loop).
  const shotGroups = useShotWorkflowStore((s) => s.shotGroups);
  const otherShots = shotGroups.filter((g) => g.shot_id !== data.shotId);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const refInputRef = useRef<HTMLInputElement>(null);
  const variantInputRef = useRef<HTMLInputElement>(null);

  async function onVariantPick(file: File) {
    setError(null);
    setUploading(true);
    try {
      const mid = await uploadVariantToNode(rfId, data, file);
      if (!mid) setError("no project");
    } catch (err) {
      setError(err instanceof Error ? err.message : "upload failed");
    } finally {
      setUploading(false);
    }
  }

  function persistMedia(newMediaId: string, aspectRatio?: string) {
    // Replace/initial = single fresh image: clear any stale primary so the
    // displayed thumbnail (resolvePrimaryMediaId) shows the new image.
    useShotWorkflowStore.getState().updateNodeData(rfId, {
      mediaId: newMediaId,
      mediaIds: [newMediaId],
      primary_variant_id: undefined,
      variantCount: 1,
      status: "done",
      aiBrief: undefined,
      aspectRatio,
    });
    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) {
      patchNode(dbId, {
        status: "done",
        data: {
          mediaId: newMediaId,
          mediaIds: [newMediaId],
          primary_variant_id: null,
          variantCount: 1,
          aiBrief: null,
          aspectRatio,
          renderedAt: new Date().toISOString(),
        },
      }).catch(() => {});
    }
    requestAutoBrief(rfId, newMediaId);
  }

  async function uploadOwn(file: File) {
    setError(null);
    setUploading(true);
    try {
      const projectId = await useGenerationStore.getState().ensureProjectId();
      if (!projectId) {
        setError("no project");
        return;
      }
      const dbId = parseInt(rfId, 10);
      const resp = await uploadImage(file, projectId, isNaN(dbId) ? undefined : dbId);
      persistMedia(resp.media_id, resp.aspect_ratio);
    } catch (err) {
      setError(err instanceof Error ? err.message : "upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function uploadFromLink(url: string) {
    const trimmed = url.trim();
    if (!trimmed) return;
    setError(null);
    setUploading(true);
    try {
      const projectId = await useGenerationStore.getState().ensureProjectId();
      if (!projectId) {
        setError("no project");
        return;
      }
      const dbId = parseInt(rfId, 10);
      const resp = await uploadImageFromUrl(
        trimmed,
        projectId,
        isNaN(dbId) ? undefined : dbId,
      );
      persistMedia(resp.media_id, resp.aspect_ratio);
      setLinkMode(false);
      setLinkValue("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "link upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function uploadRef(file: File) {
    setError(null);
    try {
      const projectId = await useGenerationStore.getState().ensureProjectId();
      if (!projectId) {
        setError("no project");
        return;
      }
      const resp = await uploadImage(file, projectId);
      setRefMediaId(resp.media_id);
      setRefRefreshKey((k) => k + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "ref upload failed");
    }
  }

  async function submitRefine() {
    if (!mediaId) return;
    if (!refinePrompt.trim()) return;
    await useGenerationStore.getState().refineImage(rfId, {
      prompt: refinePrompt.trim(),
      refMediaIds: refMediaId ? [refMediaId] : [],
    });
    setRefineOpen(false);
    setRefinePrompt("");
    setRefMediaId(null);
  }

  function openGenerate() {
    useGenerationStore.getState().openGenerationDialog(rfId, data.prompt ?? "");
  }

  async function onSendContinuity(targetShotId: string, targetLabel: string) {
    setContinuityOpen(false);
    const id = await useShotWorkflowStore
      .getState()
      .sendFrameAsContinuity(rfId, targetShotId);
    if (id) {
      useGenerationStore
        .getState()
        .setNotice(`Continuity reference added to ${targetLabel}`);
    } else {
      useGenerationStore.setState({ error: "Couldn't add continuity reference" });
    }
  }

  if (!mediaId) {
    return (
      <div className="node-body node-body--visual-asset">
        <div
          className={`visual-asset__empty${isProcessing ? " visual-asset__empty--processing" : ""}`}
        >
          {isProcessing ? (
            <span className="visual-asset__hint">Generating…</span>
          ) : linkMode ? (
            <div className="visual-asset__link-row">
              <input
                type="url"
                className="visual-asset__link-input"
                placeholder="https://… (png/jpg/webp)"
                value={linkValue}
                onChange={(e) => setLinkValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") uploadFromLink(linkValue);
                  if (e.key === "Escape") {
                    setLinkMode(false);
                    setLinkValue("");
                    setError(null);
                  }
                }}
                disabled={uploading}
                autoFocus
              />
              <button
                type="button"
                className="visual-asset__action"
                onClick={() => uploadFromLink(linkValue)}
                disabled={uploading || !linkValue.trim()}
              >
                {uploading ? "Fetching…" : "Save"}
              </button>
              <button
                type="button"
                className="visual-asset__action"
                onClick={() => {
                  setLinkMode(false);
                  setLinkValue("");
                  setError(null);
                }}
                disabled={uploading}
              >
                ×
              </button>
            </div>
          ) : (
            <>
              <button
                type="button"
                className="visual-asset__action"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
              >
                {uploading ? "Uploading…" : "Upload"}
              </button>
              <button
                type="button"
                className="visual-asset__action"
                onClick={() => {
                  setError(null);
                  setLinkMode(true);
                }}
                disabled={uploading}
              >
                Add link
              </button>
              <button
                type="button"
                className="visual-asset__action"
                onClick={openGenerate}
                disabled={uploading}
              >
                Generate
              </button>
            </>
          )}
        </div>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadOwn(f);
            e.target.value = "";
          }}
        />
        <RefLabelFields rfId={rfId} data={data} />
        {error && <p className="visual-asset__error">{error}</p>}
      </div>
    );
  }

  return (
    <div className="node-body node-body--visual-asset node-body--visual-asset-with-media">
      <div className="visual-asset__media">
        <img
          className="visual-asset__image visual-asset__image--clickable"
          src={mediaUrl(displayId ?? mediaId)}
          alt={data.title}
          role="button"
          tabIndex={0}
          onClick={() => useGenerationStore.getState().openResultViewer(rfId)}
        />
        {!isProcessing && (
          <button
            type="button"
            className="visual-asset__refine-btn"
            onClick={() => setRefineOpen((o) => !o)}
            aria-label="Refine image"
          >
            Refine
          </button>
        )}
      </div>
      <BriefHint data={data} />
      {!isProcessing && (
        <button
          type="button"
          className="visual-asset__action"
          onClick={(e) => {
            e.stopPropagation();
            saveTileToLibrary({
              mediaId: displayId ?? mediaId,
              nodeType: data.type,
              data,
            });
          }}
          title="Save this asset to the library"
          aria-label="Save to library"
        >
          ★ Save
        </button>
      )}
      {!isProcessing && (
        <button
          type="button"
          className="visual-asset__action"
          onClick={(e) => {
            e.stopPropagation();
            fileInputRef.current?.click();
          }}
          disabled={uploading}
          title="Replace this image (resets variants)"
          aria-label="Replace image"
        >
          ⤓ Replace
        </button>
      )}
      {!isProcessing && (
        <button
          type="button"
          className="visual-asset__action"
          onClick={(e) => {
            e.stopPropagation();
            variantInputRef.current?.click();
          }}
          disabled={uploading}
          title="Upload another image as a variant (double-click node → set primary)"
          aria-label="Upload variant"
        >
          + Variant
        </button>
      )}
      <input
        ref={variantInputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void onVariantPick(f);
          e.target.value = "";
        }}
      />
      {/* Phase 8.4 — extracted frames can be sent into another shot as an
          i2v first_frame continuity reference. */}
      {!isProcessing && isExtractedFrame && (
        <div className="continuity-send">
          <button
            type="button"
            className="visual-asset__action continuity-send__btn"
            onClick={(e) => {
              e.stopPropagation();
              setContinuityOpen((o) => !o);
            }}
            title="Send this frame to another shot as a continuity reference"
          >
            → Use as continuity
          </button>
          {continuityOpen && (
            <div className="continuity-send__menu nodrag" onClick={(e) => e.stopPropagation()}>
              {otherShots.length === 0 ? (
                <div className="continuity-send__empty">No other shots</div>
              ) : (
                otherShots.map((g) => (
                  <button
                    key={g.shot_id}
                    type="button"
                    className="continuity-send__item"
                    onClick={() => void onSendContinuity(g.shot_id, g.label)}
                  >
                    {g.label}
                  </button>
                ))
              )}
            </div>
          )}
        </div>
      )}
      {/* Replace picker (has-media state) — the empty-state input is in the
          other branch, so the Replace button needs its own input here. */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) uploadOwn(f);
          e.target.value = "";
        }}
      />
      {refineOpen && (
        <div className="visual-asset__refine-panel" role="region" aria-label="Refine">
          <textarea
            className="visual-asset__refine-textarea"
            placeholder="Describe the change…"
            rows={2}
            value={refinePrompt}
            onChange={(e) => setRefinePrompt(e.target.value)}
          />
          <div className="visual-asset__refine-actions">
            <button
              type="button"
              className="visual-asset__refine-ref"
              onClick={() => refInputRef.current?.click()}
            >
              {refMediaId ? `Ref ✓ (${refRefreshKey})` : "Add ref"}
            </button>
            <button
              type="button"
              className="visual-asset__refine-submit"
              disabled={!refinePrompt.trim()}
              onClick={submitRefine}
            >
              Refine →
            </button>
          </div>
          <input
            ref={refInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) uploadRef(f);
              e.target.value = "";
            }}
          />
        </div>
      )}
      <RefLabelFields rfId={rfId} data={data} />
      {error && <p className="visual-asset__error">{error}</p>}
    </div>
  );
}

export function VisualAssetNode(props: NodeProps<FlowNode>) {
  const data = props.data;
  return (
    <BaseNodeShell
      data={data}
      selected={props.selected ?? false}
      isGenerable
      isDownloadable={!!data.mediaId}
      onGenerate={() =>
        useGenerationStore.getState().openGenerationDialog(props.id, data.prompt ?? "")
      }
      onDownload={() => downloadVisualAsset(data)}
    >
      <VisualAssetBody rfId={props.id} data={data} />
    </BaseNodeShell>
  );
}

function downloadVisualAsset(data: FlowboardNodeData) {
  const dl = resolvePrimaryMediaId(data) ?? data.mediaId;
  if (!dl) return;
  const safeTitle = (data.title || data.type).replace(/[^A-Za-z0-9_-]+/g, "_");
  const a = document.createElement("a");
  a.href = mediaUrl(dl);
  a.download = `${safeTitle}-${data.shortId}.png`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
