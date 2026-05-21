import type { FlowboardNodeData } from "../../../store/shotWorkflow";

export function BriefHint({ data }: { data: FlowboardNodeData }) {
  if (data.autoPromptStatus === "pending") {
    return <p className="brief-hint brief-hint--pending">✨ Composing prompt…</p>;
  }
  if (data.aiBriefStatus === "pending") {
    return <p className="brief-hint brief-hint--pending">✨ Analyzing…</p>;
  }
  if (data.aiBrief) {
    return <p className="brief-hint" title={data.aiBrief}>✨ {data.aiBrief}</p>;
  }
  return null;
}
