import type { NodeProps } from "@xyflow/react";

import { mediaUrl } from "../../api/client";
import { resolvePrimaryMediaId, useGenerationStore } from "../../store/generation";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";
import { VideoTile } from "./shared/VideoTile";
import { VideoScrubber } from "./shared/VideoScrubber";

function tileCountFor(data: FlowboardNodeData): number {
  const fromVariants = data.variantCount;
  const fromMedia = data.mediaIds?.length;
  const n = fromVariants && fromVariants > 0 ? fromVariants : fromMedia ?? 1;
  return Math.max(1, Math.min(n, 4));
}

function VideoBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const tileCount = tileCountFor(data);
  const ids = data.mediaIds ?? (data.mediaId ? [data.mediaId] : []);
  const isProcessing = data.status === "queued" || data.status === "running";
  const isError = data.status === "error";
  const isPartial = data.status === "done" && Boolean(data.error);

  // Phase 8.1.5c: the poster is only meaningful for i2v — the upstream
  // image/storyboard IS the video's first frame. For r2v the upstream is a
  // Character/VisualAsset ref sheet (NOT the video's content), so using it as
  // a poster made the Video node look like the character sheet. With no
  // poster, VideoTile renders the actual <video> (its own first frame).
  const { nodes, edges } = useShotWorkflowStore.getState();
  const I2V_SOURCE_TYPES = new Set(["image", "storyboard"]);
  const upstreamEdge = edges.find((e) => {
    if (e.target !== rfId) return false;
    const s = nodes.find((n) => n.id === e.source);
    return !!s && I2V_SOURCE_TYPES.has(s.data.type);
  });
  const upstreamNode = upstreamEdge
    ? nodes.find((n) => n.id === upstreamEdge.source)
    : undefined;
  const posterIds: (string | null)[] =
    upstreamNode?.data.mediaIds ??
    (upstreamNode?.data.mediaId ? [upstreamNode.data.mediaId] : []);

  const tiles: JSX.Element[] = [];
  for (let i = 0; i < tileCount; i++) {
    const rawMid = ids[i];
    const mid = typeof rawMid === "string" && rawMid ? rawMid : undefined;
    const slotError = data.slotErrors?.[i] ?? null;
    const slotBlocked = isPartial && rawMid === null;
    const onClick =
      mid || slotBlocked
        ? () => useGenerationStore.getState().openResultViewer(rfId, i)
        : undefined;
    const rawPoster = posterIds[i] ?? posterIds.find((p) => Boolean(p)) ?? null;
    const poster = typeof rawPoster === "string" ? rawPoster : undefined;
    tiles.push(
      <VideoTile
        key={i}
        mediaId={mid}
        posterMediaId={poster}
        isProcessing={isProcessing && !mid}
        isError={(isError && !mid) || slotBlocked}
        slotError={slotError}
        alt={data.title}
        onClick={onClick}
      />,
    );
  }

  // Phase 8.1.5d: client-side gen progress overlay (estimate; API has no %).
  const genProgress = typeof data.genProgress === "number" ? data.genProgress : null;
  const genPhase = data.genPhase;
  const progressLabel =
    genPhase === "queued" ? "Queued ⏳" : genPhase === "generating" ? "Generating ⚙️" : "Working";

  // Phase 8.4 — inline scrubber on a finished video so the user can extract a
  // continuity frame. Operates on the PRIMARY playable variant.
  const scrubId =
    data.status === "done"
      ? (() => {
          const primary = resolvePrimaryMediaId(data) ?? data.mediaId;
          if (typeof primary === "string" && primary) return primary;
          const first = ids.find((m): m is string => typeof m === "string" && !!m);
          return first;
        })()
      : undefined;

  return (
    <div className="node-body node-body--video">
      <div className={`video-grid video-grid--${tileCount}`}>
        {tiles}
      </div>
      {scrubId && (
        <VideoScrubber videoRfId={rfId} mediaId={scrubId} shotId={data.shotId} />
      )}
      {isProcessing && genProgress !== null && (
        <div className="video-progress" role="status" aria-label={`${progressLabel} ${genProgress}%`}>
          <div className="video-progress__bar">
            <div className="video-progress__fill" style={{ width: `${genProgress}%` }} />
          </div>
          <span className="video-progress__label">{progressLabel} {genProgress}%</span>
        </div>
      )}
      {(isError || isPartial) && data.error && (
        <p
          className={`node-error${isPartial ? " node-error--partial" : ""}`}
          role={isError ? "alert" : "status"}
        >
          {data.error}
        </p>
      )}
    </div>
  );
}

export function VideoNode(props: NodeProps<FlowNode>) {
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
      onDownload={() => downloadAllVideoVariants(data)}
    >
      <VideoBody rfId={props.id} data={data} />
    </BaseNodeShell>
  );
}

function downloadAllVideoVariants(data: FlowboardNodeData) {
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
    a.download = `${safeTitle}-${data.shortId}${suffix}.mp4`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  });
}
