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
  // character/visual nodes).
  //
  // Accept on the extension as well as the mime, the way AudioRefNode always
  // has. A dropped file's `type` is whatever the OS told the browser, and that
  // is routinely empty — Chrome on Windows reads it from the registry, where
  // the mapping for .mov (sometimes .mp4) is often simply missing. Testing
  // `type` alone rejected a perfectly good clip as "Video files only", which
  // was doubly confusing because that error used to be clipped out of view.
  function isVideoFile(f: File) {
    return f.type.startsWith("video/") || /\.(mp4|mov|webm|m4v)$/i.test(f.name);
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f && isVideoFile(f)) void upload(f);
    else if (f) setError(`Not a video file: ${f.name}`);
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

  // `nodrag` below is load-bearing, not cosmetic. React Flow starts a node
  // drag on mousedown and preventDefault()s it, which swallows the click that
  // would otherwise follow — so a <button> in here never fires its onClick,
  // the file picker never opens, and there is no error to show for it either.
  // React Flow exempts INPUT/SELECT/TEXTAREA by itself (which is why the label
  // and description fields always worked), but NOT button. AudioRefNode has
  // carried this class from the start; this node was missing it everywhere.
  return (
    <div
      className="node-body node-body--video-ref nodrag"
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
    >
      {/* FIRST, not last. `.node-body` is `overflow: hidden`, and this used to
          render below the label + description fields — which already reach the
          bottom of the card. So a failed upload set an error that was clipped
          out of view, and the node just sat there looking like the button had
          done nothing. An error is the one thing on a node that must never be
          the part that gets cropped. */}
      {error && <p className="video-ref__error" role="alert">{error}</p>}

      {videoMediaId ? (
        <div className="video-ref__loaded">
          <video
            className="video-ref__player nodrag"
            controls
            preload="metadata"
            src={`/media/${videoMediaId}`}
          />
          <button
            type="button"
            className="video-ref__action nodrag"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            {uploading ? "Uploading…" : "Replace"}
          </button>
        </div>
      ) : (
        <div className={`video-ref__empty${dragOver ? " video-ref__empty--over" : ""}`}>
          {dragOver ? (
            <span className="video-ref__hint">Drop a video here</span>
          ) : (
            <>
              <button
                type="button"
                className="video-ref__action nodrag"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
              >
                {uploading ? "Uploading…" : "Upload video (mp4)"}
              </button>
              <span className="video-ref__hint">Drag a video in, or reference its motion / style</span>
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
