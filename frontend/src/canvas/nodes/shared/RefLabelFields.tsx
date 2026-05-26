import { useEffect, useState } from "react";

import { patchNode } from "../../../api/client";
import {
  useShotWorkflowStore,
  type FlowboardNodeData,
} from "../../../store/shotWorkflow";

const DESC_MAX = 300;

/**
 * Phase 8.1 — per-ref @image label + optional description, shown inline on
 * Character / VisualAsset nodes (the r2v reference sources).
 *
 * - `reference_label` (e.g. "@image1", "@kenji") drives positional ordering
 *   of the reference_images array on the backend so the Nth reference_image
 *   block matches @imageN in a pasted Manual prompt.
 * - `reference_description` is optional (decision A3) — Manual mode doesn't
 *   need it; reserved for Phase 8.5 Automation prompt composition.
 *
 * Persists on blur (one patch per field) so typing doesn't spam the backend.
 */
export function RefLabelFields({
  rfId,
  data,
}: {
  rfId: string;
  data: FlowboardNodeData;
}) {
  const [label, setLabel] = useState(data.reference_label ?? "");
  const [desc, setDesc] = useState(data.reference_description ?? "");

  // Re-sync when the node's persisted values change from elsewhere (e.g. a
  // board reload) without clobbering an in-progress edit on every render.
  useEffect(() => {
    setLabel(data.reference_label ?? "");
  }, [data.reference_label]);
  useEffect(() => {
    setDesc(data.reference_description ?? "");
  }, [data.reference_description]);

  function persist(patch: Partial<FlowboardNodeData>) {
    useShotWorkflowStore.getState().updateNodeData(rfId, patch);
    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) {
      patchNode(dbId, { data: patch }).catch(() => {});
    }
  }

  return (
    <div className="ref-label-fields">
      <input
        className="ref-label-fields__label"
        type="text"
        value={label}
        placeholder="@image1"
        maxLength={40}
        onChange={(e) => setLabel(e.target.value)}
        onBlur={() => persist({ reference_label: label.trim() })}
        aria-label="Reference label"
      />
      <textarea
        className="ref-label-fields__desc"
        value={desc}
        placeholder="Description (optional) — JOSH: tall, black suit, amber eyes"
        maxLength={DESC_MAX}
        rows={2}
        onChange={(e) => setDesc(e.target.value)}
        onBlur={() => persist({ reference_description: desc })}
        aria-label="Reference description"
      />
    </div>
  );
}
