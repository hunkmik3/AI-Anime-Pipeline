import type { NodeProps } from "@xyflow/react";

import { useGenerationStore } from "../../store/generation";
import type { FlowNode } from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";
import { EditableTextBody } from "./EditableTextBody";

export function PromptNode(props: NodeProps<FlowNode>) {
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
      <EditableTextBody rfId={props.id} data={data} variant="prompt" />
    </BaseNodeShell>
  );
}
