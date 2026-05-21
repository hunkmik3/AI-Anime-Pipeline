import type { FlowboardNodeData } from "../../../store/shotWorkflow";

export function isLLMBusy(data: FlowboardNodeData): boolean {
  return (
    data.autoPromptStatus === "pending"
    || data.aiBriefStatus === "pending"
  );
}
