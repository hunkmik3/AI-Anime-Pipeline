import { useRef, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import { mediaUrl, patchNode, uploadImage } from "../../api/client";
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

const ACCEPT_MIME = "image/png,image/jpeg,image/webp,image/gif";

function CharacterBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  // Phase 8.1.5b: display the PRIMARY variant (primary_variant_id ?? mediaId
  // ?? mediaIds[0]) — not always the first gen output. `mediaId` is still the
  // presence gate (a node with any media shows the rendered state).
  const mediaId = data.mediaId;
  const displayId = resolvePrimaryMediaId(data) ?? mediaId;
  const isProcessing = data.status === "queued" || data.status === "running";
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
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
    // Replace/initial upload = a fresh single image: collapse variants to
    // just this one and clear any stale primary so the displayed thumbnail
    // (resolvePrimaryMediaId) shows the new image, not an old primary.
    useShotWorkflowStore.getState().updateNodeData(rfId, {
      mediaId: newMediaId,
      mediaIds: [newMediaId],
      primary_variant_id: undefined,
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

  function onPick() {
    fileInputRef.current?.click();
  }

  function onChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) uploadOwn(f);
    e.target.value = "";
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) uploadOwn(f);
  }

  function onDragOver(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (!dragOver) setDragOver(true);
  }

  function onDragLeave(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
  }

  function openGenerate() {
    useGenerationStore.getState().openGenerationDialog(rfId, data.prompt ?? "");
  }

  if (mediaId) {
    return (
      <div
        className="node-body node-body--character"
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
      >
        <div
          className={`character-avatar${dragOver ? " character-avatar--over" : ""}${uploading ? " character-avatar--uploading" : ""}`}
          onClick={() => useGenerationStore.getState().openResultViewer(rfId)}
          role="button"
          aria-label="Open character variants"
          tabIndex={0}
        >
          <img
            className="character-avatar__img"
            src={mediaUrl(displayId ?? mediaId!)}
            alt={data.title}
          />
          {uploading && <span className="character-drop__overlay">…</span>}
        </div>
        <BriefHint data={data} />
        <button
          type="button"
          className="visual-asset__action"
          onClick={(e) => {
            e.stopPropagation();
            saveTileToLibrary({
              mediaId: displayId ?? mediaId!,
              nodeType: data.type,
              data,
            });
          }}
          title="Save this character to the library"
          aria-label="Save to library"
        >
          ★ Save
        </button>
        <button
          type="button"
          className="visual-asset__action"
          onClick={(e) => {
            e.stopPropagation();
            onPick();
          }}
          disabled={uploading}
          title="Replace this image (resets variants)"
          aria-label="Replace image"
        >
          ⤓ Replace
        </button>
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
        <RefLabelFields rfId={rfId} data={data} />
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPT_MIME}
          style={{ display: "none" }}
          onChange={onChange}
        />
        <input
          ref={variantInputRef}
          type="file"
          accept={ACCEPT_MIME}
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void onVariantPick(f);
            e.target.value = "";
          }}
        />
        {error && <p className="character-drop__error" role="alert">{error}</p>}
      </div>
    );
  }

  return (
    <div
      className="node-body node-body--character"
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
    >
      <div
        className={`character-empty${dragOver ? " character-empty--over" : ""}${isProcessing ? " character-empty--processing" : ""}`}
      >
        {isProcessing ? (
          <span className="visual-asset__hint">Generating…</span>
        ) : dragOver ? (
          <span className="visual-asset__hint">Drop image</span>
        ) : (
          <>
            <button
              type="button"
              className="visual-asset__action"
              onClick={onPick}
              disabled={uploading}
            >
              {uploading ? "Uploading…" : "Upload"}
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
      <RefLabelFields rfId={rfId} data={data} />
      <input
        ref={fileInputRef}
        type="file"
        accept={ACCEPT_MIME}
        style={{ display: "none" }}
        onChange={onChange}
      />
      {error && <p className="character-drop__error" role="alert">{error}</p>}
    </div>
  );
}

export function CharacterNode(props: NodeProps<FlowNode>) {
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
      onDownload={() => triggerCharacterDownload(props.id, data)}
    >
      <CharacterBody rfId={props.id} data={data} />
    </BaseNodeShell>
  );
}

function triggerCharacterDownload(_rfId: string, data: FlowboardNodeData) {
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
