import type { FlowboardNodeData } from "../../../store/shotWorkflow";

export function BriefHint({ data }: { data: FlowboardNodeData }) {
  // The "Analyzing…" / "Composing…" pending indicators are intentionally not
  // shown (the auto-brief still runs in the background). Only the finished
  // brief text is surfaced.
  if (data.aiBrief) {
    return <p className="brief-hint" title={data.aiBrief}>✨ {data.aiBrief}</p>;
  }
  return null;
}
