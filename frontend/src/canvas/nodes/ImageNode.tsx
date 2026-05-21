import { useRef, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import { mediaUrl, patchNode, uploadImage } from "../../api/client";
import { requestAutoBrief } from "../../api/autoBrief";
import { useGenerationStore } from "../../store/generation";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";
import { BriefHint } from "./shared/BriefHint";
import { ImageTile } from "./shared/ImageTile";
import { saveTileToLibrary } from "./shared/saveTileToLibrary";
import {
  VariantPicker,
  applyVariantToTarget,
  collectGenTargets,
  type VariantPickerState,
} from "./shared/VariantPicker";

const ACCEPT_MIME = "image/png,image/jpeg,image/webp,image/gif";

function tileCountFor(data: FlowboardNodeData): number {
  const fromVariants = data.variantCount;
  const fromMedia = data.mediaIds?.length;
  const n = fromVariants && fromVariants > 0 ? fromVariants : fromMedia ?? 1;
  return Math.max(1, Math.min(n, 4));
}

function ImageBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const tileCount = tileCountFor(data);
  const ids = data.mediaIds ?? (data.mediaId ? [data.mediaId] : []);
  const hasMedia = ids.length > 0;
  const isProcessing = data.status === "queued" || data.status === "running";

  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [picker, setPicker] = useState<VariantPickerState | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function persistMedia(newMediaId: string, aspectRatio?: string) {
    useShotWorkflowStore.getState().updateNodeData(rfId, {
      mediaId: newMediaId,
      mediaIds: undefined,
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
          mediaIds: null,
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

  const hiddenFileInput = (
    <input
      ref={fileInputRef}
      type="file"
      accept={ACCEPT_MIME}
      style={{ display: "none" }}
      onChange={onChange}
    />
  );

  if (!hasMedia && !isProcessing) {
    return (
      <div
        className="node-body node-body--image"
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
      >
        <div className={`character-empty${dragOver ? " character-empty--over" : ""}`}>
          {dragOver ? (
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
        <BriefHint data={data} />
        {hiddenFileInput}
        {error && <p className="character-drop__error" role="alert">{error}</p>}
      </div>
    );
  }

  const isMultiVariant = ids.length >= 2;

  function onUseVariantClick(variantIdx: number) {
    const targets = collectGenTargets(rfId);
    if (targets.length === 0) {
      useGenerationStore.setState({
        error: "Connect this image to a downstream image/video target first.",
      });
      return;
    }
    if (targets.length === 1) {
      void applyVariantToTarget(variantIdx, targets[0]);
      return;
    }
    setPicker({ variantIdx, targets });
  }

  const tiles: JSX.Element[] = [];
  for (let i = 0; i < tileCount; i++) {
    const rawMid = ids[i];
    const mid = typeof rawMid === "string" && rawMid ? rawMid : undefined;
    const onClick = mid
      ? () => useGenerationStore.getState().openResultViewer(rfId, i)
      : undefined;
    tiles.push(
      <ImageTile
        key={i}
        rfId={rfId}
        mediaId={mid}
        isProcessing={isProcessing && !mid}
        alt={data.title}
        onClick={onClick}
        onUseAsRef={
          isMultiVariant && mid && !isProcessing
            ? () => onUseVariantClick(i)
            : undefined
        }
        onSaveToLibrary={
          mid
            ? () =>
                saveTileToLibrary({
                  mediaId: mid,
                  nodeType: data.type,
                  data,
                })
            : undefined
        }
      />,
    );
  }

  return (
    <div
      className={`node-body node-body--image${dragOver ? " node-body--image--over" : ""}`}
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
    >
      <div className={`thumbnail-grid thumbnail-grid--${tileCount}`}>
        {tiles}
      </div>
      {picker && (
        <VariantPicker
          state={picker}
          onPick={(target) => {
            void applyVariantToTarget(picker.variantIdx, target);
            setPicker(null);
          }}
          onCancel={() => setPicker(null)}
        />
      )}
      <BriefHint data={data} />
      {hiddenFileInput}
      {error && <p className="character-drop__error" role="alert">{error}</p>}
    </div>
  );
}

export function ImageNode(props: NodeProps<FlowNode>) {
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
      onDownload={() => downloadAllVariants(data)}
    >
      <ImageBody rfId={props.id} data={data} />
    </BaseNodeShell>
  );
}

function downloadAllVariants(data: FlowboardNodeData) {
  const rawIds =
    data.mediaIds && data.mediaIds.length > 0
      ? data.mediaIds
      : data.mediaId
        ? [data.mediaId]
        : [];
  const ids = rawIds.filter((m): m is string => typeof m === "string" && m.length > 0);
  if (ids.length === 0) return;
  const safeTitle = (data.title || data.type).replace(/[^A-Za-z0-9_-]+/g, "_");
  ids.forEach((mid, i) => {
    const a = document.createElement("a");
    a.href = mediaUrl(mid);
    const suffix = ids.length > 1 ? `-${i + 1}` : "";
    a.download = `${safeTitle}-${data.shortId}${suffix}.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  });
}
