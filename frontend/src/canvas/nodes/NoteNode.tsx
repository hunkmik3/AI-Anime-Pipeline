import type { NodeProps } from "@xyflow/react";

import type { FlowNode } from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";
import { EditableTextBody } from "./EditableTextBody";

export function NoteNode(props: NodeProps<FlowNode>) {
  const data = props.data;
  return (
    <BaseNodeShell
      data={data}
      selected={props.selected ?? false}
      variant="note"
    >
      <EditableTextBody rfId={props.id} data={data} variant="note" />
    </BaseNodeShell>
  );
}
