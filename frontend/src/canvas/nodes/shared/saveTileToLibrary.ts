import { useReferencesStore } from "../../../store/references";
import { useShotWorkflowStore, type FlowboardNodeData } from "../../../store/shotWorkflow";

export type ReferenceKind = "image" | "character" | "visual_asset" | "storyboard_shot";

export function referenceKindFor(nodeType: string): ReferenceKind {
  if (nodeType === "storyboard") return "storyboard_shot";
  if (nodeType === "character") return "character";
  if (nodeType === "visual_asset") return "visual_asset";
  return "image";
}

export function saveTileToLibrary(opts: {
  mediaId: string;
  nodeType: string;
  data: FlowboardNodeData;
}) {
  const { mediaId, nodeType, data } = opts;
  const label =
    typeof data.aiBrief === "string" && data.aiBrief.trim().length > 0
      ? data.aiBrief.slice(0, 80)
      : `#${data.shortId}`;
  void useReferencesStore.getState().save({
    media_id: mediaId,
    kind: referenceKindFor(nodeType),
    ai_brief: typeof data.aiBrief === "string" ? data.aiBrief : null,
    aspect_ratio: typeof data.aspectRatio === "string" ? data.aspectRatio : null,
    label,
    source_shot_id: useShotWorkflowStore.getState().shotId ?? null,
    source_node_short_id:
      typeof data.shortId === "string" ? data.shortId : null,
  });
}
