import { useEffect, useState } from "react";

import { patchNode } from "../../../api/client";
import {
  useShotWorkflowStore,
  type FlowboardNodeData,
} from "../../../store/shotWorkflow";

const DESC_MAX = 300;

/** Which @-stream a ref node feeds. Image refs share one numbering (@image1…N)
 *  regardless of their node type; audio and video have their own. */
const PREFIX_BY_TYPE: Record<string, string> = {
  character: "@image",
  visual_asset: "@image",
  master_shot: "@image",
  image: "@image",
  audio_ref: "@audio",
  video_ref: "@video",
};

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
 * The label is a picker, not a text box: it decides ordering, so a typo
 * ("@iamge2") or a duplicate silently mis-binds the prompt. Options are the
 * slots that actually exist — one per same-stream ref in this sequence.
 * Description persists on blur so typing doesn't spam the backend.
 */
export function RefLabelFields({
  rfId,
  data,
  labelPlaceholder = "@image1",
}: {
  rfId: string;
  data: FlowboardNodeData;
  labelPlaceholder?: string;
}) {
  const nodes = useShotWorkflowStore((s) => s.nodes);
  const [desc, setDesc] = useState(data.reference_description ?? "");

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

  const label = data.reference_label ?? "";
  // Fall back to the placeholder's stem so an unmapped type still gets a sane
  // prefix ("@video1" → "@video").
  const prefix =
    PREFIX_BY_TYPE[data.type] ?? labelPlaceholder.replace(/\d+$/, "") ?? "@image";

  // One slot per ref feeding the same @-stream in this sequence: 10 image refs
  // → @image1…@image10. Never fewer than one, so a lone ref can still be named.
  const slots = Math.max(
    1,
    nodes.filter(
      (n) => n.data.shotId === data.shotId && PREFIX_BY_TYPE[n.data.type] === prefix,
    ).length,
  );
  const options = Array.from({ length: slots }, (_, i) => `${prefix}${i + 1}`);
  // A label typed before this became a picker (or a named one like "@kenji")
  // must stay selectable — dropping it would silently re-bind the prompt.
  if (label && !options.includes(label)) options.unshift(label);

  return (
    <div className="ref-label-fields">
      <select
        className="ref-label-fields__label"
        value={label}
        onChange={(e) => persist({ reference_label: e.target.value })}
        aria-label="Reference label"
        title="Which @slot this reference binds to in the prompt"
      >
        <option value="">— no label —</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
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
