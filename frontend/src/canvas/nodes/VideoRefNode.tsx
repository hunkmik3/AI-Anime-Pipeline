import { useRef, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import { patchNode, uploadVideo } from "../../api/client";
import { useGenerationStore } from "../../store/generation";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";
import { RefLabelFields } from "./shared/RefLabelFields";

/**
 * VideoRefNode — reference VIDEO for Seedance 2.0 r2v (contract §11.9).
 *
 * Uploads a short clip whose motion/style/camera the gen should reference,
 * then feeds its media_id to a connected VideoNode → `reference_videos`.
 * Mirrors AudioRefNode. Unlike image refs (sent inline as base64), a video
 * ref has no inline path — the worker hoists the media_id to a public R2 URL
 * on submit, so R2 must be configured. Honored only when the resolved model
 * has `supports_video_ref` (Seedance 2.0); other models drop it with a warning.
 */
function VideoRefBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const videoMediaId = data.videoRefMediaId;
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function persist(patch: Partial<FlowboardNodeData>) {
    useShotWorkflowStore.getState().updateNodeData(rfId, patch);
    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) {
      patchNode(dbId, { data: patch }).catch(() => {});
    }
  }

  async function upload(file: File) {
    setError(null);
    setUploading(true);
    try {
      const projectId = await useGenerationStore.getState().ensureProjectId();
      if (!projectId) {
        setError("no project");
        return;
      }
      const dbId = parseInt(rfId, 10);
      const resp = await uploadVideo(file, projectId, isNaN(dbId) ? undefined : dbId);
      persist({ videoRefMediaId: resp.media_id, videoRefMime: resp.mime, status: "done" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "video upload failed");
    } finally {
      setUploading(false);
    }
  }

  // Drag-and-drop a video file straight onto the node (parity with the
  // character/visual nodes). Accept only video/* drops.
  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f && f.type.startsWith("video/")) void upload(f);
    else if (f) setError("Chỉ nhận file video");
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

  return (
    <div
      className="node-body node-body--video-ref"
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
    >
      {videoMediaId ? (
        <div className="video-ref__loaded">
          <video
            className="video-ref__player"
            controls
            preload="metadata"
            src={`/media/${videoMediaId}`}
          />
          <button
            type="button"
            className="video-ref__action"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            {uploading ? "Uploading…" : "Replace"}
          </button>
        </div>
      ) : (
        <div className={`video-ref__empty${dragOver ? " video-ref__empty--over" : ""}`}>
          {dragOver ? (
            <span className="video-ref__hint">Thả video vào đây</span>
          ) : (
            <>
              <button
                type="button"
                className="video-ref__action"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
              >
                {uploading ? "Uploading…" : "Upload video (mp4)"}
              </button>
              <span className="video-ref__hint">Kéo video vào, hoặc tham chiếu chuyển động / phong cách</span>
            </>
          )}
        </div>
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) upload(f);
          e.target.value = "";
        }}
      />
      <RefLabelFields rfId={rfId} data={data} labelPlaceholder="@video1" />
      {error && <p className="video-ref__error">{error}</p>}
    </div>
  );
}

export function VideoRefNode(props: NodeProps<FlowNode>) {
  return (
    <BaseNodeShell
      data={props.data}
      selected={props.selected ?? false}
      showTargetHandle={false}
    >
      <VideoRefBody rfId={props.id} data={props.data} />
    </BaseNodeShell>
  );
}
