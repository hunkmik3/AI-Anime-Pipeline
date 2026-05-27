import { patchNode, uploadImage } from "../../../api/client";
import { useGenerationStore } from "../../../store/generation";
import {
  useShotWorkflowStore,
  type FlowboardNodeData,
} from "../../../store/shotWorkflow";

/**
 * Phase 8.1.5 — append an uploaded image as an ADDITIONAL variant on a
 * Character / VisualAsset node (distinct from the "Upload" reset, which
 * replaces the single media). The new media_id lands in `mediaIds[]`; the
 * node's existing primary (`mediaId` / `primary_variant_id`) is left
 * untouched so the user explicitly promotes it via ResultViewer if wanted.
 *
 * Returns the new media_id, or null on failure (caller surfaces the error).
 */
export async function uploadVariantToNode(
  rfId: string,
  data: FlowboardNodeData,
  file: File,
): Promise<string | null> {
  const projectId = await useGenerationStore.getState().ensureProjectId();
  if (!projectId) return null;
  const dbId = parseInt(rfId, 10);
  const resp = await uploadImage(file, projectId, isNaN(dbId) ? undefined : dbId);

  const existing = (Array.isArray(data.mediaIds) ? data.mediaIds : []).filter(
    (m): m is string => typeof m === "string" && m.length > 0,
  );
  const base = existing.length > 0 ? existing : data.mediaId ? [data.mediaId] : [];
  const next = base.includes(resp.media_id) ? base : [...base, resp.media_id];

  useShotWorkflowStore.getState().updateNodeData(rfId, {
    mediaIds: next,
    // Keep the current primary; only seed mediaId if the node had none.
    mediaId: data.mediaId ?? resp.media_id,
  });
  if (!isNaN(dbId)) {
    patchNode(dbId, { data: { mediaIds: next } }).catch(() => {});
  }
  return resp.media_id;
}
