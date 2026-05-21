import { patchEdge } from "../../../api/client";
import { useShotWorkflowStore } from "../../../store/shotWorkflow";
import { useGenerationStore } from "../../../store/generation";

export interface VariantTarget {
  edgeId: string;
  targetRfId: string;
  title: string;
  kind: "image" | "video";
  hasPrompt: boolean;
}

export interface VariantPickerState {
  variantIdx: number;
  targets: VariantTarget[];
}

export function collectGenTargets(srcRfId: string): VariantTarget[] {
  const { nodes, edges } = useShotWorkflowStore.getState();
  const out: VariantTarget[] = [];
  for (const e of edges) {
    if (e.source !== srcRfId) continue;
    const t = nodes.find((n) => n.id === e.target);
    if (!t) continue;
    if (t.data.type !== "image" && t.data.type !== "video") continue;
    out.push({
      edgeId: e.id,
      targetRfId: t.id,
      title: t.data.title || `#${t.data.shortId}`,
      kind: t.data.type as "image" | "video",
      hasPrompt: typeof t.data.prompt === "string" && t.data.prompt.trim().length > 0,
    });
  }
  return out;
}

export async function applyVariantToTarget(variantIdx: number, target: VariantTarget) {
  const edgeDbId = parseInt(target.edgeId, 10);
  if (!isNaN(edgeDbId)) {
    try {
      const updated = await patchEdge(edgeDbId, {
        source_variant_idx: variantIdx,
      });
      useShotWorkflowStore.getState().updateEdgeData(target.edgeId, {
        sourceVariantIdx: updated.source_variant_idx,
      });
    } catch (err) {
      useGenerationStore.setState({
        error: `Couldn't pin variant: ${err instanceof Error ? err.message : String(err)}`,
      });
      return;
    }
  }
  const targetNode = useShotWorkflowStore
    .getState()
    .nodes.find((n) => n.id === target.targetRfId);
  if (!targetNode) return;
  const prompt = (targetNode.data.prompt ?? "").trim();
  if (!prompt) {
    useGenerationStore.getState().openGenerationDialog(target.targetRfId, "");
    return;
  }
  await useGenerationStore.getState().dispatchGeneration(target.targetRfId, {
    prompt,
    kind: target.kind,
    aspectRatio: targetNode.data.aspectRatio,
    variantCount: targetNode.data.variantCount,
  });
}

export function VariantPicker({
  state,
  onPick,
  onCancel,
}: {
  state: VariantPickerState;
  onPick(target: VariantTarget): void;
  onCancel(): void;
}) {
  return (
    <div className="variant-picker" role="dialog" aria-label="Pick downstream target">
      <div className="variant-picker__heading">
        Use variant v{state.variantIdx + 1} for:
      </div>
      <ul className="variant-picker__list">
        {state.targets.map((t) => (
          <li key={t.edgeId}>
            <button
              type="button"
              className="variant-picker__btn"
              onClick={() => onPick(t)}
            >
              {t.title}
              <span className="variant-picker__kind">
                {t.kind === "video" ? "video" : "image"}
                {!t.hasPrompt ? " · empty" : ""}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className="variant-picker__cancel"
        onClick={onCancel}
      >
        Cancel
      </button>
    </div>
  );
}
