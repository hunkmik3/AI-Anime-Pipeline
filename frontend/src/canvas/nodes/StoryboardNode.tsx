import type { NodeProps } from "@xyflow/react";

import { useGenerationStore } from "../../store/generation";
import type { FlowNode, FlowboardNodeData } from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";
import { ImageTile } from "./shared/ImageTile";
import { saveTileToLibrary } from "./shared/saveTileToLibrary";

function StoryboardBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const shots = Array.isArray(data.shots) ? data.shots : [];
  const isProcessing = data.status === "queued" || data.status === "running";
  const cols = Math.min(Math.max(shots.length || 1, 1), 4);

  if (shots.length === 0) {
    return (
      <div className="storyboard-empty">
        <span style={{ opacity: 0.6 }}>
          Click Generate to plan {data.shotCount ?? 4} narrative shots.
        </span>
      </div>
    );
  }

  function onRetry(idx: number) {
    useGenerationStore.getState().retryStoryboardShot(rfId, idx);
  }

  return (
    <div
      className="thumbnail-grid"
      style={{ gridTemplateColumns: `repeat(${cols}, 1fr)` }}
    >
      {shots.map((shot) => {
        const tileProcessing =
          isProcessing &&
          (shot.status === "queued" || shot.status === "running");
        const isError = shot.status === "error";
        const isBlocked = shot.status === "blocked";
        const onClick = shot.mediaId
          ? () =>
              useGenerationStore.getState().openResultViewer(rfId, shot.idx)
          : undefined;
        return (
          <div key={shot.idx} className="storyboard-tile-wrap">
            <ImageTile
              rfId={rfId}
              mediaId={shot.mediaId}
              isProcessing={tileProcessing}
              alt={`Shot ${shot.idx + 1}`}
              onClick={onClick}
              onSaveToLibrary={
                shot.mediaId
                  ? () =>
                      saveTileToLibrary({
                        mediaId: shot.mediaId as string,
                        nodeType: data.type,
                        data,
                      })
                  : undefined
              }
            />
            {shot.parentShotIdx !== null && shot.parentShotIdx !== undefined && (
              <span
                className="storyboard-badge storyboard-badge--cont"
                title={`Continues from shot ${shot.parentShotIdx + 1}`}
              >
                ↩{shot.parentShotIdx + 1}
              </span>
            )}
            {isBlocked && (
              <span
                className="storyboard-badge storyboard-badge--blocked"
                title={shot.error || "blocked"}
              >
                🔒
              </span>
            )}
            {isError && !tileProcessing && (
              <button
                type="button"
                className="storyboard-retry-btn"
                onClick={(e) => {
                  e.stopPropagation();
                  onRetry(shot.idx);
                }}
                title={shot.error ? `Retry: ${shot.error}` : "Retry shot"}
              >
                ↻
              </button>
            )}
            <span className="storyboard-badge storyboard-badge--idx">
              {shot.idx + 1}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function StoryboardNode(props: NodeProps<FlowNode>) {
  const data = props.data;
  return (
    <BaseNodeShell
      data={data}
      selected={props.selected ?? false}
      isGenerable
      onGenerate={() =>
        useGenerationStore.getState().openGenerationDialog(props.id, data.prompt ?? "")
      }
    >
      <StoryboardBody rfId={props.id} data={data} />
    </BaseNodeShell>
  );
}
